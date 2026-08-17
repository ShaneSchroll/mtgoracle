"""Account rows: creation, lookup, password changes, and the admin switches."""

import sqlite3
from datetime import timedelta
from typing import Optional

from argon2.exceptions import InvalidHash, VerifyMismatchError

from .config import PASSWORD_MIN_LEN, TRIAL_DAYS
from .db import _db, _normalize_email, _now
from .passwords import _clean_name, _ph


# ---------- user / password ops ----------

def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _insert_user(email: str, pw_hash: str, name: Optional[str] = None) -> int:
    """Insert a pre-normalized email and pre-computed hash. Caller owns hashing
    so register() can hash unconditionally (constant timing) without this
    function hashing a second time.

    Every new account starts on a TRIAL_DAYS free trial. The trial only opens
    the subscription half of the gate - usage still needs purchased credits -
    so it is a "try it for a week, pay only for what you use" window rather
    than free API spend.

    Accounts are approved on creation: the money gate (subscription + credits)
    is what actually limits usage, so making people wait for a human to let
    them in bought nothing. `approved` survives as the revoke switch - see
    set_approved - so an abusive account can still be shut off and later let
    back in.

    No credits are granted here, and none should be. A new account's balance is
    $0.00 and stays there until money moves: a paid Checkout session
    (billing._credit_paid_session) or a deliberate admin grant (grant_credits)
    are the only two writers of positive credits_ledger rows. Registration is
    open, so a welcome bonus here would be an open invitation to farm accounts
    for free API spend.
    """
    now = _now()
    trial_end = (now + timedelta(days=TRIAL_DAYS)).isoformat()
    with _db() as db:
        cur = db.execute(
            "INSERT INTO users (email, password_hash, name, approved, is_admin, "
            "subscription_status, subscription_period_end, trial_ends_at, created_at) "
            "VALUES (?, ?, ?, 1, 0, 'trialing', ?, ?, ?)",
            (email, pw_hash, name, trial_end, trial_end, now.isoformat()),
        )
        return cur.lastrowid


def create_user(email: str, password: str, name: Optional[str] = None) -> int:
    email = _normalize_email(email)
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    return _insert_user(email, _ph.hash(password), _clean_name(name))


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


def update_name(user_id: int, name: Optional[str]) -> None:
    """Set or clear the optional display name. NULL means "unset", in which
    case the UI falls back to the email's local-part."""
    with _db() as db:
        db.execute(
            "UPDATE users SET name = ? WHERE id = ?", (_clean_name(name), user_id)
        )


def update_password(user_id: int, new_password: str) -> None:
    if len(new_password) < PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LEN} characters.")
    pw_hash = _ph.hash(new_password)
    with _db() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
        # Invalidate every existing session on password change.
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# ---------- admin ops (called by admin.py) ----------

def list_users() -> list[sqlite3.Row]:
    # SELECT * so callers can pass rows straight to subscription_ok() /
    # monthly_limit_view(), which read the trial, cancellation and limit columns.
    with _db() as db:
        return db.execute("SELECT * FROM users ORDER BY created_at").fetchall()


def count_admins() -> int:
    with _db() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]


def set_approved(email: str, approved: bool) -> bool:
    """Suspend (False) or reinstate (True) an account. Signups arrive approved,
    so this is the moderation switch rather than an intake queue - and it moves
    in both directions, so a revoked account can always be let back in."""
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET approved = ? WHERE email = ?",
            (1 if approved else 0, _normalize_email(email)),
        )
        return cur.rowcount > 0


def set_admin(email: str, is_admin: bool) -> bool:
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET is_admin = ? WHERE email = ?",
            (1 if is_admin else 0, _normalize_email(email)),
        )
        return cur.rowcount > 0


def delete_user(email: str) -> bool:
    with _db() as db:
        cur = db.execute("DELETE FROM users WHERE email = ?", (_normalize_email(email),))
        return cur.rowcount > 0
