"""DB-backed sliding-window rate limiters.

State lives in the ``rate_limits`` table rather than process memory so the
limits hold across uvicorn workers and restarts.
"""

import sqlite3
import time
from datetime import timedelta

from .config import (
    CARD_MAX, CARD_WINDOW, CHAT_MAX, CHAT_WINDOW, LOGIN_IP_MAX_FAILS,
    LOGIN_MAX_FAILS, LOGIN_WINDOW, PWLINK_MAX, PWLINK_WINDOW, REGISTER_MAX,
    REGISTER_WINDOW,
)
from .db import _db, _write_tx


# ---------- rate limiting ----------
# Shared, not process-local: the counters live in users.db, so N uvicorn workers
# enforce ONE limit instead of N of them. They also survive a restart, so a
# redeploy no longer hands an attacker mid-spray a fresh budget.

# Hard ceiling on the rows one bucket may hold at once. Expired rows are pruned
# on every write, so this only bites during a flood of distinct keys from many
# IPs at once; it then drops the closest-to-expiring rows first, which keeps the
# disk bounded at the cost of forgiving the oldest offenders slightly early.
_MAX_BUCKET_ROWS = 50_000


class _RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string, stored in the
    `rate_limits` table: one row per event, holding the moment that event stops
    counting.

    `record` and `hit` run inside `_write_tx`, which is what makes
    count-then-insert atomic between workers. `blocked` is a single SELECT and
    needs no transaction.

    A SQLite error propagates rather than failing open. Anything that reaches a
    limiter has already read users.db to resolve the session, so a database that
    can't be read was going to fail the request either way — and a limiter that
    silently stopped counting is exactly the failure this table exists to
    prevent.
    """

    def __init__(self, bucket: str, max_events: int, window: timedelta) -> None:
        self._bucket = bucket
        self._max = max_events
        self._window = window.total_seconds()

    def _live(self, db: sqlite3.Connection, key: str, now: float) -> int:
        """Events for `key` still inside the window."""
        return db.execute(
            "SELECT COUNT(*) FROM rate_limits "
            "WHERE bucket = ? AND key = ? AND expires_at > ?",
            (self._bucket, key, now),
        ).fetchone()[0]

    def _add(self, db: sqlite3.Connection, key: str, now: float) -> None:
        # Prune the bucket's dead rows on the way in. One delete per insert
        # amortized, so the table settles at roughly the number of events
        # actually inside the window rather than growing with total traffic.
        db.execute(
            "DELETE FROM rate_limits WHERE bucket = ? AND expires_at <= ?",
            (self._bucket, now),
        )
        db.execute(
            "INSERT INTO rate_limits (bucket, key, expires_at) VALUES (?, ?, ?)",
            (self._bucket, key, now + self._window),
        )
        rows = db.execute(
            "SELECT COUNT(*) FROM rate_limits WHERE bucket = ?", (self._bucket,)
        ).fetchone()[0]
        if rows > _MAX_BUCKET_ROWS:
            db.execute(
                "DELETE FROM rate_limits WHERE rowid IN ("
                "  SELECT rowid FROM rate_limits WHERE bucket = ?"
                "  ORDER BY expires_at LIMIT ?)",
                (self._bucket, rows - _MAX_BUCKET_ROWS),
            )

    def blocked(self, key: str) -> bool:
        """True if the key is already at/over the limit (no event recorded)."""
        with _db() as db:
            return self._live(db, key, time.time()) >= self._max

    def record(self, key: str) -> None:
        """Record one event against the key."""
        with _db() as db, _write_tx(db):
            self._add(db, key, time.time())

    def hit(self, key: str) -> bool:
        """Record one event and return True if the key is now over the limit."""
        with _db() as db, _write_tx(db):
            now = time.time()
            over = self._live(db, key, now) + 1 > self._max
            self._add(db, key, now)
            return over

    def clear(self, key: str) -> None:
        with _db() as db:
            db.execute(
                "DELETE FROM rate_limits WHERE bucket = ? AND key = ?",
                (self._bucket, key),
            )


# Bucket names are the stored identity of each limiter: renaming one resets it
# (its old rows simply age out), so keep them stable.
_login_fail = _RateLimiter("login_fail", LOGIN_MAX_FAILS, LOGIN_WINDOW)
_login_ip = _RateLimiter("login_ip", LOGIN_IP_MAX_FAILS, LOGIN_WINDOW)
_register_limit = _RateLimiter("register", REGISTER_MAX, REGISTER_WINDOW)
_chat_limit = _RateLimiter("chat", CHAT_MAX, CHAT_WINDOW)
_card_limit = _RateLimiter("card", CARD_MAX, CARD_WINDOW)
_pwlink_limit = _RateLimiter("pwlink", PWLINK_MAX, PWLINK_WINDOW)


def chat_rate_limited(user_id: int) -> bool:
    """Record a chat request for this user; True if they are now over CHAT_MAX."""
    return _chat_limit.hit(str(user_id))


def card_rate_limited(user_id: int) -> bool:
    """Record a card lookup for this user; True if they are now over CARD_MAX."""
    return _card_limit.hit(str(user_id))
