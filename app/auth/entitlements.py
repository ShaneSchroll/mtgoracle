"""What an account is entitled to: prepaid credits + subscription state.

Credits and subscription live together because the gates read both - a purchase
needs a live subscription, and access needs either a subscription or a trial.
Splitting them would only buy a circular import.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from .config import (
    BILLING_REQUIRED, MAX_CREDIT_BALANCE_MICROS, MAX_CREDIT_BALANCE_USD,
    SUB_GRACE, _SUB_OK_STATUSES,
)
from .db import _db, _normalize_email, _now
from .usage import ensure_monthly_limit_default
from .users import get_user_by_email, get_user_by_id


# ---------- billing: prepaid credits + subscription state ----------

def credit_balance_micros(user_id: int) -> int:
    """The user's prepaid balance: every purchase/grant minus every marked-up
    usage deduction. Can go slightly negative - output tokens aren't known
    until a request finishes, so the request that empties the balance is
    allowed to complete and the next one is refused (same shape as the monthly
    budget check)."""
    with _db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) AS total FROM credits_ledger "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["total"])


def add_credits(
    user_id: int,
    amount_micros: int,
    kind: str,
    stripe_ref: Optional[str] = None,
    note: Optional[str] = None,
) -> bool:
    """Append one credit row. When stripe_ref is set, the UNIQUE constraint
    makes the insert idempotent - Stripe retries webhook deliveries and the
    /api/billing/refresh self-heal re-lists past checkouts, so the same
    purchase may be offered more than once. Returns True if a row was written
    (False = duplicate stripe_ref, already credited)."""
    with _db() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO credits_ledger "
            "(user_id, amount_micros, kind, stripe_ref, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount_micros, kind, stripe_ref, note, _now().isoformat()),
        )
        written = cur.rowcount > 0
    # First money in gets a monthly limit to match. Deliberately after the
    # insert so the balance it snapshots includes this credit, and skipped for
    # a duplicate so a webhook retry can't move a limit the user has since set.
    if written and amount_micros > 0 and kind in ("purchase", "grant"):
        ensure_monthly_limit_default(user_id)
    return written


def credit_headroom_micros(user_id: int) -> int:
    """How much more this user may buy before hitting MAX_CREDIT_BALANCE. A
    balance run negative by a final overshooting request doesn't earn extra
    headroom, hence the clamp at the cap."""
    return max(0, min(
        MAX_CREDIT_BALANCE_MICROS,
        MAX_CREDIT_BALANCE_MICROS - credit_balance_micros(user_id),
    ))


def pack_affordable(user_id: int, cents: int) -> bool:
    """True if buying this pack would leave the balance at or under the cap."""
    return cents * 10_000 <= credit_headroom_micros(user_id)


def grant_credits(email: str, usd: float, note: Optional[str] = None) -> bool:
    """Admin: grant (or claw back, with a negative amount) credits by email."""
    user = get_user_by_email(_normalize_email(email))
    if not user:
        return False
    return add_credits(
        user["id"], int(round(usd * 1_000_000)), "grant", note=note or "admin grant"
    )


def credit_history(user_id: int, limit: int = 10) -> list[dict]:
    """Most recent credit-ledger rows for the account page."""
    with _db() as db:
        rows = db.execute(
            "SELECT amount_micros, kind, note, created_at FROM credits_ledger "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "amount_usd": round(r["amount_micros"] / 1_000_000, 4),
            "kind": r["kind"],
            "note": r["note"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def set_stripe_customer(user_id: int, customer_id: str) -> None:
    """Record the Stripe Customer for a user, first writer wins (a concurrent
    checkout could race to create two customers; keeping the first stored id
    consistent matters more than which one wins)."""
    with _db() as db:
        db.execute(
            "UPDATE users SET stripe_customer_id = ? "
            "WHERE id = ? AND stripe_customer_id IS NULL",
            (customer_id, user_id),
        )


def get_user_by_stripe_customer(customer_id: str) -> Optional[sqlite3.Row]:
    if not customer_id:
        return None
    with _db() as db:
        return db.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()


def set_subscription(
    user_id: int, status: Optional[str], period_end_iso: Optional[str]
) -> None:
    """Mirror Stripe subscription state onto the user row (called from webhook
    handlers and the refresh self-heal). Never lets a Stripe event downgrade a
    'comp' user: comp is admin-granted, so e.g. cancelling an old paid
    subscription must not revoke complimentary access."""
    with _db() as db:
        row = db.execute(
            "SELECT subscription_status FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return
        if row["subscription_status"] == "comp" and status not in ("active", "trialing"):
            return
        db.execute(
            "UPDATE users SET subscription_status = ?, subscription_period_end = ? "
            "WHERE id = ?",
            (status, period_end_iso, user_id),
        )


def set_comp(email: str, comp: bool) -> bool:
    """Admin: grant or revoke complimentary subscription access. Granting also
    clears any cancellation (comp is an override, so it shouldn't leave the
    user locked out by an earlier cancel). Revoking clears the status entirely;
    if the user also has a real Stripe subscription, the next webhook or
    refresh restores it."""
    with _db() as db:
        if comp:
            cur = db.execute(
                "UPDATE users SET subscription_status = 'comp', "
                "subscription_period_end = NULL, canceled_at = NULL, "
                "access_ends_at = NULL WHERE email = ?",
                (_normalize_email(email),),
            )
        else:
            cur = db.execute(
                "UPDATE users SET subscription_status = NULL, "
                "subscription_period_end = NULL WHERE email = ?",
                (_normalize_email(email),),
            )
        return cur.rowcount > 0


def _parse_ts(iso: Optional[str]) -> Optional[datetime]:
    """Parse a stored ISO timestamp into an aware UTC datetime, or None if it
    is missing/unparseable. Naive values are assumed UTC (everything we write
    goes through _now(), which is aware, but hand-edited rows happen)."""
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def in_trial(user_row) -> bool:
    """True while the user is inside their signup trial - the window in which
    cancelling means never being charged for a subscription."""
    end = _parse_ts(user_row["trial_ends_at"])
    return end is not None and _now() <= end


def is_canceled(user_row) -> bool:
    """True once the user has asked to cancel, whether or not access has
    actually lapsed yet (post-trial cancellations keep access until the paid
    period runs out)."""
    return bool(user_row["canceled_at"])


def subscription_ok(user_row) -> bool:
    """True when this user's subscription entitles them to use the app now.

    Order matters: comp overrides everything; a cancellation then decides on
    its own recorded end time (exactly, with no grace - the user keeps what
    they already have and not a day more). Otherwise active/trialing hold
    until the period ends, with SUB_GRACE extended only to paid subscriptions
    so a late renewal webhook can't lock out a paying customer.
    """
    status = user_row["subscription_status"]
    if status == "comp":
        return True
    if is_canceled(user_row):
        end = _parse_ts(user_row["access_ends_at"])
        return end is not None and _now() <= end
    if status not in _SUB_OK_STATUSES:
        return False
    end = _parse_ts(user_row["subscription_period_end"])
    if end is None:
        return True
    if status == "trialing":
        return _now() <= end
    return _now() <= end + SUB_GRACE


def purchase_blocked(user_row) -> Optional[dict]:
    """Why this user may not buy more credits, or None if they may.

    The balance cap is checked first because it applies to everyone, comped
    accounts included - it's about how much money we're willing to take, not
    about entitlement. Past that, buying requires a subscription that is
    currently good AND not cancelled, so the default for an account with no
    live subscription is 'blocked', and someone winding down can't stock up on
    fuel they won't be able to burn.
    """
    if credit_balance_micros(user_row["id"]) >= MAX_CREDIT_BALANCE_MICROS:
        return {
            "code": "credit_limit_reached",
            "message": f"You already have the maximum ${MAX_CREDIT_BALANCE_USD:.0f} "
                       f"in credits. You can buy more once you've used some.",
        }
    if user_row["subscription_status"] == "comp":
        return None
    if is_canceled(user_row):
        return {
            "code": "subscription_canceled",
            "message": "Your subscription is cancelled — resume it to buy credits.",
        }
    if not subscription_ok(user_row):
        return {
            "code": "subscription_required",
            "message": "An active subscription is required to buy credits.",
        }
    return None


def cancel_subscription(user_id: int) -> Optional[dict]:
    """Record a cancellation. Access always runs to the end of whatever the
    user already has - the rest of the trial, or the period they've paid for -
    because credits are non-refundable and cutting access early would strand a
    balance they can no longer spend. Returns a description of what happened,
    or None if the user doesn't exist / was already cancelled."""
    user = get_user_by_id(user_id)
    if user is None or is_canceled(user):
        return None
    now = _now()
    access_end = entitlement_end(user) or now
    if access_end < now:
        access_end = now
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = ?, access_ends_at = ? WHERE id = ?",
            (now.isoformat(), access_end.isoformat(), user_id),
        )
    return {
        "in_trial": in_trial(user),
        "access_ends_at": access_end.isoformat(),
    }


