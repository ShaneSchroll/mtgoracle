"""Shared configuration and paths.

This module is the top of the import graph and calls load_dotenv() as a side
effect of being imported. Every module that reads the environment at import time
must be reachable only through here, so .env is guaranteed to be loaded first.
"""

from pathlib import Path

from dotenv import load_dotenv

# app/config.py -> app/ -> repo root. Paths below are all relative to the repo,
# not to the package, so the data files stay where the CLIs write them.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Frontend: served straight out of Astro's build output (dist/).
#
# `npm run build` emits one folder per page (dist/<route>/index.html), hashed
# JS/CSS into dist/_astro/, and copies public/ assets (favicons) to the dist
# root. app/pages.py points the app's existing URLs at those built files, and
# app/main.py mounts ASTRO_ASSETS. Public URLs are intentionally unchanged -
# only the file each one serves moved.
# ---------------------------------------------------------------------------
DIST_DIR = BASE_DIR / "dist"
ASTRO_ASSETS = DIST_DIR / "_astro"
DOCS_JSON = BASE_DIR / "docs.json"

# The one model every signed-in user gets: Claude Opus 4.8. It is both the sole
# option and the global default, with no per-user gating - everyone can use it.
ALLOWED_MODELS = {"claude-opus-4-8"}
DEFAULT_MODEL = "claude-opus-4-8"

# Per-model extra params merged into each Messages API call. Opus 4.8 runs with
# adaptive thinking (the model decides when and how much to think) at high
# reasoning effort.
MODEL_CALL_PARAMS = {
    "claude-opus-4-8": {
        # Adaptive: think on complex queries, answer instantly on simple ones.
        # display "omitted" hides the thought summary (already the Opus 4.8
        # default) - the model still thinks and is billed for it either way,
        # so this is a visibility setting, not a way to save tokens.
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
    },
}

# Caps on chat input. These bound how large a conversation can get before we
# ask the user to start a new chat - they are NOT model context limits (these
# models have ~1M-token windows). max_tokens (8192) is shared by adaptive
# thinking and the visible answer, but only the visible answer is stored back
# into history - concise rules answers stay well under MAX_MESSAGE_CHARS when
# re-sent; MAX_TOTAL_CHARS allows several turns before the client nudges toward
# a fresh chat (HTTP 422).
MAX_MESSAGES = 15
MAX_MESSAGE_CHARS = 12_000
MAX_TOTAL_CHARS = 48_000
