"""Session cookies, reset tokens, and the FastAPI request guards.

``require_user`` / ``require_admin`` / ``require_same_origin`` are the
dependencies every protected route depends on.
"""

import os
import secrets
import sqlite3
from typing import Optional

from fastapi import HTTPException, Request, Response

from .config import (
    RESET_TOKEN_TTL, SESSION_COOKIE, SESSION_TTL, TRUSTED_PROXY_HOPS,
)
from .db import _db, _hash_token, _now


# ---------- sessions ----------

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _db() as db:
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now.isoformat(), (now + SESSION_TTL).isoformat()),
        )
    return token


def revoke_session(token: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def get_user_by_session(token: str) -> Optional[sqlite3.Row]:
    with _db() as db:
        return db.execute(
            "SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_hash_token(token), _now().isoformat()),
        ).fetchone()


# ---------- reset tokens ----------

def create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _db() as db:
        db.execute(
            "INSERT INTO reset_tokens (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now.isoformat(), (now + RESET_TOKEN_TTL).isoformat()),
        )
    return token


def consume_reset_token(token: str) -> Optional[int]:
    """Return user_id if the token is valid and unused, marking it used. Else None."""
    th = _hash_token(token)
    now_iso = _now().isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT user_id FROM reset_tokens "
            "WHERE token_hash = ? AND expires_at > ? AND used_at IS NULL",
            (th, now_iso),
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE reset_tokens SET used_at = ? WHERE token_hash = ?", (now_iso, th))
        return row["user_id"]


def _client_ip(request: Request) -> str:
    """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

    The leftmost XFF entries are written by the client and cannot be trusted.
    Each proxy *appends* the address it received the connection from, so with
    TRUSTED_PROXY_HOPS proxies in front of us the real client is that many
    entries from the right. If the chain is shorter than configured (anomaly
    or misconfig), fall back to the direct peer, which cannot be spoofed.
    """
    direct = request.client.host if request.client else "0.0.0.0"
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    idx = len(parts) - TRUSTED_PROXY_HOPS
    if 0 <= idx < len(parts):
        return parts[idx]
    return direct


def _is_secure(request: Request) -> bool:
    # Render and Fly terminate TLS at the proxy. Trust X-Forwarded-Proto when
    # present; uvicorn with --proxy-headers will also rewrite request.url.scheme.
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


def base_url(request: Request) -> str:
    """Absolute origin for links and redirects we hand to users (reset links,
    Stripe checkout returns). APP_BASE_URL wins when set; otherwise it is
    derived from the incoming request so links are correct with no config."""
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if base:
        return base
    scheme = "https" if _is_secure(request) else request.url.scheme
    return f"{scheme}://{request.headers.get('host', '')}"


def _set_session_cookie(
    response: Response, token: str, request: Request, remember: bool = True
) -> None:
    # remember -> persistent cookie for SESSION_TTL; otherwise a session cookie
    # (no Max-Age) the browser drops on close. The server-side session row keeps
    # its own SESSION_TTL expiry either way; a session cookie just means the
    # browser stops presenting the token sooner.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()) if remember else None,
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=_is_secure(request),
        samesite="lax",
    )


def get_current_user(request: Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return get_user_by_session(token)


def require_user(request: Request) -> sqlite3.Row:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user["approved"]:
        raise HTTPException(status_code=403, detail="Account suspended")
    return user


def require_admin(request: Request) -> sqlite3.Row:
    """Gate for the admin panel and its API. Authenticated + not suspended +
    admin."""
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_same_origin(request: Request) -> None:
    """Defense-in-depth for high-privilege state-changing admin calls. SameSite
    =Lax already blocks cross-site cookie use; this additionally rejects any
    request whose Origin (when the browser sends one) isn't our own host. A
    missing Origin (common on same-origin GETs) is allowed and left to SameSite."""
    origin = request.headers.get("origin")
    if not origin:
        return
    from urllib.parse import urlparse
    origin_host = urlparse(origin).netloc.lower()
    host = (request.headers.get("host") or "").lower()
    if origin_host and host and origin_host != host:
        raise HTTPException(status_code=403, detail="Cross-origin request refused.")
