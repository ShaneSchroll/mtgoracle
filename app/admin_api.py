"""The /api/admin router.

Mirrors every operation in admin.py so day-to-day management can happen in
the browser, while the CLI remains fully functional as a fallback if the
frontend ever has issues. Every endpoint requires an admin session; every
state-changing endpoint also enforces same-origin as defense-in-depth.
"""

import argparse
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request, UploadFile

# The CLI entrypoints live at the repo root, like retriever and mtg_api. The
# panel calls into them directly rather than shelling out: two of admin.py's
# commands prompt on the terminal, and an in-process call keeps the loaded
# environment and gives real progress instead of scraped stdout.
import admin as admin_cli
import build_card_cache
import card_ingest
import ingest
from mtg_api import get_cache

from . import auth, jobs, rules
from .config import BASE_DIR


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_guard(request: Request):
    auth.require_same_origin(request)
    return auth.require_admin(request)


def _user_view(row) -> dict:
    """Shape a user row for the admin table, including this month's spend. The
    limit and `spent_usd` are credit dollars (what the balance drops by);
    `raw_spent_usd` is what we actually pay Anthropic."""
    budget = auth._budget_for(row["id"])
    raw_spent = auth.usage_month_micros(row["id"])
    spent = auth.usage_month_credit_micros(row["id"])
    raw = row["monthly_budget_micros"] if "monthly_budget_micros" in row.keys() else None
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "approved": bool(row["approved"]),
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        # The rest of the subscription columns, so the table can show every
        # stored field. password_hash is deliberately never exposed.
        "stripe_customer_id": row["stripe_customer_id"],
        "subscription_period_end": row["subscription_period_end"],
        "trial_ends_at": row["trial_ends_at"],
        "canceled_at": row["canceled_at"],
        "access_ends_at": row["access_ends_at"],
        "budget_is_default": raw is None,
        "budget_unlimited": budget < 0,
        "budget_usd": None if budget < 0 else round(budget / 1_000_000, 2),
        "spent_usd": round(spent / 1_000_000, 4),
        "raw_spent_usd": round(raw_spent / 1_000_000, 4),
        "subscription_status": row["subscription_status"],
        "subscription_ok": auth.subscription_ok(row),
        "in_trial": auth.in_trial(row),
        "canceled": auth.is_canceled(row),
        "credit_balance_usd": round(
            auth.credit_balance_micros(row["id"]) / 1_000_000, 2
        ),
    }


@admin_router.get("/users")
def admin_list_users(_admin=Depends(auth.require_admin)):
    return {
        "users": [_user_view(u) for u in auth.list_users()],
        "default_budget_usd": round(auth.DEFAULT_MONTHLY_BUDGET_MICROS / 1_000_000, 2),
    }


def _require_existing(email: str):
    user = auth.get_user_by_email(auth._normalize_email(email))
    if not user:
        raise HTTPException(404, f"No such user: {email}")
    return user


