"""The Comprehensive Rules: retrieval index, per-subrule text, and version.

Owns the three objects built once at import - the BM25 retriever, the flattened
subrule index, and the CR effective date - plus the routes that serve them.
"""

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from retriever import Retriever

from . import auth
from .config import DOCS_JSON

router = APIRouter()

retriever = Retriever()


# Exact per-subrule text index for the citation popover. rules.json chunks are
# keyed by parent rule (e.g. "510.1"), with subrules ("510.1c") living inside
# the chunk text - so a citation like 510.1c isn't directly addressable there.
# We flatten every numbered line into {number -> paragraph text} once at startup
# so /api/rule/<id> can return the exact text a citation points at.
_RULE_LINE = re.compile(r"^(\d{3}\.\d+[a-z]?)\.?\s+(.*)$")


def _build_rule_index(chunks) -> dict[str, str]:
    idx: dict[str, str] = {}
    for chunk in chunks:
        current = None
        for raw in chunk["text"].split("\n"):
            line = raw.strip()
            if not line:
                continue
            m = _RULE_LINE.match(line)
            if m:
                current = m.group(1)
                idx[current] = m.group(2).strip()
            elif current:
                # Continuation / "Example:" line belongs to the current subrule.
                idx[current] += " " + line
    return idx


RULE_INDEX = _build_rule_index(retriever.chunks)

def _load_cr_effective_date() -> str | None:
    """The Comprehensive Rules' effective date, captured into docs.json by
    ingest.py. Read once at startup for the CR badge in the chat header; a
    missing/unreadable value is fine — the UI just hides the chip. Refreshes on
    restart, in step with the retriever's rules.json load."""
    try:
        with open(DOCS_JSON, encoding="utf-8") as f:
            date = json.load(f).get("effective_date")
    except (OSError, json.JSONDecodeError):
        return None
    return date if isinstance(date, str) and date.strip() else None


CR_EFFECTIVE_DATE = _load_cr_effective_date()

@router.get("/api/rule/{rule_id}")
def rule_text(rule_id: str, _user=Depends(auth.require_user)):
    """Exact text for a single Comprehensive Rules citation (e.g. 510.1c), so the
    client citation popover/sheet can show the real rule rather than a fallback."""
    text = RULE_INDEX.get(rule_id.strip())
    if not text:
        raise HTTPException(status_code=404, detail=f"No rule {rule_id}.")
    return {"rule": rule_id, "text": text}


@router.get("/docs.json")
def docs_json(_user=Depends(auth.require_user)):
    """Powers the in-app rules docs page. Auth-gated like /api/chat -
    don't ship the cleaned rulebook to anonymous visitors. GZipMiddleware
    above compresses the ~1MB JSON down to ~360KB on the wire."""
    if not DOCS_JSON.exists():
        raise HTTPException(
            status_code=503,
            detail="docs.json is missing. Run `python ingest.py rules.txt` to generate it.",
        )
    return FileResponse(DOCS_JSON, media_type="application/json")


@router.get("/api/cr-version")
def cr_version():
    """Effective date of the loaded Comprehensive Rules, for the CR badge in the
    chat header. Public: it's just non-sensitive version metadata (the date is
    published on WotC's site), so it needn't be auth-gated like the rules text."""
    return {"effective_date": CR_EFFECTIVE_DATE}
