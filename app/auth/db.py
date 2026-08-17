"""Connections, schema, and migrations for users.db.

Everything in the auth package reaches the database through ``_db()`` and
``_write_tx()`` here; no other module in the app opens users.db directly.
"""

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from email_validator import validate_email

from .config import DB_PATH


# ---------- low-level helpers ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normalize_email(email: str) -> str:
    info = validate_email(email, check_deliverability=False)
    return info.normalized.lower()


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """Wrap a read-then-write so it is atomic against other processes.

    The connection is in autocommit, so each statement would otherwise commit on
    its own and two workers could both read "4 events" before either inserted
    its fifth. BEGIN IMMEDIATE takes SQLite's write lock up front, which
    serializes the whole read-decide-write against every other worker on the
    same file. Waiting for that lock is bounded by sqlite3's busy timeout (5s
    by default) — far beyond anything these millisecond transactions need.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db() -> None:
    with _db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              email         TEXT    NOT NULL UNIQUE,
              password_hash TEXT    NOT NULL,
              -- Registration is open: accounts start usable and `approved` is
              -- a moderation switch (admin.py revoke / approve) rather than a
              -- gate every signup has to wait behind.
              approved      INTEGER NOT NULL DEFAULT 1,
              is_admin      INTEGER NOT NULL DEFAULT 0,
              created_at    TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT    PRIMARY KEY,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT    NOT NULL,
              expires_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS reset_tokens (
              token_hash TEXT    PRIMARY KEY,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT    NOT NULL,
              expires_at TEXT    NOT NULL,
              used_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_ledger (
              id                 INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              model              TEXT    NOT NULL,
              input_tokens       INTEGER NOT NULL DEFAULT 0,
              output_tokens      INTEGER NOT NULL DEFAULT 0,
              cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
              cost_micros        INTEGER NOT NULL DEFAULT 0,
              created_at         TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_user_time
              ON usage_ledger(user_id, created_at);
            CREATE TABLE IF NOT EXISTS credits_ledger (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              amount_micros INTEGER NOT NULL,  -- + purchase/grant, - usage
              kind          TEXT    NOT NULL,  -- purchase | grant | usage
              stripe_ref    TEXT    UNIQUE,    -- checkout session id; dedupes webhook retries
              note          TEXT,
              created_at    TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_credits_user
              ON credits_ledger(user_id, created_at);
            CREATE TABLE IF NOT EXISTS conversations (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title      TEXT    NOT NULL,
              format     TEXT    NOT NULL DEFAULT 'Commander',
              messages   TEXT    NOT NULL,   -- JSON: [{role, content}] display turns
              created_at TEXT    NOT NULL,
              updated_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user
              ON conversations(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS decks (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name         TEXT    NOT NULL,
              format       TEXT    NOT NULL DEFAULT 'Commander',
              commander    TEXT    NOT NULL DEFAULT '',
              cards        TEXT    NOT NULL,   -- the decklist, verbatim text
              goal         TEXT    NOT NULL DEFAULT '',   -- the player's stated intent
              allow_banned INTEGER NOT NULL DEFAULT 0,
              -- JSON: [{role, content}] - the build session travels with the
              -- deck so "iterate on the same deck" resumes the conversation too.
              messages     TEXT    NOT NULL DEFAULT '[]',
              created_at   TEXT    NOT NULL,
              updated_at   TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decks_user
              ON decks(user_id, updated_at);
            -- Long-running admin jobs (rules/card rebuilds). The state lives
            -- here rather than in process memory precisely so it does not pin
            -- the deployment to one uvicorn worker: whichever worker answers
            -- the status poll reads the same row the working thread writes.
            -- See app/jobs.py, which owns every read and write of this table.
            CREATE TABLE IF NOT EXISTS admin_jobs (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              kind       TEXT    NOT NULL,  -- rules_ingest | cards_ingest | cards_refresh
              status     TEXT    NOT NULL,  -- running | done | error
              phase      TEXT    NOT NULL DEFAULT '',
              done       INTEGER NOT NULL DEFAULT 0,
              total      INTEGER NOT NULL DEFAULT 0,
              message    TEXT    NOT NULL DEFAULT '',
              started_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT    NOT NULL,
              updated_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_admin_jobs_kind
              ON admin_jobs(kind, status);
            CREATE TABLE IF NOT EXISTS rate_limits (
              bucket     TEXT NOT NULL,  -- which limiter: 'chat', 'login_ip', …
              key        TEXT NOT NULL,  -- what it counts: user id, IP, ip+email
              -- Unix seconds, not the ISO text the tables above use: these
              -- windows are sub-minute and the limiter is on the request hot
              -- path, so a float that compares and adds directly beats
              -- formatting and parsing a timestamp on every call.
              expires_at REAL NOT NULL   -- when this event leaves its window
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limits
              ON rate_limits(bucket, key, expires_at);
            """
        )
        _migrate(db)
    _cleanup_expired()


