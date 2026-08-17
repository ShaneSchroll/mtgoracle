"""Long-running admin jobs (rules and card-cache rebuilds).

Rebuilding the card cache used to be off-limits over HTTP: it ran in a
background thread with an in-process status dict, so the thread lived in
whichever worker answered the POST while the status poll could land on any of
the others - which pinned the deployment to exactly one uvicorn worker.

Keeping the state in users.db removes that constraint. The working thread writes
progress to a row; any worker can answer the poll by reading it. Claiming a job
takes SQLite's write lock (BEGIN IMMEDIATE via auth._write_tx), so two workers
cannot start the same rebuild even if two admins click at the same moment.

The work itself still runs in-process, so a rebuild competes for CPU with
requests on that worker. That is acceptable for something run a few times a
year, and the CLI (`python build_card_cache.py --force`) remains the right tool
for a big migration.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .auth import _db, _now, _write_tx

KINDS = ("rules_ingest", "cards_ingest", "cards_refresh")

# A running row this stale is presumed dead - its worker was restarted or killed
# mid-run - and stops blocking new jobs of the same kind. Both rebuilds write
# progress every batch, so a quarter hour of silence means something is wrong.
STALE_AFTER = timedelta(minutes=15)

# Floor between progress writes. write_db calls its callback once per 2000-card
# batch and the downloader once per chunk; without this a rebuild would spend
# more time writing status rows than doing work.
_WRITE_EVERY = 0.75


class JobBusy(Exception):
    """A job of this kind is already running."""


def _fields(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "phase": row["phase"],
        "done": row["done"],
        "total": row["total"],
        "message": row["message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "percent": (
            max(0, min(100, int(row["done"] * 100 / row["total"])))
            if row["total"] else None
        ),
    }


def _is_stale(row) -> bool:
    try:
        seen = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen > STALE_AFTER


def claim(kind: str, started_by: int | None) -> int:
    """Start a job of `kind`, or raise JobBusy if a live one already exists.

    The check and the insert share one BEGIN IMMEDIATE transaction, so this is
    safe against another worker doing the same thing at the same instant.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown job kind: {kind}")
    now = _now().isoformat()
    with _db() as db, _write_tx(db):
        live = db.execute(
            "SELECT * FROM admin_jobs WHERE kind = ? AND status = 'running'",
            (kind,),
        ).fetchall()
        for row in live:
            if _is_stale(row):
                db.execute(
                    "UPDATE admin_jobs SET status = 'error', message = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("Abandoned - the server restarted while it was running.",
                     now, row["id"]),
                )
            else:
                raise JobBusy(f"A {kind.replace('_', ' ')} job is already running.")
        cur = db.execute(
            "INSERT INTO admin_jobs (kind, status, phase, started_by, "
            "created_at, updated_at) VALUES (?, 'running', 'starting', ?, ?, ?)",
            (kind, started_by, now, now),
        )
        return cur.lastrowid


def _touch(job_id: int, phase: str, done: int, total: int) -> None:
    with _db() as db:
        db.execute(
            "UPDATE admin_jobs SET phase = ?, done = ?, total = ?, updated_at = ? "
            "WHERE id = ?",
            (phase, int(done), int(total), _now().isoformat(), job_id),
        )


def _settle(job_id: int, status: str, message: str) -> None:
    with _db() as db:
        db.execute(
            "UPDATE admin_jobs SET status = ?, message = ?, updated_at = ? "
            "WHERE id = ?",
            (status, message[:500], _now().isoformat(), job_id),
        )


def progress_writer(job_id: int):
    """A (phase, done, total) callback that writes into the job row.

    Matches the signature build_card_cache.write_db and card_ingest expect, so
    the same parsing code drives a terminal bar on the CLI and a progress bar in
    the browser. Writes are throttled; a phase change always flushes.
    """
    state = {"at": 0.0, "phase": None}

    def report(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if phase != state["phase"] or now - state["at"] >= _WRITE_EVERY:
            state["at"], state["phase"] = now, phase
            _touch(job_id, phase, done, total)

    return report


def get(job_id: int) -> dict | None:
    with _db() as db:
        row = db.execute("SELECT * FROM admin_jobs WHERE id = ?", (job_id,)).fetchone()
    return _fields(row) if row else None


def recent(limit: int = 10) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM admin_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_fields(r) for r in rows]


def start(kind: str, started_by: int | None, work) -> int:
    """Claim a job and run `work(progress)` on a background thread.

    `work` receives the progress callback and returns the message to record on
    success. Any exception is caught and stored on the row - a rebuild must
    never take the web process down with it.
    """
    job_id = claim(kind, started_by)

    def runner():
        try:
            message = work(progress_writer(job_id)) or "Finished."
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            traceback.print_exc()
            _settle(job_id, "error", f"{type(exc).__name__}: {exc}")
        else:
            _settle(job_id, "done", message)

    threading.Thread(target=runner, name=f"admin-job-{job_id}", daemon=True).start()
    return job_id