def clear_cancellation(user_id: int) -> None:
    """Unconditionally drop the cancel state. Used when a brand-new
    subscription starts, where resume_subscription's "hasn't lapsed yet" guard
    would otherwise leave a stale cancellation pinning access shut."""
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = NULL, access_ends_at = NULL WHERE id = ?",
            (user_id,),
        )


def end_access_now(user_id: int) -> None:
    """Cut access off immediately (Stripe deleted the subscription outright).
    An existing canceled_at is preserved so a cancellation the user already
    made keeps its original timestamp."""
    now = _now().isoformat()
    with _db() as db:
        db.execute(
            "UPDATE users SET canceled_at = COALESCE(canceled_at, ?), "
            "access_ends_at = ? WHERE id = ?",
            (now, now, user_id),
        )


def entitlement_end(user_row) -> Optional[datetime]:
    """The last moment this user is entitled to access, ignoring any
    cancellation: the later of the trial end and the paid-through date. This
    is what a cancellation freezes access_ends_at to, and what can_resume()
    measures against - a Stripe-side deletion can still pull access_ends_at
    forward (end_access_now), so the two aren't always the same value."""
    ends = [
        ts for ts in (
            _parse_ts(user_row["trial_ends_at"]),
            _parse_ts(user_row["subscription_period_end"]),
        ) if ts is not None
    ]
    return max(ends) if ends else None


