"""Card text lookup and the Deck Builder's name autocomplete."""

import time

from fastapi import APIRouter, Depends, HTTPException

from mtg_api import get_cache, lookup_card

from . import auth

router = APIRouter()


# Longest real card name (an Un-set joke card) is ~141 chars; anything past
# this is garbage and shouldn't reach the cache or the live Scryfall fallback.
MAX_CARD_NAME_CHARS = 200


@router.get("/api/card")
def card_text(name: str, user=Depends(auth.require_user)):
    """Text-only card preview (name, cost, type, Oracle text) for card links in
    answers. Cache-first via mtg_api; no imagery, per the legal constraint."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A card name is required.")
    if len(name) > MAX_CARD_NAME_CHARS:
        raise HTTPException(status_code=400, detail="Card name is too long.")
    if auth.card_rate_limited(user["id"]):
        raise HTTPException(
            status_code=429,
            detail="Too many card lookups. Please wait a moment and try again.",
        )
    hit = lookup_card(name)
    if not hit or hit.get("error"):
        raise HTTPException(status_code=404, detail=f"No card matching '{name}'.")
    return {
        "name": hit.get("name") or name,
        "cost": hit.get("mana_cost") or "",
        "type": hit.get("type_line") or "",
        "text": hit.get("oracle_text") or "",
    }


# ----- Card-name autocomplete (Deck Builder decklist entry) -----------------
#
# Deliberately NOT behind auth._RateLimiter like /api/card: that limiter writes
# a rate_limits row per hit, which costs more than the thing it would guard. A
# suggestion is an indexed prefix scan of the local cards.db (~1ms, see
# CardCache._prefix_range) with no outbound request, so a per-process token
# bucket is the proportionate control — it keeps one signed-in user from
# spinning the CPU without touching disk at all. Per-process means the ceiling
# scales with worker count; that is fine for a read-only local query.
SUGGEST_MIN_CHARS = 2
SUGGEST_MAX_CHARS = 60
SUGGEST_LIMIT = 8
SUGGEST_MAX_LIMIT = 25   # the build overlay's search list wants a longer page

_SUGGEST_RATE = 8.0     # sustained suggestions per second, per user
_SUGGEST_BURST = 24.0   # headroom for a fast typist's first word
_SUGGEST_USERS_MAX = 5_000
_suggest_buckets: dict[int, tuple[float, float]] = {}  # user_id -> (tokens, when)


def _suggest_allowed(user_id: int) -> bool:
    now = time.monotonic()
    tokens, when = _suggest_buckets.get(user_id, (_SUGGEST_BURST, now))
    tokens = min(_SUGGEST_BURST, tokens + (now - when) * _SUGGEST_RATE)
    if tokens < 1.0:
        _suggest_buckets[user_id] = (tokens, now)
        return False
    if len(_suggest_buckets) > _SUGGEST_USERS_MAX:
        # Bounded memory. Dropping the map only forgives in-flight throttling
        # for a moment, and the dict is rebuilt on the next keystroke.
        _suggest_buckets.clear()
    _suggest_buckets[user_id] = (tokens - 1.0, now)
    return True


@router.get("/api/card/suggest")
def card_suggest(q: str = "", detail: int = 0, limit: int = SUGGEST_LIMIT,
                 user=Depends(auth.require_user)):
    """Card names starting with `q`, for the Deck Builder's decklist field.
    Cache-only — a name too new to be cached simply doesn't suggest, rather than
    turning every keystroke into a live Scryfall request.

    `detail=1` adds mana cost and type line (the build overlay's search list);
    `limit` is clamped to SUGGEST_MAX_LIMIT."""
    q = (q or "").strip()[:SUGGEST_MAX_CHARS]
    if len(q) < SUGGEST_MIN_CHARS:
        return {"names": [], "cards": []}
    if not _suggest_allowed(user["id"]):
        raise HTTPException(status_code=429, detail="Too many suggestions.")
    limit = max(1, min(limit, SUGGEST_MAX_LIMIT))
    try:
        cache = get_cache()
        if detail:
            return {"cards": cache.autocomplete_detailed(q, limit)}
        return {"names": cache.autocomplete(q, limit)}
    except FileNotFoundError:
        return {"names": [], "cards": []}
