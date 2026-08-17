"""
build_card_cache.py — Download Scryfall's `oracle_cards` bulk file once and
build a local SQLite card cache for instant, offline lookups.

This is the cards-side analogue of ingest.py: a one-shot build step that
produces an artifact (cards.db) the running server loads read-only via
card_cache.CardCache — exactly how Retriever loads rules.json.

Why bulk data instead of looping the API:
  Scryfall explicitly asks you NOT to fetch cards one-by-one for catalog-scale
  work. The `oracle_cards` file is one object per functionally-unique card
  (~30k rows), updated roughly every 12h. Gameplay text changes rarely, so a
  rebuild after a set release is plenty.

Usage:
    python build_card_cache.py                 # build ./cards.db
    python build_card_cache.py --out cards.db  # custom path
    python build_card_cache.py --force         # rebuild even if fresh

This is the whole card refresh, and it runs from a shell — never from the web
process. It downloads ~23MB gzipped and parses ~30k cards, which is minutes of
work with no HTTP request that could sensibly wait on it, and keeping it out
here is what lets the app run more than one uvicorn worker. Restart the server
afterwards: it holds cards.db open, so it keeps serving the old file until it
does. Run it in your deploy/build step (next to `python ingest.py rules.txt`) or
by hand when a set drops. No dependencies beyond httpx, which is already in your
requirements; gzip and sqlite3 ship with Python.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import httpx

SCRYFALL = "https://api.scryfall.com"
BULK_TYPE = "oracle_cards"  # one row per gameplay-unique card
USER_AGENT = "mtg-rules-assistant/1.0"
ACCEPT = "application/json;q=0.9,*/*;q=0.8"  # Scryfall asks clients to send Accept
# cards.db location. Configurable like AUTH_DB_PATH so it can live on a
# persistent disk (e.g. CARD_DB_PATH=/var/data/cards.db) rather than the
# ephemeral code directory; defaults next to this script for local dev.
DEFAULT_OUT = Path(os.getenv("CARD_DB_PATH") or Path(__file__).resolve().parent / "cards.db")

# Layouts with no rules text worth ruling on — skipped so they don't pollute
# fuzzy matches.
SKIP_LAYOUTS = {"token", "double_faced_token", "art_series",
                "emblem", "vanguard", "scheme"}
# Rows per INSERT. Small enough that the in-flight batch is a few MB; large
# enough that we're not paying per-statement overhead 30k times.
BATCH = 2000


def normalize_name(name: str) -> str:
    """Casefold + strip accents/punctuation noise so 'Lim-Dûl's Vault',
    'lim-dul's vault' and 'Lim-Dul’s Vault' all collide on one key.
    Mirrors the kind of forgiving match Scryfall's fuzzy endpoint gives you,
    but computed locally so lookups never touch the network."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))  # drop accents
    n = n.casefold()
    return "".join(c for c in n if c.isalnum() or c == " ").strip()


def project(card: dict) -> dict:
    """Trim a full Scryfall card to the gameplay fields the app needs.

    IMPORTANT: this is the single source of truth for card shape. mtg_api.py
    imports and reuses it so cached cards and live-API cards are byte-for-byte
    identical to Claude — same keys, same join format on double-faced text."""
    def faces(c):
        return c.get("card_faces") if "card_faces" in c else [c]

    return {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "cmc": card.get("cmc"),
        "type_line": card.get("type_line"),
        "oracle_text": "\n//\n".join(f.get("oracle_text", "") for f in faces(card)),
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty"),
        "colors": card.get("colors") or card.get("color_identity"),
        "color_identity": card.get("color_identity", []),
        "keywords": card.get("keywords", []),
        "legalities": {
            fmt: status
            for fmt, status in card.get("legalities", {}).items()
            if status == "legal"
        },
        # `legalities` keeps only the formats a card is legal in, so a banned
        # card and a card simply outside the format's pool both read as "absent"
        # there. The Deck Builder has to tell those apart — "banned in Commander"
        # is a very different message from "not a Commander card" — so the ban
        # list is carried separately. Empty for the overwhelming majority of
        # cards, hence cheap to store.
        "banned_in": sorted(
            fmt
            for fmt, status in card.get("legalities", {}).items()
            if status == "banned"
        ),
        "scryfall_uri": card.get("scryfall_uri"),
    }


