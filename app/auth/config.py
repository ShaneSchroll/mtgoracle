"""Configuration for everything that touches users.db.

A leaf module: it imports nothing from the rest of the auth package, so every
sibling can read a constant without risking an import cycle.

Importing ``..config`` here is what fixes a long-standing ordering bug. The old
flat auth.py computed DB_PATH, BILLING_REQUIRED, CREDIT_MARKUP and friends at
import time, but server.py did ``import auth`` BEFORE ``load_dotenv``, so a
value that lived only in .env was silently ignored by the web process. Reading
BASE_DIR from app.config guarantees load_dotenv() has already run.
"""

import os
from datetime import timedelta
from pathlib import Path

from ..config import BASE_DIR


def _env(name: str, default: str) -> str:
    """Environment lookup that treats a blank value as unset.

    os.getenv() only falls back when the key is ABSENT, and a key set to the
    empty string is very easy to produce by accident: `FOO=` in an env file
    (python-dotenv puts it in os.environ as "") and a Render dashboard row with
    the value field left empty both do it. Without this, `CREDIT_MARKUP=` would
    reach float("") and take the whole app down at import with a ValueError
    naming neither the variable nor the file. Blank now means "use the default",
    which is what anyone writing it meant.
    """
    return (os.getenv(name) or "").strip() or default


DB_PATH = Path(_env("AUTH_DB_PATH", str(BASE_DIR / "users.db")))

SESSION_COOKIE = "session"
SESSION_TTL = timedelta(days=30)
RESET_TOKEN_TTL = timedelta(hours=24)

# Failed-login limits. Two layers: per (ip, email) to stop hammering one
# account, and per ip to blunt password-spraying across many accounts.
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILS = 3
LOGIN_IP_MAX_FAILS = 10

# New-account attempts per source IP. Argon2 is deliberately expensive and each
# new account writes a row, so unbounded registration is a CPU + storage DoS.
REGISTER_WINDOW = timedelta(minutes=15)
REGISTER_MAX = 5

# Chat requests per user
CHAT_WINDOW = timedelta(minutes=1)
CHAT_MAX = 5

# Card-preview lookups per user (/api/card). Generous — hovers are cheap and
# cache-first — but bounds how hard one user can drive live Scryfall fallbacks.
CARD_WINDOW = timedelta(minutes=1)
CARD_MAX = 30

# Self-service password-reset links per user. Each call verifies the current
# password and writes a token row, so this bounds both online guessing of that
# password and unbounded token creation.
PWLINK_WINDOW = timedelta(hours=1)
PWLINK_MAX = 5

# Trusted reverse proxies in front of the app, counted from the connection
# inward: Render alone = 1; Cloudflare -> Render = 2. The env var MUST match
# the real chain: too low and per-IP rate limits key on a proxy's (shared) IP;
# too high and clients can spoof their IP via X-Forwarded-For.
TRUSTED_PROXY_HOPS = int(_env("TRUSTED_PROXY_HOPS", "1"))

# ---------- spend accounting ----------
# Monthly spend cap for an account that has never bought credits. Denominated
# in CREDIT dollars (what the balance actually drops by), not raw Anthropic
# cost - see usage_month_credit_micros. Buying credits replaces this with the
# user's own limit, defaulting to their balance (ensure_monthly_limit_default).
DEFAULT_MONTHLY_BUDGET_MICROS = int(
    float(_env("MONTHLY_BUDGET_USD", "5.00")) * 1_000_000
)

PRICING = {
    "claude-opus-4-8": {
        "input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50,
    },
}
# Fallback for an unrecognized model: the most expensive rate in each column,
# so an unknown model can never be under-billed past the monthly budget.
_FALLBACK_RATE = {
    field: max(p[field] for p in PRICING.values())
    for field in ("input", "output", "cache_write", "cache_read")
}

# ---------- billing (subscription + prepaid credits) ----------
# Master switch. When off (the default) the subscription/credits gate is not
# enforced and usage does not deduct credits, so this code can be deployed,
# Stripe configured, and credits granted/tested before anyone is locked out.
# Flip BILLING_REQUIRED=1 only after the SETUP-BILLING.md checklist is done.
BILLING_REQUIRED = _env("BILLING_REQUIRED", "0").lower() in ("1", "true", "yes")

# Multiplier applied to the raw Anthropic cost when deducting credits. Covers
# Stripe's fee (2.9% + $0.30), the retrieval context the user never sees, and
# margin. The usage_ledger keeps recording RAW cost; only the credit deduction
# is marked up, so the markup can be tuned without rewriting history.
CREDIT_MARKUP = float(_env("CREDIT_MARKUP", "1.4"))

# Hard ceiling on the credits one account may hold at once. Credits are
# non-refundable, so this bounds what any single change of heart (or
# chargeback) can be worth. Enforced at checkout - billing.py refuses a pack
# that would overshoot - and surfaced as disabled pack buttons on /account.
MAX_CREDIT_BALANCE_USD = float(_env("MAX_CREDIT_BALANCE_USD", "20"))
MAX_CREDIT_BALANCE_MICROS = int(MAX_CREDIT_BALANCE_USD * 1_000_000)

# Bounds on the monthly limit a user may choose for themselves. The floor keeps
# a stray "0" from silently refusing every request; the ceiling is the most
# credits they could hold anyway, so anything higher is the same as no limit.
MIN_MONTHLY_LIMIT_MICROS = 250_000  # $0.25
MAX_MONTHLY_LIMIT_MICROS = MAX_CREDIT_BALANCE_MICROS

# Subscription statuses that count as "may use the app". 'active'/'trialing'
# come from Stripe; 'comp' is a local, admin-granted status Stripe never sets
# (complimentary access - e.g. the owner and testers).
_SUB_OK_STATUSES = ("active", "trialing", "comp")

# Grace window past the paid-through date, so a briefly-late renewal webhook
# doesn't lock a paying user out the second their period ticks over. Applies
# to PAID subscriptions only - a trial gets exactly its TRIAL_DAYS, and a
# cancellation ends exactly when it says it does.
SUB_GRACE = timedelta(days=3)

# Every new account starts on a free trial of this length, granted at
# registration. It opens the subscription half of the gate only - usage still
# needs purchased credits - so nobody spends API dollars for free, and
# cancelling before it runs out simply means never being charged.
TRIAL_DAYS = int(_env("TRIAL_DAYS", "7"))

PASSWORD_MIN_LEN = 12
# Upper bound applied at the API boundary. Without one, uvicorn accepts
# arbitrarily large bodies and Argon2 would grind through a multi-megabyte
# "password" — free CPU burn for an attacker. 128 chars is far beyond any
# real passphrase.
PASSWORD_MAX_LEN = 128

# Optional display name shown in the sidebar. Kept short; falls back to the
# email when unset. Trimmed and capped so a long value can't bloat the row.
MAX_NAME_LEN = 60
