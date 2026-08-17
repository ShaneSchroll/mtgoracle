"""The /api/auth router: register, login, logout, me, name, reset."""

import sqlite3
from typing import Optional
from urllib.parse import quote

from email_validator import EmailNotValidError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import (
    BILLING_REQUIRED, PASSWORD_MAX_LEN, PASSWORD_MIN_LEN, RESET_TOKEN_TTL,
    SESSION_COOKIE, TRIAL_DAYS,
)
from .db import _normalize_email
from .entitlements import (
    credit_balance_micros, in_trial, is_canceled, membership_ok,
    subscription_ok,
)
from .limits import _login_fail, _login_ip, _pwlink_limit, _register_limit
from .passwords import _DUMMY_HASH, _clean_name, _ph
from .sessions import (
    _clear_session_cookie, _client_ip, _set_session_cookie, base_url,
    consume_reset_token, create_reset_token, create_session,
    require_same_origin, require_user, revoke_session,
)
from .usage import _budget_from_row, usage_month_credit_micros
from .users import (
    _insert_user, get_user_by_email, update_name, update_password,
    verify_password,
)


# ---------- FastAPI integration ----------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterReq(BaseModel):
    email: str
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)
    # Optional display name. Bounded generously here; _clean_name trims and caps
    # it to MAX_NAME_LEN before storage.
    name: Optional[str] = Field(default=None, max_length=200)


class LoginReq(BaseModel):
    email: str
    # Same cap as registration, so no legitimately-set password is rejected
    # here while oversized bodies never reach Argon2.
    password: str = Field(max_length=PASSWORD_MAX_LEN)
    # When false, the session cookie is dropped when the browser closes; when
    # true (the default, matching the "keep me signed in" checkbox), it persists
    # for SESSION_TTL. Defaults true so an omitted field keeps prior behavior.
    remember: bool = True


class ResetReq(BaseModel):
    token: str = Field(max_length=256)
    new_password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN)


class NameReq(BaseModel):
    # Bounded generously here; _clean_name trims to MAX_NAME_LEN and turns a
    # blank value into NULL (display falls back to the email's local-part).
    name: Optional[str] = Field(default=None, max_length=200)


class PasswordLinkReq(BaseModel):
    # The caller's CURRENT password, re-entered to authorize a reset link.
    password: str = Field(max_length=PASSWORD_MAX_LEN)


@router.post("/register", status_code=201)
def register(req: RegisterReq, request: Request):
    ip = _client_ip(request)
    if _register_limit.blocked(ip):
        raise HTTPException(429, "Too many registration attempts. Please wait and try again.")

    try:
        email = _normalize_email(req.email)
    except EmailNotValidError:
        _register_limit.record(ip)
        raise HTTPException(400, "Invalid email address.")
    if len(req.password) < PASSWORD_MIN_LEN:
        raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LEN} characters.")

    _register_limit.record(ip)

    # Always compute one Argon2 hash so response time doesn't reveal whether
    # the email already exists (the hash dominates timing; the existence check
    # and the insert are negligible by comparison). Same response either way.
    pw_hash = _ph.hash(req.password)
    if not get_user_by_email(email):
        try:
            _insert_user(email, pw_hash, _clean_name(req.name))
        except sqlite3.IntegrityError:
            pass  # Lost a race; treat as success.

    return {
        "message": "Account created. You can sign in now — your free trial "
                   f"runs for {TRIAL_DAYS} days."
    }