def fetch_bulk_download_uri(client: httpx.Client) -> tuple[str, str, int]:
    """Resolve the current oracle_cards download URL, its updated_at stamp, and
    the download size in bytes.

    The download URL's filename changes daily, so we always ask the API for the
    latest one rather than hardcoding it. This is a single lightweight request.
    The size returned is of the file as it comes off the wire, which is what we
    stream to disk — so it's an accurate denominator for download progress.

    Scryfall renamed both fields: `download_uri` (a ~150MB uncompressed JSON
    array) became `jsonl_download_uri` (gzipped JSON Lines, ~23MB), and `size`
    became `compressed_size`. We read the new names and fall back to the old
    ones, so this keeps working whichever they serve.
    """
    r = client.get(f"{SCRYFALL}/bulk-data/{BULK_TYPE}", timeout=30)
    r.raise_for_status()
    meta = r.json()
    uri = meta.get("jsonl_download_uri") or meta.get("download_uri")
    if not uri:
        # A bare KeyError here just names a dict key, which sends you looking in
        # the wrong place. The response was a 200, so the API shape moved again.
        raise RuntimeError(
            f"Scryfall's {BULK_TYPE} response carried no download URL. It "
            f"returned keys {sorted(meta)} — the bulk-data API shape has "
            "changed, so fetch_bulk_download_uri needs updating."
        )
    size = int(meta.get("compressed_size") or meta.get("size") or 0)
    return uri, meta.get("updated_at", ""), size


ProgressFn = "Callable[[str, int, int], None] | None"


def _records(stream):
    """Yield one card dict per non-blank JSONL line."""
    for line in stream:
        line = line.strip()
        if line:
            yield json.loads(line)


@contextmanager
def bulk_stream(path: Path):
    """Open a Scryfall bulk file for streaming, transparently handling gzip.

    Yields ``(records, tell, total)``: an iterator of card dicts, a callable
    returning bytes consumed, and the file's size. Both counters measure the
    *compressed* file, so progress tracks the bytes actually read off disk
    rather than the much larger expansion.

    Shared by both build paths — the live download here and card_ingest.py's
    local file — so a cache is identical whichever produced it. Reading line by
    line is what keeps memory flat: holding the whole file as a string plus the
    full ~30k-object graph blows past a 512MB instance and gets the process
    OOM-killed (SIGKILL, which no except-handler ever sees).
    """
    raw = open(path, "rb")
    try:
        total = os.fstat(raw.fileno()).st_size or 1
        is_gz = raw.read(2) == b"\x1f\x8b"
        raw.seek(0)
        stream = gzip.GzipFile(fileobj=raw) if is_gz else raw
        try:
            yield _records(stream), raw.tell, total
        finally:
            if stream is not raw:
                stream.close()
    finally:
        raw.close()


def build(out: Path, force: bool = False, progress=None) -> int | None:
    """Build cards.db. Returns the number of cards written, or None if the file
    already reflected Scryfall's current bulk version and nothing was done —
    which is what tells the caller whether a server restart is even warranted.

    `progress`, if given, is called as progress(phase, done, total) with byte
    counts during the two long phases: phase is "downloading" then "parsing".
    It's a plain callback — the caller decides how (and how often) to surface
    it. None keeps the function silent apart from its own status lines."""
    def emit(phase, done, total):
        if progress is not None:
            progress(phase, done, total)

    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        download_uri, updated_at, size = fetch_bulk_download_uri(client)
        print(f"[build] latest {BULK_TYPE} updated_at={updated_at or 'unknown'}")

        # Skip work if our db already reflects this bulk version.
        if out.exists() and not force and _db_stamp(out) == updated_at and updated_at:
            print(f"[build] {out.name} already current — nothing to do "
                  f"(use --force to rebuild).")
            return None

        print(f"[build] downloading {download_uri}")
        # Stream straight to disk rather than holding the payload in memory,
        # then parse from the temp file. It arrives gzipped (~23MB) and expands
        # to a few hundred MB, so it is never fully decompressed anywhere.
        tmp = out.with_suffix(".download.jsonl.gz")
        got = 0
        emit("downloading", 0, size)
        with client.stream("GET", download_uri, timeout=None) as resp:
            resp.raise_for_status()
            # Prefer the metadata size; fall back to Content-Length if present.
            total = size or int(resp.headers.get("content-length", 0))
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    emit("downloading", got, total)  # ~150 calls; cheap
        emit("downloading", got, total or got)

    print("[build] parsing + writing SQLite (streaming)…")
    try:
        count = _write_db(out, tmp, updated_at, progress=emit)
    finally:
        tmp.unlink(missing_ok=True)  # always clear the download temp
    print(f"[build] wrote {out} with {count} cards.")
    return count


