"""Password hashing and display-name cleanup."""

from typing import Optional

from argon2 import PasswordHasher

from .config import MAX_NAME_LEN


def _clean_name(name: Optional[str]) -> Optional[str]:
    """Normalize an optional display name: trim, cap length, and treat blank as
    unset (stored as NULL so the UI can fall back to the email)."""
    if not name:
        return None
    name = name.strip()[:MAX_NAME_LEN]
    return name or None


_ph = PasswordHasher()
# Used to keep the argon2 verify cost constant when the email doesn't exist,
# so an attacker can't tell registered emails from unregistered ones by timing.
_DUMMY_HASH = _ph.hash("not-a-real-password-only-for-timing-safety")
