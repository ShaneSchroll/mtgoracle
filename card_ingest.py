"""
card_ingest.py — Build cards.db from a local Scryfall bulk card file (JSONL).

The card-side companion to ingest.py (which turns the rules .txt into
rules.json/docs.json): this turns a Scryfall bulk *card* export into cards.db,
the exact artifact build_card_cache.py produces from the live bulk download. Use
it to seed or rebuild the card cache from a file you already downloaded — no
network at all.

Input: one JSON card object per line (JSONL), optionally gzip-compressed. Both
the plain `.jsonl` and the `.jsonl.gz` work; the gz is streamed and decompressed
on the fly, so you never need the (much larger) uncompressed copy on disk. Grab a
bulk file from https://scryfall.com/docs/api/bulk-data.

Prefer the **Oracle Cards** export (~23MB gzipped). It is one row per
gameplay-unique card, which is exactly what this cache stores and exactly what
build_card_cache.py downloads — so both paths yield the same card set. The
broader Default/All Cards exports work too, but they are several times the size
and carry extra printings whose name is the doubled `X // X` form, which dedup
keeps as separate rows.

    python card_ingest.py oracle-card-data.jsonl.gz

Projection, dedup (one row per gameplay-unique card, by oracle_id) and the
on-disk schema are all shared with build_card_cache.py, so a cache built here is
interchangeable with one built from the live download and is read the same way by
card_cache.CardCache. `python build_card_cache.py` is the other half of the pair:
same artifact, but it pulls the bulk file from Scryfall instead of reading yours.

Note: this is a one-shot CLI build step (run it, then start/reload the server),
never called on the request hot path.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import build_card_cache as bcc

DEFAULT_OUT = bcc.DEFAULT_OUT


def _progress(phase: str, done: int, total: int) -> None:
    pct = int(done * 100 / total) if total else 0
    pct = max(0, min(100, pct))
    bar = "#" * (pct // 4)
    print(f"\r[card_ingest] {phase}: {pct:3d}%  |{bar:<25}|", end="", flush=True)


def build_from_file(src: Path, out: Path, progress=None) -> int:
    """Build `out` (cards.db) from the local bulk file `src`. Returns card count.

    `progress` is a callable (phase: str, done: int, total: int) -> None. It
    defaults to the terminal progress bar; the admin panel passes one that
    writes into the admin_jobs table instead, so a browser can poll the job.
    """
    to_terminal = progress is None
    if to_terminal:
        progress = _progress
    # Version the cache by the source file's mtime, prefixed so it never
    # collides with Scryfall's bulk `updated_at` stamp — a later live refresh
    # will always see a different version and can rebuild over this one.
    updated_at = "local-" + time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(src.stat().st_mtime)
    )
    # bcc.bulk_stream reads gzipped-or-plain JSONL and is the same reader the
    # live download uses, so both paths produce an identical cache.
    with bcc.bulk_stream(src) as (records, tell, total):
        count = bcc.write_db(
            out, records, updated_at,
            progress=progress, tell=tell, total=total,
        )
    if to_terminal:
        print()  # terminate the progress line
    return count


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser, built without side effects so the admin panel can
    introspect it for its command reference (see admin.build_parser)."""
    ap = argparse.ArgumentParser(
        description="Build cards.db from a local Scryfall bulk JSONL file."
    )
    ap.add_argument("src", type=Path, help="path to the bulk .jsonl or .jsonl.gz")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output SQLite path")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.src.exists():
        print(f"File not found: {args.src}", file=sys.stderr)
        return 1

    size_mb = args.src.stat().st_size / 1_048_576
    print(f"[card_ingest] reading {args.src} ({size_mb:.0f} MB)")
    count = build_from_file(args.src, args.out)
    print(f"[card_ingest] wrote {args.out} with {count} unique cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
