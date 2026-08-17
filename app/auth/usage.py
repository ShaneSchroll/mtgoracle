"""Token accounting and monthly spend limits.

usage_ledger records RAW Anthropic cost; CREDIT_MARKUP is applied only when
deducting credits, which keeps the markup tunable after the fact.
"""

from datetime import timedelta
from typing import Optional

from .config import (
    BILLING_REQUIRED, CREDIT_MARKUP, DEFAULT_MONTHLY_BUDGET_MICROS,
    MAX_MONTHLY_LIMIT_MICROS, MIN_MONTHLY_LIMIT_MICROS, PRICING, _FALLBACK_RATE,
)
from .db import _db, _normalize_email, _now
from .users import get_user_by_email


# ---------- spend accounting ----------

def _cost_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> int:
    """Cost of one API call in integer micro-dollars. A token priced at $X per
    million tokens costs exactly X micro-dollars, so the rate doubles as the
    per-token micro-dollar price. Unknown models fall back to the priciest
    rates so we never under-bill."""
    r = PRICING.get(model, _FALLBACK_RATE)
    if r is _FALLBACK_RATE or "input" not in r:
        r = _FALLBACK_RATE
    total = (
        input_tokens * r["input"]
        + output_tokens * r["output"]
        + cache_write_tokens * r["cache_write"]
        + cache_read_tokens * r["cache_read"]
    )
    return round(total)


def record_usage(
    user_id: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int:
    """Append one row to the usage ledger. Returns the cost in micro-dollars.
    Costs are locked in at record time from the PRICING then in effect, so the
    ledger is an immutable record even if rates change later."""
    cost = _cost_micros(
        model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens
    )
    now_iso = _now().isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO usage_ledger (user_id, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_micros, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, model, input_tokens, output_tokens,
                cache_write_tokens, cache_read_tokens, cost, now_iso,
            ),
        )
        # Deduct prepaid credits at the marked-up rate. Only while billing is
        # enforced, so pre-launch usage never drives balances negative before
        # anyone has had a chance to buy credits.
        if BILLING_REQUIRED and cost > 0:
            db.execute(
                "INSERT INTO credits_ledger (user_id, amount_micros, kind, note, "
                "created_at) VALUES (?, ?, 'usage', ?, ?)",
                (user_id, -int(round(cost * CREDIT_MARKUP)), model, now_iso),
            )
    return cost


def _utc_month_start_iso() -> str:
    """00:00 UTC on the 1st of the current calendar month - the instant every
    spend window resets. Calendar months, not rolling 30-day windows, so the
    reset date a user is told is a real date they can put in a diary."""
    return _now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def _next_month_start_iso() -> str:
    """00:00 UTC on the 1st of NEXT month - when the current window rolls over.
    A month is long enough that "it resets eventually" isn't good enough: the
    account page shows this date so someone who has hit their cap knows exactly
    how long they're waiting."""
    start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # 32 days past the 1st always lands somewhere inside the following month
    # (months run 28-31 days), so snapping back to day 1 gives its start with
    # no calendar arithmetic and no December wrap-around to special-case.
    return (start + timedelta(days=32)).replace(day=1).isoformat()


def usage_month_micros(user_id: int) -> int:
    """Total spend (micro-dollars) for this user since the 1st of the month."""
    with _db() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(cost_micros), 0) AS total FROM usage_ledger "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, _utc_month_start_iso()),
        ).fetchone()
    return int(row["total"])


def usage_month_credit_micros(user_id: int) -> int:
    """This month's spend in CREDIT dollars: the raw cost marked up exactly the
    way record_usage deducts it. The monthly limit and every user-facing spend
    readout are denominated this way so they line up with the balance the user
    watches drop - a $5 limit means $5 off the balance, not $5 of raw cost
    (which would be $7 of credits). Derived from the raw ledger rather than
    summed from credits_ledger so the cap still works with BILLING_REQUIRED
    off, when nothing is being deducted yet."""
    return int(round(usage_month_micros(user_id) * CREDIT_MARKUP))


def _budget_from_row(row) -> int:
    """Monthly limit (credit micro-dollars) from an already-loaded user row.
    NULL -> the default; negative -> unlimited (a sentinel the callers treat as
    no cap)."""
    if row is None:
        return DEFAULT_MONTHLY_BUDGET_MICROS
    override = row["monthly_budget_micros"]
    return DEFAULT_MONTHLY_BUDGET_MICROS if override is None else int(override)