# Throwaway temp-db pragmas + schema. journal_mode=OFF + synchronous=OFF: this
# db is rebuilt from scratch on any failure, so we don't need crash durability
# here — and it avoids leaving -wal/-shm siblings next to the final file.
_SCHEMA = """
    PRAGMA journal_mode = OFF;
    PRAGMA synchronous = OFF;
    CREATE TABLE cards (
        norm   TEXT NOT NULL,   -- normalized name (lookup key)
        name   TEXT NOT NULL,   -- canonical display name
        data   TEXT NOT NULL    -- JSON: the projected gameplay fields
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def write_db(out: Path, records, updated_at: str, *,
             progress=None, tell=None, total: int = 0) -> int:
    """Write cards.db from an iterable of raw Scryfall card dicts.

    The single writer shared by the live bulk download (see build/_write_db) and
    the local-file ingest (card_ingest.py), so a cache built either way is
    byte-for-byte identical. Filters non-gameplay layouts, projects each card to
    the gameplay fields, and dedups gameplay-identical printings by oracle_id so
    the cache holds one row per gameplay-unique card (the same shape Scryfall's
    oracle_cards bulk already has, and what default/all_cards collapse to).

    Builds into a temp db then atomically swaps, so a running server never reads
    a half-written file. `progress`, if given with `tell` (a callable returning
    bytes consumed) and `total`, is called as progress("parsing", done, total).
    Returns the number of rows written."""
    tmp_db = out.with_suffix(".building.db")
    tmp_db.unlink(missing_ok=True)

    con = sqlite3.connect(tmp_db)
    written = 0
    seen: set[str] = set()  # oracle_id (or name) of cards already written
    batch: list[tuple] = []

    def flush():
        nonlocal written
        if batch:
            con.executemany(
                "INSERT INTO cards (norm, name, data) VALUES (?, ?, ?)", batch)
            written += len(batch)
            batch.clear()

    try:
        con.executescript(_SCHEMA)

        for card in records:
            if card.get("layout") in SKIP_LAYOUTS:
                continue
            name = card.get("name", "")
            # oracle_id is stable across every printing of a gameplay-unique
            # card; fall back to the normalized name for the rare card without
            # one. Skipping seen keys collapses default/all_cards printings.
            key = card.get("oracle_id") or normalize_name(name)
            if not key or key in seen:
                continue
            seen.add(key)

            batch.append((normalize_name(name), name,
                          json.dumps(project(card), ensure_ascii=False)))
            if len(batch) >= BATCH:
                if progress is not None and tell is not None and total:
                    # A free, monotonic proxy for parse progress.
                    progress("parsing", tell(), total)
                flush()
        flush()

        if progress is not None and total:
            progress("parsing", total, total)  # 100%

        # Build the index AFTER the bulk load — far cheaper than maintaining it
        # row-by-row during insert.
        con.execute("CREATE INDEX idx_cards_norm ON cards(norm)")
        con.execute("INSERT INTO meta (key, value) VALUES ('updated_at', ?)", (updated_at,))
        con.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
        con.commit()
    finally:
        con.close()
    tmp_db.replace(out)  # atomic on the same filesystem
    return written


def _write_db(out: Path, src: Path, updated_at: str, progress=None) -> int:
    """Live-download path: stream the gzipped JSONL at `src` into the shared
    writer, exactly as card_ingest.py does with a local bulk file."""
    with bulk_stream(src) as (records, tell, total):
        return write_db(out, records, updated_at,
                        progress=progress, tell=tell, total=total)


def _db_stamp(db: Path) -> str:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='updated_at'").fetchone()
            return row[0] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def _cli_progress():
    """A terminal progress bar, in the shape `build` wants for its `progress`
    callback. This is minutes of silence otherwise — the same reason the old
    admin button had a progress bar, just rendered where the job now runs.

    Returns a closure because it has to remember the current phase: `build`
    prints its own status lines between phases, and they would land on top of an
    unterminated `\\r` bar."""
    state = {"phase": None}

    def emit(phase: str, done: int, total: int) -> None:
        if state["phase"] not in (None, phase):
            print()  # phase changed mid-bar; close the old line first
        state["phase"] = phase
        pct = int(done * 100 / total) if total else 0
        pct = max(0, min(100, pct))
        bar = "#" * (pct // 4)
        print(f"\r[build] {phase}: {pct:3d}%  |{bar:<25}|", end="", flush=True)
        if total and done >= total:
            print()
            state["phase"] = None

    return emit


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser, built without side effects so the admin panel can
    introspect it for its command reference (see admin.build_parser)."""
    ap = argparse.ArgumentParser(description="Build a local Scryfall card cache.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output SQLite path")
    ap.add_argument("--force", action="store_true", help="rebuild even if current")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        count = build(args.out, force=args.force, progress=_cli_progress())
    except httpx.HTTPError as e:
        print(f"[build] network/HTTP error talking to Scryfall: {e}", file=sys.stderr)
        return 1
    if count is not None:
        # A running server opened cards.db before the rename and keeps reading
        # the old file, so the rebuild isn't live until it reopens.
        print("[build] restart the server to serve the new cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
