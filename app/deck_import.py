"""Turn a collection-tracker CSV export into a decklist.

ManaBox, Moxfield, Archidekt and Deckbox all export "one row per card" CSVs that
differ only in their column names, so the parser sniffs headers rather than
hardcoding one app's layout. Everything except the card name and the quantity is
discarded: set, printing, foil, condition, language, price, rarity and timestamps
say nothing the Deck Builder can use.

Notably that includes the Scryfall ID. cards.db is built from Scryfall's *Oracle
Cards* export, which keeps one representative printing per gameplay-unique card,
while a tracker exports the exact printing you own — measured against a real
ManaBox export, 29% of rows named a different printing than the cached one, so
matching on that ID would lose about a third of the collection. Names match all
of it, and every field the builder reads (mana cost, type line, legality, ban
list) is identical across printings anyway.

The CSV itself is never written to disk or to the database: the route parses the
upload in memory and returns a decklist, which is only persisted if the user then
saves the deck.
"""

from __future__ import annotations

import csv
import io

from build_card_cache import normalize_name

# Header aliases, most-specific first. Moxfield exports both "Count" and
# "Tradelist Count"; "count" must win, and neither may be matched by a loose
# substring test, hence exact comparison against a lowercased header.
NAME_HEADERS = ("name", "card name", "card", "cardname")
QUANTITY_HEADERS = ("quantity", "count", "qty", "amount", "number")

MAX_COUNT = 99          # DeckCard.count ceiling
MAX_ROWS = 20_000       # refuse absurd files before doing per-row work


class CsvImportError(ValueError):
    """A CSV we cannot turn into a decklist. The message is shown to the user."""


def _pick(fieldnames, aliases: tuple[str, ...]) -> str | None:
    lookup = {}
    for raw in fieldnames or ():
        key = (raw or "").strip().lower()
        lookup.setdefault(key, raw)      # first wins on duplicate headers
    for alias in aliases:
        if alias in lookup:
            return lookup[alias]
    return None


def _count(raw: str | None) -> int:
    """Parse a quantity cell. Blank, junk or <1 all mean a single copy."""
    text = (raw or "").strip()
    if not text:
        return 1
    try:
        n = int(float(text))            # tolerate "2", "2.0", " 2 "
    except ValueError:
        return 1
    return max(1, min(MAX_COUNT, n))


def parse_csv(text: str, *, max_entries: int) -> dict:
    """Parse a tracker CSV into a decklist.

    Returns a dict with the decklist text, per-card entries, and counts the UI
    reports back to the user. Raises CsvImportError with a message meant to be
    shown verbatim.
    """
    # utf-8-sig is handled by the caller; a stray BOM here would corrupt the
    # first header name and break column sniffing.
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("That file has no header row, so we can't tell which column holds the card name.")

    name_col = _pick(reader.fieldnames, NAME_HEADERS)
    if not name_col:
        found = ", ".join(f for f in reader.fieldnames if f) or "(none)"
        raise CsvImportError(
            "No card-name column found. Expected one called Name, Card Name or "
            f"Card. This file has: {found}."
        )
    qty_col = _pick(reader.fieldnames, QUANTITY_HEADERS)

    entries: dict[str, dict] = {}        # merge key -> {name, count}
    order: list[str] = []
    blank_rows = merged = 0

    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            raise CsvImportError(
                f"That file has more than {MAX_ROWS:,} rows. Export a single deck "
                "rather than a whole collection."
            )
        name = (row.get(name_col) or "").strip()
        if not name:
            blank_rows += 1
            continue
        # Merge printings of the same card: a tracker lists each printing you
        # own separately, but a decklist wants "2 Rite of Replication".
        key = normalize_name(name) or name.casefold()
        count = _count(row.get(qty_col) if qty_col else None)
        if key in entries:
            entries[key]["count"] = min(MAX_COUNT, entries[key]["count"] + count)
            merged += 1
        else:
            entries[key] = {"name": name, "count": count}
            order.append(key)

    if not order:
        raise CsvImportError("No card rows found in that file.")

    if len(order) > max_entries:
        raise CsvImportError(
            f"That file has {len(order):,} different cards, and a deck holds "
            f"{max_entries}. Trim the export down to {max_entries} cards and try "
            "again."
        )

    cards = [entries[k] for k in order]
    return {
        "cards": cards,
        "text": "\n".join(
            (f"{c['count']} {c['name']}" if c["count"] > 1 else c["name"])
            for c in cards
        ),
        "entries": len(cards),
        "copies": sum(c["count"] for c in cards),
        "merged": merged,
        "blank_rows": blank_rows,
        "columns": {"name": name_col, "quantity": qty_col},
    }