def can_resume(user_row) -> bool:
    """True when a cancellation can still be undone, so the UI knows to offer
    Resume rather than a fresh Subscribe."""
    if not is_canceled(user_row):
        return False
    end = entitlement_end(user_row)
    return end is None or _now() <= end


def resume_subscription(user_id: int) -> bool:
    """Undo a cancellation, restoring access and the ability to buy credits.
    Allowed for as long as the user still has time left on the trial or paid
    period they cancelled - including right after an in-trial cancellation,
    which is exactly when someone is most likely to change their mind.
    Returns False when there is nothing left to resume; that needs a fresh
    subscription."""
    user = get_user_by_id(user_id)
    if user is None or not can_resume(user):
        return False
    clear_cancellation(user_id)
    return True


def membership_blocked(user_row) -> Optional[dict]:
    """Why this user may not reach the members-only features, or None if they may.

    Membership is the subscription half of the old billing gate, on its own. It
    guards exactly two things - the Arbiter and the Deck Builder - and nothing
    else: the Rulebook, card lookups, saved rulings and decks, and the Account
    page need only a session, so a lapsed member keeps everything they wrote and
    can still read the rules while they decide whether to come back.

    Credits are deliberately NOT part of this. Running dry is a per-request
    problem with its own message, so an out-of-credits member still opens the
    feature and is told what to top up, rather than being shown a locked door.
    """
    if not BILLING_REQUIRED:
        return None
    if subscription_ok(user_row):
        return None
    if is_canceled(user_row):
        return {
            "code": "subscription_canceled",
            "message": "Your membership is cancelled and access has ended. "
                       "Subscribe again to use the Arbiter and Deck Builder.",
        }
    return {
        "code": "subscription_required",
        "message": "An active membership is required to use the Arbiter and "
                   "Deck Builder.",
    }


def membership_ok(user_row) -> bool:
    """True when this account may reach the Arbiter and the Deck Builder."""
    return membership_blocked(user_row) is None


def require_membership(user_row) -> None:
    """Gate for the members-only endpoints that don't themselves spend API
    dollars (deck validation, CSV import, saving a deck). The dollar-spending
    endpoints use require_billing below, which checks this first.

    402 rather than 403 deliberately: the frontends already branch on a 402's
    {code, message} to point at the Account page, whereas they treat a 403 as a
    dead session and bounce to /login.
    """
    blocked = membership_blocked(user_row)
    if blocked:
        raise HTTPException(status_code=402, detail=blocked)


def billing_blocked(user_row) -> Optional[dict]:
    """Why this user may not spend API dollars right now, or None if they may.
    Membership first, then credits. The failure codes drive distinct UI on the
    account page."""
    if not BILLING_REQUIRED:
        return None
    blocked = membership_blocked(user_row)
    if blocked:
        return blocked
    if credit_balance_micros(user_row["id"]) <= 0:
        return {
            "code": "credits_required",
            "message": "You're out of usage credits.",
        }
    return None


def require_billing(user_row) -> None:
    """Gate for the endpoints that spend API dollars (/api/chat,
    /api/deckbuilder). 402 with a structured detail the frontends turn into a
    'visit your Account page' notice. No-op until BILLING_REQUIRED is set."""
    blocked = billing_blocked(user_row)
    if blocked:
        raise HTTPException(status_code=402, detail=blocked)
