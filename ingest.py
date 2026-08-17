"""
ingest.py - Preprocess the official MTG Comprehensive Rules into search JSON.

Wizards publishes the Comprehensive Rules as a plain-text file: one rule or
subrule per line, blank-line separated. Parsing that is far more reliable than
scraping the old PDF - there are no page headers/footers, no mid-sentence line
wraps, and rule/section boundaries are unambiguous. Grab the .txt from
https://magic.wizards.com/en/rules.

Run once, and again whenever the rules update:

    python ingest.py rules.txt

It writes two files next to this script:
  - rules.json : one chunk per base rule (its subrules and examples folded in),
                 plus one chunk per glossary term. retriever.py BM25-searches it.
  - docs.json  : section/subsection titles + per-rule text, for the in-app
                 rules page (/pages/rules).

UTF-8 throughout: the rules contain characters like the real minus sign
(U+2212) and curly quotes that Windows' default cp1252 codec can't write.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# "1. Game Concepts" - one of the nine top-level sections.
SECTION = re.compile(r"^([1-9])\.\s+(\S.*)$")
# "100. General" - a three-digit subsection heading (no rule number follows).
SUBSECTION = re.compile(r"^(\d{3})\.\s+(\S.*)$")
# "100.1. ..." - a base rule. The period is optional to tolerate the occasional
# source typo (e.g. "606.5 ..."). Subrules ("100.1a ...") and "Example:" lines
# intentionally do NOT match: they fold into the base rule's chunk.
BASE_RULE = re.compile(r"^(\d{3}\.\d+)\.?\s")
# "These rules are effective as of June 19, 2026." - the CR's version stamp,
# in the preamble above the first rule. Captures the date string.
EFFECTIVE_DATE = re.compile(r"effective as of\s+(.+?)\.?\s*$", re.IGNORECASE)


def find_effective_date(preamble: list[str]) -> str | None:
    """The date string from the CR's 'These rules are effective as of ...' line,
    or None if it's absent (e.g. a source file trimmed above the Introduction).
    Only the preamble (before the first rule) is scanned so a rule that happens
    to contain the phrase can't be mistaken for the version stamp."""
    for ln in preamble:
        m = EFFECTIVE_DATE.search(ln.strip())
        if m:
            return m.group(1).strip()
    return None


def split_body_and_glossary(lines: list[str]) -> tuple[int, int]:
    """Find where the rules body and the glossary begin.

    The file opens with an Introduction and a CONTENTS table that repeats every
    section/subsection title (plus the words "Glossary" and "Credits"). We skip
    all of it by anchoring on the first real rule line, then stepping back over
    the headings that introduce it so the first section/subsection titles still
    get captured. The glossary is the last line that is exactly "Glossary".
    """
    first_rule = next(
        (i for i, ln in enumerate(lines) if BASE_RULE.match(ln.strip())), None
    )
    if first_rule is None:
        raise ValueError(
            "No rule lines found - is this the Comprehensive Rules .txt?"
        )

    body_start = first_rule
    j = first_rule - 1
    while j >= 0:
        s = lines[j].strip()
        if SECTION.match(s) or SUBSECTION.match(s):
            body_start = j
        elif s:
            break  # a non-heading, non-blank line: the CONTENTS tail
        j -= 1

    glossary_start = max(
        (i for i, ln in enumerate(lines) if ln.strip() == "Glossary"),
        default=len(lines),
    )
    return body_start, glossary_start


def parse_rules(body: list[str]):
    """Walk the rules body once, returning (chunks, sections, subsections).

    One chunk per base rule, with its subrules and examples folded in. Section
    and subsection headings become titles - never text appended to the previous
    rule. (Appending them was the PDF bug that left a rule chunk ending with the
    next rule's heading.)
    """
    chunks: list[dict] = []
    sections: dict[str, str] = {}
    subsections: dict[str, str] = {}
    current: dict | None = None

    def flush():
        nonlocal current
        if current:
            chunks.append({
                "id": f"rule-{current['rule']}",
                "rule": current["rule"],
                "text": "\n".join(current["lines"]).strip(),
            })
        current = None

    for raw in body:
        line = raw.strip()
        if not line:
            continue
        m_rule = BASE_RULE.match(line)
        if m_rule:
            flush()
            current = {"rule": m_rule.group(1), "lines": [line]}
        elif m_sec := SECTION.match(line):
            flush()
            sections[m_sec.group(1)] = m_sec.group(2).strip()
        elif m_sub := SUBSECTION.match(line):
            flush()
            subsections[m_sub.group(1)] = m_sub.group(2).strip()
        elif current is not None:
            current["lines"].append(line)  # subrule, example, or continuation

    flush()
    return chunks, sections, subsections


