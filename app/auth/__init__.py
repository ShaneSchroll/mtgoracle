"""Accounts, sessions, and money - everything that touches users.db.

`routes.router` is mounted by app/main.py. `require_user` is the baseline gate -
a signed-in, unsuspended account, which is all most of the app asks for.
`require_membership` adds a live subscription on top, and guards only the
Arbiter and the Deck Builder; `require_billing` adds credits to that for the two
endpoints that spend API dollars. `require_admin` gates the panel.

    config        constants and env reading (a leaf - imports no sibling)
    passwords     Argon2 hashing, display-name cleanup
    db            connections, schema, migrations
    limits        DB-backed sliding-window rate limiters
    users         account rows, password changes, the admin switches
    usage         token accounting and monthly spend limits
    entitlements  prepaid credits + subscription state, and the gates on both
    sessions      session cookies, reset tokens, the FastAPI request guards
    store         saved conversations and decks
    routes        the /api/auth endpoints

This package re-exports the whole surface, so every existing `auth.foo(...)`
call site keeps working unchanged. The list below is grouped by owning module
and ordered by dependency - each module imports only from ones above it.
"""

from .config import (  # noqa: F401
    BILLING_REQUIRED, CARD_MAX, CARD_WINDOW, CHAT_MAX, CHAT_WINDOW,
    CREDIT_MARKUP, DB_PATH, DEFAULT_MONTHLY_BUDGET_MICROS,
    LOGIN_IP_MAX_FAILS, LOGIN_MAX_FAILS, LOGIN_WINDOW,
    MAX_CREDIT_BALANCE_MICROS, MAX_CREDIT_BALANCE_USD,
    MAX_MONTHLY_LIMIT_MICROS, MAX_NAME_LEN, MIN_MONTHLY_LIMIT_MICROS,
    PASSWORD_MAX_LEN, PASSWORD_MIN_LEN, PRICING, PWLINK_MAX, PWLINK_WINDOW,
    REGISTER_MAX, REGISTER_WINDOW, RESET_TOKEN_TTL, SESSION_COOKIE,
    SESSION_TTL, SUB_GRACE, TRIAL_DAYS, TRUSTED_PROXY_HOPS, _FALLBACK_RATE,
    _SUB_OK_STATUSES, _env,
)
from .passwords import (  # noqa: F401
    _DUMMY_HASH, _clean_name, _ph,
)
from .db import (  # noqa: F401
    _cleanup_expired, _db, _hash_token, _migrate, _normalize_email, _now,
    _write_tx, init_db,
)
from .limits import (  # noqa: F401
    _MAX_BUCKET_ROWS, _RateLimiter, _card_limit, _chat_limit, _login_fail,
    _login_ip, _pwlink_limit, _register_limit, card_rate_limited,
    chat_rate_limited,
)
from .users import (  # noqa: F401
    _insert_user, count_admins, create_user, delete_user, get_user_by_email,
    get_user_by_id, list_users, set_admin, set_approved, update_name,
    update_password, verify_password,
)
from .usage import (  # noqa: F401
    _budget_for, _budget_from_row, _cost_micros, _next_month_start_iso,
    _utc_month_start_iso, ensure_monthly_limit_default,
    monthly_budget_exceeded, monthly_limit_view, record_usage,
    set_monthly_budget, set_user_monthly_limit, usage_month_credit_micros,
    usage_month_micros, usage_summary_month,
)
from .entitlements import (  # noqa: F401
    _parse_ts, add_credits, billing_blocked, can_resume,
    cancel_subscription, clear_cancellation,
    credit_balance_micros, credit_headroom_micros, credit_history,
    end_access_now, entitlement_end, get_user_by_stripe_customer,
    grant_credits, in_trial, is_canceled, membership_blocked, membership_ok,
    pack_affordable, purchase_blocked, require_billing, require_membership,
    resume_subscription, set_comp, set_stripe_customer, set_subscription,
    subscription_ok,
)
from .sessions import (  # noqa: F401
    _clear_session_cookie, _client_ip, _is_secure, _set_session_cookie,
    base_url, consume_reset_token, create_reset_token, create_session,
    get_current_user, get_user_by_session, require_admin,
    require_same_origin, require_user, revoke_session,
)
from .store import (  # noqa: F401
    DeckSlotsFull, MAX_CONVERSATIONS, MAX_DECKS, _deck_row,
    delete_conversation, delete_deck, get_conversation, get_deck,
    list_conversations, list_decks, save_conversation, save_deck,
)
from .routes import (  # noqa: F401
    LoginReq, NameReq, PasswordLinkReq, RegisterReq, ResetReq, login,
    logout, me, register, reset_password, router, self_service_reset_link,
    set_display_name,
)