@router.post("/login")
def login(req: LoginReq, request: Request, response: Response):
    try:
        email = _normalize_email(req.email)
    except EmailNotValidError:
        raise HTTPException(401, "Invalid Credentials. Check your input.")

    ip = _client_ip(request)
    fail_key = f"{ip}\x00{email}"
    if _login_fail.blocked(fail_key) or _login_ip.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Please wait and try again.")

    user = get_user_by_email(email)
    # Verify against a real hash even when the user doesn't exist, so the
    # timing of failed logins doesn't leak registration status.
    pw_ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, req.password)

    if not user or not pw_ok:
        _login_fail.record(fail_key)
        _login_ip.record(ip)
        raise HTTPException(401, "Invalid Credentials. Check your input.")

    if not user["approved"]:
        # Reached only for an account an admin has revoked - registration
        # approves by default. The credentials were correct, so there is
        # nothing to hide by being vague here.
        raise HTTPException(403, "This account has been suspended.")

    # Clear the per-account counter on success. The per-IP counter is left to
    # age out so one success can't reset a spraying attack from the same IP.
    _login_fail.clear(fail_key)
    token = create_session(user["id"])
    _set_session_cookie(response, token, request, remember=req.remember)
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(token)
    _clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/me")
def me(user: sqlite3.Row = Depends(require_user)):
    # Spend is reported in credit dollars, the same units as the monthly limit
    # and the balance, so the header's "$0.40 / $5.00 this month" reads against
    # the number the user actually watches drop rather than raw Anthropic cost.
    spent = usage_month_credit_micros(user["id"])
    budget = _budget_from_row(user)
    unlimited = budget < 0
    return {
        "email": user["email"],
        "name": user["name"],
        "is_admin": bool(user["is_admin"]),
        "spent_micros": spent,
        "budget_micros": budget,
        "unlimited": unlimited,
        "spent_usd": round(spent / 1_000_000, 4),
        "budget_usd": None if unlimited else round(budget / 1_000_000, 2),
        # Billing state for the header + account page. Additive - existing
        # consumers of the fields above are unaffected.
        "billing_required": BILLING_REQUIRED,
        "subscription_status": user["subscription_status"],
        "subscription_ok": subscription_ok(user),
        # May this account open the Arbiter and the Deck Builder? Already folded
        # through BILLING_REQUIRED, so app.js can gate on this one flag instead
        # of re-deriving the rule in the browser.
        "membership_ok": membership_ok(user),
        "in_trial": in_trial(user),
        "canceled": is_canceled(user),
        "credit_balance_usd": round(credit_balance_micros(user["id"]) / 1_000_000, 2),
    }


@router.post("/name")
def set_display_name(
    req: NameReq, request: Request, user: sqlite3.Row = Depends(require_user)
):
    """Set or clear the sidebar display name. The email is deliberately NOT
    editable: it is the account's identity for sign-in, reset links, and the
    Stripe customer, so changing it here would silently split those."""
    require_same_origin(request)
    update_name(user["id"], req.name)
    return {"ok": True, "name": _clean_name(req.name)}


@router.post("/password-reset-link")
def self_service_reset_link(
    req: PasswordLinkReq, request: Request, user: sqlite3.Row = Depends(require_user)
):
    """Issue a single-use reset link for the caller's own account - the
    self-service equivalent of `python admin.py reset <email>`, reusing the
    same token table and /reset page.

    The current password is required: a session cookie alone must not be able
    to change the password, or an unattended browser (or a stolen cookie)
    could lock the real owner out. Spending the link invalidates every
    session, so the user signs in again with the new password.
    """
    require_same_origin(request)
    if _pwlink_limit.hit(str(user["id"])):
        raise HTTPException(429, "Too many attempts. Please wait and try again.")
    if not verify_password(user["password_hash"], req.password):
        raise HTTPException(403, "That password is incorrect.")
    token = create_reset_token(user["id"])
    return {
        "reset_url": f"{base_url(request)}/reset?token={quote(token)}",
        "expires_hours": int(RESET_TOKEN_TTL.total_seconds() // 3600),
    }


@router.post("/reset")
def reset_password(req: ResetReq, request: Request, response: Response):
    user_id = consume_reset_token(req.token)
    if user_id is None:
        raise HTTPException(400, "Invalid or expired reset token.")
    try:
        update_password(user_id, req.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _clear_session_cookie(response, request)
    return {"ok": True, "message": "Password updated. Please sign in again."}