def _migrate(db: sqlite3.Connection) -> None:
    """Apply additive schema changes to an already-populated database. Only
    adds columns that are missing, so it is safe to run on every startup and
    never touches existing rows. (CREATE TABLE IF NOT EXISTS won't add a new
    column to a table that already exists, hence this guarded ALTER.)"""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    if "name" not in cols:
        # Optional display name captured at registration. NULL means "unset",
        # in which case the UI falls back to the email's local-part.
        db.execute("ALTER TABLE users ADD COLUMN name TEXT")
    if "monthly_budget_micros" not in cols:
        # The monthly spend limit, in credit dollars. Nullable: NULL means "use
        # DEFAULT_MONTHLY_BUDGET_MICROS" and is the state of an account that
        # has never bought credits. A negative value means unlimited (admin
        # only). Anything else is the limit the user picked, or the balance
        # snapshot ensure_monthly_limit_default wrote at their first purchase.
        db.execute("ALTER TABLE users ADD COLUMN monthly_budget_micros INTEGER")
        if "daily_budget_micros" in cols:
            # Carry the old per-day limits over verbatim. They were seeded from
            # the balance and bounded by the same [MIN, MAX] range as the new
            # column, so every stored value is still a valid monthly figure -
            # just a stricter one, which is the safe direction to land in.
            db.execute(
                "UPDATE users SET monthly_budget_micros = daily_budget_micros"
            )
    # Columns that no longer back anything: the pre-monthly spend limit, and
    # per-user Opus gating (every user gets the same model now). Dropped rather
    # than left dangling so the row can't drift out of sync with the code that
    # reads it. DROP COLUMN needs SQLite 3.35+; on anything older the columns
    # simply stay put, unread and harmless.
    for dead in ("daily_budget_micros", "opus_allowed"):
        if dead in cols:
            try:
                db.execute(f"ALTER TABLE users DROP COLUMN {dead}")
            except sqlite3.OperationalError:
                pass
    # NOTE: `approved` gained a DEFAULT of 1 (registration is open). SQLite
    # can't alter a column default in place, but nothing relies on it - every
    # insert passes `approved` explicitly - so existing databases need no
    # rewrite. Accounts already sitting at approved=0 stay that way; approve
    # them with `python admin.py approve <email>` (or `approve --all`).
    if "stripe_customer_id" not in cols:
        # The Stripe Customer this user maps to. Created lazily on their first
        # checkout; webhooks resolve users through it. Uniqueness is enforced
        # by an index (ALTER ADD COLUMN can't carry a UNIQUE constraint).
        db.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer "
            "ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
        )
    if "subscription_status" not in cols:
        # Mirror of the Stripe subscription status ('active', 'trialing',
        # 'past_due', 'canceled', ...) or the local 'comp'. NULL = never
        # subscribed. Kept current by webhooks + /api/billing/refresh.
        db.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT")
    if "subscription_period_end" not in cols:
        # ISO timestamp the subscription is paid through. The gate allows
        # SUB_GRACE past it so a late renewal webhook doesn't lock users out.
        db.execute("ALTER TABLE users ADD COLUMN subscription_period_end TEXT")
    if "trial_ends_at" not in cols:
        # End of the free trial granted at registration. NULL on accounts that
        # predate trials - they simply never had one, and fall through to the
        # normal subscription rules.
        db.execute("ALTER TABLE users ADD COLUMN trial_ends_at TEXT")
    if "canceled_at" not in cols:
        # When the user asked to cancel. NULL = not cancelled. Set locally even
        # when there is no Stripe subscription (e.g. cancelling a free trial).
        db.execute("ALTER TABLE users ADD COLUMN canceled_at TEXT")
    if "access_ends_at" not in cols:
        # When a cancellation actually cuts access off: the end of whatever the
        # user already has - the rest of the trial, or the period they've paid
        # for. Credits are non-refundable, so access is never cut early.
        db.execute("ALTER TABLE users ADD COLUMN access_ends_at TEXT")


def _cleanup_expired() -> None:
    """Delete rows that can no longer authenticate or count against anything, so
    sessions, reset_tokens and rate_limits don't grow without bound on the
    persistent disk."""
    now_iso = _now().isoformat()
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        db.execute(
            "DELETE FROM reset_tokens WHERE expires_at <= ? OR used_at IS NOT NULL",
            (now_iso,),
        )
        # Every rate_limits row carries its own expiry, so one statement sweeps
        # every bucket — including buckets no limiter uses any more. Live
        # buckets also prune themselves on write; this catches the ones that
        # went quiet, and whatever went stale while the app was down.
        db.execute("DELETE FROM rate_limits WHERE expires_at <= ?", (time.time(),))