def parse_glossary(gloss: list[str]):
    """One chunk per glossary term: the term line plus its definition line(s),
    blank-line separated. Per-term chunks retrieve far better than fixed-size
    blobs for "what does <keyword> mean" questions."""
    chunks: list[dict] = []
    block: list[str] = []

    for raw in [*gloss, ""]:  # trailing "" flushes the final block
        line = raw.strip()
        if line:
            block.append(line)
            continue
        if block:
            slug = re.sub(r"[^a-z0-9]+", "-", block[0].lower()).strip("-")
            chunks.append({
                "id": f"glossary-{slug or len(chunks)}",
                "rule": None,
                "text": "\n".join(block),
            })
            block = []
    return chunks


HERE = Path(__file__).parent
DEFAULT_RULES_OUT = HERE / "rules.json"
DEFAULT_DOCS_OUT = HERE / "docs.json"


def build_from_text(text: str) -> tuple[list[dict], dict]:
    """Parse the Comprehensive Rules text into (rules_chunks, docs).

    The entire parse with no file I/O, so callers that already hold the text -
    notably the admin panel's upload handler - run the exact same code the CLI
    does. `rules_chunks` is the BM25 corpus written to rules.json (base rules
    with their subrules folded in, plus one chunk per glossary term); `docs` is
    the object written to docs.json.
    """
    lines = text.splitlines()
    body_start, glossary_start = split_body_and_glossary(lines)
    effective_date = find_effective_date(lines[:body_start])
    rules, sections, subsections = parse_rules(lines[body_start:glossary_start])
    glossary = parse_glossary(lines[glossary_start + 1:])
    docs = {
        "effective_date": effective_date,
        "sections": sections,
        "subsections": subsections,
        "rules": [{"rule": c["rule"], "text": c["text"]} for c in rules],
    }
    return rules + glossary, docs


def write_outputs(chunks: list[dict], docs: dict,
                  rules_out: Path = DEFAULT_RULES_OUT,
                  docs_out: Path = DEFAULT_DOCS_OUT) -> None:
    """Write both artifacts. UTF-8 explicitly - see the module docstring."""
    rules_out.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=0), encoding="utf-8",
    )
    docs_out.write_text(
        json.dumps(docs, ensure_ascii=False, indent=0), encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser, built without side effects so the admin panel can
    introspect it for its command reference (same reason as admin.build_parser)."""
    p = argparse.ArgumentParser(
        description="Parse the Comprehensive Rules .txt into rules.json + docs.json."
    )
    p.add_argument("src", type=Path, help="path to the Comprehensive Rules .txt")
    p.add_argument("--out-rules", type=Path, default=DEFAULT_RULES_OUT,
                   help="output path for the BM25 corpus")
    p.add_argument("--out-docs", type=Path, default=DEFAULT_DOCS_OUT,
                   help="output path for the in-app rules browser")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.src.exists():
        print(f"File not found: {args.src}", file=sys.stderr)
        return 1

    print(f"Reading {args.src} ...")
    chunks, docs = build_from_text(args.src.read_text(encoding="utf-8"))
    write_outputs(chunks, docs, args.out_rules, args.out_docs)

    n_rules = len(docs["rules"])
    print(
        f"Wrote {args.out_rules} "
        f"({n_rules} rules + {len(chunks) - n_rules} glossary entries)."
    )
    print(
        f"Wrote {args.out_docs} "
        f"({len(docs['sections'])} sections, {len(docs['subsections'])} "
        f"subsections, {n_rules} rules; effective date: "
        f"{docs['effective_date'] or 'not found'})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