def _budget_for(user_id: int) -> int:
    """This user's monthly limit in credit micro-dollars."""
    with _db() as db:
        row = db.execute(
            "SELECT monthly_budget_micros FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _budget_from_row(row)


def monthly_budget_exceeded(user_id: int) -> bool:
    """True if the user has already met or passed their monthly limit. Checked
    BEFORE a request starts; since token cost isn't known until generation
    finishes, the request that crosses the line is allowed to complete and the
    next one is refused. Per-request overshoot is bounded by max_tokens and the
    tool-loop cap in server.py."""
    budget = _budget_for(user_id)
    if budget < 0:
        return False  # unlimited
    return usage_month_credit_micros(user_id) >= budget


def set_monthly_budget(email: str, micros: Optional[int]) -> bool:
    """Set a per-user monthly limit override by email. None clears it (back to
    default); a negative value means unlimited. Called by admin.py, which is
    the only path allowed to clear it or lift the cap entirely."""
    with _db() as db:
        cur = db.execute(
            "UPDATE users SET monthly_budget_micros = ? WHERE email = ?",
            (micros, _normalize_email(email)),
        )
        return cur.rowcount > 0


def set_user_monthly_limit(user_id: int, micros: int) -> None:
    """The self-service setter behind the account page. Always stores a
    concrete number inside [MIN_MONTHLY_LIMIT_MICROS, MAX_MONTHLY_LIMIT_MICROS]
    - users can't clear the limit or make it unlimited, only admins can."""
    micros = max(MIN_MONTHLY_LIMIT_MICROS, min(int(micros), MAX_MONTHLY_LIMIT_MICROS))
    with _db() as db:
        db.execute(
            "UPDATE users SET monthly_budget_micros = ? WHERE id = ?",
            (micros, user_id),
        )


def ensure_monthly_limit_default(user_id: int) -> None:
    """Give a user their first monthly limit the moment they first hold
    credits: their whole balance, i.e. "I bought $10, I can spend $10 this
    month".

    Only ever fills a NULL, which is the point - topping up later must NOT
    raise (or reset) a limit the user has since chosen, and an admin override
    or an 'unlimited' setting is likewise left alone.
    """
    with _db() as db:
        row = db.execute(
            "SELECT monthly_budget_micros FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or row["monthly_budget_micros"] is not None:
            return
        balance = int(db.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) AS total FROM credits_ledger "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()["total"])
        if balance <= 0:
            return
        db.execute(
            "UPDATE users SET monthly_budget_micros = ? "
            "WHERE id = ? AND monthly_budget_micros IS NULL",
            (max(MIN_MONTHLY_LIMIT_MICROS, min(balance, MAX_MONTHLY_LIMIT_MICROS)), user_id),
        )


def monthly_limit_view(user_row) -> dict:
    """The monthly-limit block the account page renders and edits. All amounts
    are credit dollars (see usage_month_credit_micros)."""
    budget = _budget_from_row(user_row)
    spent = usage_month_credit_micros(user_row["id"])
    unlimited = budget < 0
    return {
        "usd": None if unlimited else round(budget / 1_000_000, 2),
        "unlimited": unlimited,
        # False while the user is still on the global default - the account
        # page says "we'll set this for you when you buy credits" instead of
        # presenting the number as a choice they made.
        "is_custom": user_row["monthly_budget_micros"] is not None,
        "spent_month_usd": round(spent / 1_000_000, 2),
        "remaining_usd": None if unlimited else round(
            max(0, budget - spent) / 1_000_000, 2
        ),
        "min_usd": round(MIN_MONTHLY_LIMIT_MICROS / 1_000_000, 2),
        "max_usd": round(MAX_MONTHLY_LIMIT_MICROS / 1_000_000, 2),
        "resets_at": _next_month_start_iso(),
    }


def usage_summary_month(email: str) -> Optional[dict]:
    """Per-user view of this month's spend and remaining limit, for admin
    display. Reports both the raw cost (what we pay Anthropic) and the
    credit-dollar figure the limit is measured in."""
    user = get_user_by_email(_normalize_email(email))
    if not user:
        return None
    raw = usage_month_micros(user["id"])
    spent = usage_month_credit_micros(user["id"])
    budget = _budget_from_row(user)
    return {
        "email": user["email"],
        "raw_micros": raw,
        "spent_micros": spent,
        "budget_micros": budget,
        "unlimited": budget < 0,
        "remaining_micros": None if budget < 0 else max(0, budget - spent),
    }