@admin_router.post("/approve")
def admin_approve(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    approved = bool(payload.get("approved", True))
    if not auth.set_approved(email, approved):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/admin")
def admin_set_admin(payload: dict = Body(...), admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    make_admin = bool(payload.get("is_admin", False))
    target = _require_existing(email)
    # Guard against locking everyone out: never remove the last admin.
    if not make_admin and target["is_admin"] and auth.count_admins() <= 1:
        raise HTTPException(400, "Refusing to remove the only remaining admin.")
    if not auth.set_admin(email, make_admin):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/budget")
def admin_set_budget(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    mode = payload.get("mode")  # "usd" | "unlimited" | "default"
    if mode == "unlimited":
        micros = -1
    elif mode == "default":
        micros = None
    elif mode == "usd":
        try:
            usd = float(payload.get("usd"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid dollar amount.")
        if usd < 0:
            raise HTTPException(400, "Use 'unlimited' for no cap; usd must be >= 0.")
        micros = int(round(usd * 1_000_000))
    else:
        raise HTTPException(400, "mode must be one of: usd, unlimited, default.")
    if not auth.set_monthly_budget(email, micros):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/delete")
def admin_delete(payload: dict = Body(...), admin=Depends(_admin_guard)):
    email = payload.get("email", "")
    target = _require_existing(email)
    if target["is_admin"] and auth.count_admins() <= 1:
        raise HTTPException(400, "Refusing to delete the only remaining admin.")
    if target["email"] == admin["email"]:
        raise HTTPException(400, "You cannot delete your own account from the panel.")
    if not auth.delete_user(email):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/credits")
def admin_grant_credits(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    """Grant (positive) or claw back (negative) prepaid credits, in dollars.
    This is how testing works before Stripe is configured, and how goodwill
    top-ups work after."""
    email = payload.get("email", "")
    try:
        usd = float(payload.get("usd"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid dollar amount.")
    if usd == 0:
        raise HTTPException(400, "Amount must be non-zero.")
    if abs(usd) > 1000:
        raise HTTPException(400, "Refusing to grant more than $1000 at once.")
    note = str(payload.get("note") or "admin grant")[:200]
    if not auth.grant_credits(email, usd, note):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/comp")
def admin_set_comp(payload: dict = Body(...), _admin=Depends(_admin_guard)):
    """Grant or revoke a complimentary subscription ('comp' status - passes
    the subscription gate without Stripe; credits are still deducted)."""
    email = payload.get("email", "")
    comp = bool(payload.get("comp", True))
    if not auth.set_comp(email, comp):
        raise HTTPException(404, f"No such user: {email}")
    return {"ok": True}


@admin_router.post("/reset-link")
def admin_reset_link(payload: dict = Body(...), _admin=Depends(_admin_guard), request: Request = None):
    """Issue a single-use password reset link the admin can hand to the user.
    The token is never stored in plaintext (only its hash, see auth.py)."""
    email = payload.get("email", "")
    user = _require_existing(email)
    token = auth.create_reset_token(user["id"])
    return {
        "ok": True,
        "reset_url": f"{auth.base_url(request)}/reset?token={quote(token)}",
    }


@admin_router.post("/create")
def admin_create(payload: dict = Body(...), _admin=Depends(_admin_guard), request: Request = None):
    """Create an account from the panel. No password is set here; instead we
    return a single-use reset link for the new user to choose their own. This
    avoids the admin ever handling someone else's password."""
    email = payload.get("email", "")
    try:
        norm = auth._normalize_email(email)
    except Exception:
        raise HTTPException(400, "Invalid email address.")
    if auth.get_user_by_email(norm):
        raise HTTPException(409, "A user with that email already exists.")
    # Random unusable password; the reset link is how they set a real one.
    import secrets as _secrets
    uid = auth.create_user(norm, _secrets.token_urlsafe(24) + "Aa1!")
    if payload.get("approved"):
        auth.set_approved(norm, True)
    if payload.get("is_admin"):
        auth.set_admin(norm, True)
    token = auth.create_reset_token(uid)
    return {
        "ok": True,
        "reset_url": f"{auth.base_url(request)}/reset?token={quote(token)}",
    }


@admin_router.post("/approve-all")
def admin_approve_all(_admin=Depends(_admin_guard)):
    """The web twin of `python admin.py approve --all`: reinstate every
    suspended account in one sweep."""
    n = 0
    for row in auth.list_users():
        if not row["approved"]:
            auth.set_approved(row["email"], True)
            n += 1
    return {"ok": True, "reinstated": n}


# ===================== Command reference =====================
#
# The panel's buttons and its shell-command list are both generated from the
# real argparse parsers, so neither can drift from the CLI. This reaches into
# argparse's private attributes (_actions, _mutually_exclusive_groups); they are
# the only way to introspect a parser and have been stable for many releases.

def _arg_kind(action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "flag"
    if action.type is float:
        return "float"
    if action.type is int:
        return "int"
    return "text"


def _describe(parser: argparse.ArgumentParser) -> list[dict]:
    """One entry per argument, positionals first, help suppressed."""
    exclusive = {}
    for i, group in enumerate(parser._mutually_exclusive_groups):
        for action in group._group_actions:
            exclusive[action.dest] = {"group": i, "required": group.required}
    args = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        default = action.default
        if isinstance(default, Path):
            default = str(default)
        elif default is argparse.SUPPRESS:
            default = None
        positional = not action.option_strings
        args.append({
            "name": action.dest,
            "flag": action.option_strings[0] if action.option_strings else None,
            "positional": positional,
            # A positional with nargs="?" is optional (e.g. `approve [email]`).
            "required": bool(action.required) or (positional and action.nargs != "?"),
            "kind": _arg_kind(action),
            "choices": list(action.choices) if action.choices else None,
            "default": default if isinstance(default, (str, int, float, bool)) else None,
            "help": action.help or "",
            "exclusive": exclusive.get(action.dest),
        })
    return args


def _commands_for(program: str, parser: argparse.ArgumentParser) -> list[dict]:
    """Flatten a parser into command entries. Handles both the subcommand style
    (admin.py) and the single-command style (ingest.py, card_ingest.py)."""
    subparsers = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if not subparsers:
        return [{
            "program": program,
            "name": "",
            "help": (parser.description or "").strip(),
            "args": _describe(parser),
        }]
    out = []
    for name, sub in subparsers[0].choices.items():
        # argparse keeps per-choice help on the subparsers action, not the
        # subparser itself, so pull it from there when present.
        helptext = next(
            (c.help for c in subparsers[0]._choices_actions if c.dest == name), None
        )
        out.append({
            "program": program,
            "name": name,
            "help": (helptext or sub.description or "").strip(),
            "args": _describe(sub),
        })
    return out


# Commands that cannot be driven from the panel, listed so the reference stays
# a complete picture of the CLI even where a button would be wrong.
_EXTRA_COMMANDS = [
    {"command": "pip install -r requirements.txt", "note": "Install backend dependencies."},
    {"command": "npm install && npm run build",
     "note": "Rebuild the frontend into dist/. Required after editing anything in src/."},
    {"command": "uvicorn server:app --port 8000", "note": "Run the app locally."},
    {"command": "uvicorn server:app --host 0.0.0.0 --port 8000 --proxy-headers "
                "--forwarded-allow-ips '*'",
     "note": "Production form, behind a proxy - makes cookies Secure."},
    {"command": "python build_card_cache.py --force",
     "note": "Rebuild cards.db from Scryfall on the command line. Same job as the "
             "Refresh button, but it survives a deploy restart."},
]


@admin_router.get("/commands")
def admin_commands(_admin=Depends(auth.require_admin)):
    """Every CLI command, introspected from the real parsers."""
    return {
        "commands": (
            _commands_for("admin.py", admin_cli.build_parser())
            + _commands_for("ingest.py", ingest.build_parser())
            + _commands_for("card_ingest.py", card_ingest.build_parser())
            + _commands_for("build_card_cache.py", build_card_cache.build_parser())
        ),
        "extra": _EXTRA_COMMANDS,
        # create/delete prompt on the terminal (getpass / input), so the panel
        # uses its own endpoints instead of shelling out. Flagged so the UI can
        # say so next to the listing.
        "interactive": ["create", "delete"],
    }


# ===================== Data pipeline =====================

MAX_RULES_BYTES = 16 * 1024 * 1024      # rules.txt is ~1MB
MAX_CARDS_BYTES = 256 * 1024 * 1024     # the Oracle bulk gz is ~24MB


async def _spool(upload: UploadFile, dest: Path, limit: int) -> int:
    """Stream an upload to disk in 1MB chunks, enforcing a size cap.

    Never read into memory: build_card_cache documents that holding the bulk
    file whole OOM-kills a 512MB instance, and the same applies here.
    """
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413, f"File is larger than {limit // (1024 * 1024)}MB."
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if not size:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file was empty.")
    return size


def _start(kind: str, admin, work):
    try:
        job_id = jobs.start(kind, admin["id"], work)
    except jobs.JobBusy as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "job": jobs.get(job_id)}


@admin_router.post("/upload/rules")
async def admin_upload_rules(file: UploadFile, admin=Depends(_admin_guard)):
    """Parse an uploaded Comprehensive Rules .txt into rules.json + docs.json,
    using the same ingest.py code the CLI runs."""
    tmp = BASE_DIR / f"rules-upload-{int(time.time())}.txt"
    await _spool(file, tmp, MAX_RULES_BYTES)

    def work(progress):
        try:
            progress("parsing", 1, 3)
            chunks, docs = ingest.build_from_text(tmp.read_text(encoding="utf-8"))
            progress("writing", 2, 3)
            ingest.write_outputs(chunks, docs)
            progress("done", 3, 3)
            n = len(docs["rules"])
            return (f"Parsed {n} rules and {len(chunks) - n} glossary entries "
                    f"(effective {docs['effective_date'] or 'unknown'}). "
                    f"Restart the server to load them.")
        finally:
            tmp.unlink(missing_ok=True)

    return _start("rules_ingest", admin, work)


@admin_router.post("/upload/cards")
async def admin_upload_cards(file: UploadFile, admin=Depends(_admin_guard)):
    """Build cards.db from an uploaded Scryfall bulk .jsonl(.gz)."""
    out = Path(build_card_cache.DEFAULT_OUT)
    # Spool beside the output rather than into the system temp dir: on Render
    # that is the persistent disk, and it is the only place with room for a
    # 24MB upload plus the database being written next to it.
    tmp = out.parent / f"cards-upload-{int(time.time())}.jsonl"
    await _spool(file, tmp, MAX_CARDS_BYTES)

    def work(progress):
        try:
            count = card_ingest.build_from_file(tmp, out, progress=progress)
            return (f"Built {out.name} with {count} unique cards. "
                    f"Restart the server to load it.")
        finally:
            tmp.unlink(missing_ok=True)

    return _start("cards_ingest", admin, work)


@admin_router.post("/jobs/cards-refresh")
def admin_cards_refresh(admin=Depends(_admin_guard)):
    """Download the current Scryfall bulk export and rebuild cards.db."""
    out = Path(build_card_cache.DEFAULT_OUT)

    def work(progress):
        count = build_card_cache.build(out, force=True, progress=progress)
        if count is None:
            return "Already up to date with Scryfall; nothing rebuilt."
        return (f"Rebuilt {out.name} with {count} cards from Scryfall. "
                f"Restart the server to load it.")

    return _start("cards_refresh", admin, work)


@admin_router.get("/jobs")
def admin_jobs_list(_admin=Depends(auth.require_admin)):
    return {"jobs": jobs.recent()}


@admin_router.get("/jobs/{job_id}")
def admin_job_get(job_id: int, _admin=Depends(auth.require_admin)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job


@admin_router.get("/status")
def admin_data_status(_admin=Depends(auth.require_admin)):
    """What this worker has loaded versus what is on disk.

    Both the rules index and the card cache are built once at import, so a
    finished rebuild does not take effect until the process restarts - and
    in-process hot-reload would only fix the one worker that ran the job. The
    panel shows a restart banner off this comparison rather than pretending.
    """
    disk_cr = rules._load_cr_effective_date()
    cache = get_cache()
    # _db_stamp returns "" when cards.db is missing or unreadable; treat that as
    # "nothing to compare" rather than as a change.
    disk_cards = build_card_cache._db_stamp(Path(build_card_cache.DEFAULT_OUT))
    stale = (disk_cr != rules.CR_EFFECTIVE_DATE
             or bool(disk_cards and disk_cards != cache.updated_at))
    return {
        "cr": {"in_process": rules.CR_EFFECTIVE_DATE, "on_disk": disk_cr},
        "cards": {
            "in_process": cache.updated_at,
            "on_disk": disk_cards,
            "count": cache.count,
        },
        "restart_required": stale,
    }
