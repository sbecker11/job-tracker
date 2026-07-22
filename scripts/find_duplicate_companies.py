#!/usr/bin/env python3
"""CLI: detect likely-duplicate companies (e.g. "Scribd" vs "Scribd, Inc.",
or "Cox Communications" vs "Cox Comm") for manual review.

Detection-only, by design — this is the *other* half of the 2026-07-21
company-name cleanup. `store.canonicalize_company_casing()` already handles
the safe, automatic case: two spellings that fold to the exact same
normalized_key-company prefix (a pure casing/punctuation difference, e.g.
NICE/NiCE) get silently reconciled at ingestion time, with zero risk, since
the normalized_key — and therefore which lead a company's contacts,
conversations, and documents live under — never changes.

This script covers the *harder*, riskier case that deliberately does NOT
get auto-resolved: two company spellings that do NOT fold to the same key
(a corporate-suffix difference like ", Inc." / ", LLC", or a genuine
abbreviation like "Communications" -> "Comm") but plausibly refer to the
same real company. Auto-merging these would mean guessing which of two
already-distinct normalized_key rows (each with its own real history —
contacts, conversations, documents on disk) is "the same" as the other,
and picking wrong silently discards history. So, like
find_duplicate_titles.py, this only ever reports candidate pairs for a
human to look at; nothing is written to the DB.

IMPORTANT — most hits here are NOT the same job posting, just the same
company with two different real, distinct open reqs (different titles) —
merging those would wrongly fold one posting's own CRM history into the
other's. Reconcile with `merge-leads` (`cli/merge_leads.py`) once you've
looked at a pair: use its `--rename-from`/`--rename-to` mode (just relabels
the company, keeps both postings distinct) unless the titles are also
near-duplicates, in which case `--keep`/`--absorb` (a real merge) is
appropriate instead.

Approach ("discounting extra terms" — the open question from 2026-07-21):
  1. Strip a whitelist of common corporate suffix words (Inc, LLC, Corp,
     Corporation, Ltd, Co, Company, Group, Holdings, PLC, LP, LLP — see
     _SUFFIXES) from the *end* of each company name, then lowercase and
     strip remaining punctuation. This handles "Scribd, Inc." -> "scribd"
     matching "Scribd" -> "scribd" exactly.
  2. For names that don't collapse to an identical suffix-stripped form,
     fall back to difflib.SequenceMatcher on that same stripped/lowercased
     text — catches minor spelling/abbreviation variants (e.g.
     "Communications" vs "Comm") without requiring an exact match.
  3. Report any pair scoring >= --threshold (default 0.90 — deliberately
     higher than find_duplicate_titles.py's 0.85, since a false-positive
     company merge is more damaging than a false-positive title merge:
     titles at the same company share a contacts/conversation history by
     construction, but two different companies never should).

Usage:
    python scripts/find_duplicate_companies.py [--db PATH] [--threshold 0.90]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402

# Common corporate-suffix words, stripped only from the *end* of a company
# name (word-by-word, so "Corp" doesn't wrongly strip out of "Corpstart").
# Not exhaustive — just what's turned up in this corpus so far.
_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "llp",
    "lp",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "group",
    "holdings",
    "gmbh",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_company(company: str) -> str:
    """Lowercase, strip punctuation to word boundaries, then strip any
    trailing run of corporate-suffix words. Deliberately lossy — this
    exists only to detect likely duplicates, never written back anywhere
    (compare `store.fold_for_key`, which is punctuation-only and IS safe to
    write back, because it never changes which real company a name maps
    to)."""
    words = _WORD_RE.findall((company or "").lower())
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def company_similarity(a: str, b: str) -> float:
    na, nb = normalize_company(a), normalize_company(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class DuplicateCompanyPair:
    a_company: str
    b_company: str
    a_leads: list[dict]
    b_leads: list[dict]
    score: float


def find_duplicate_company_pairs(conn, *, threshold: float = 0.90) -> list[DuplicateCompanyPair]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT normalized_key, company, title, status, first_seen FROM job_leads"
        )
    ]
    by_company: defaultdict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_company[r["company"]].append(r)

    companies = sorted(by_company.keys())
    pairs: list[DuplicateCompanyPair] = []
    for i in range(len(companies)):
        for j in range(i + 1, len(companies)):
            a_company, b_company = companies[i], companies[j]
            score = company_similarity(a_company, b_company)
            if score >= threshold:
                pairs.append(
                    DuplicateCompanyPair(
                        a_company, b_company, by_company[a_company], by_company[b_company], score
                    )
                )
    pairs.sort(key=lambda p: -p.score)
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--threshold", type=float, default=0.90, help="Minimum suffix-stripped similarity to report (0-1)"
    )
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        pairs = find_duplicate_company_pairs(conn, threshold=args.threshold)
        if not pairs:
            print(f"No likely-duplicate companies found at threshold={args.threshold}.")
            return 0

        print(f"{len(pairs)} likely-duplicate company pair(s) (threshold={args.threshold}):\n")
        for p in pairs:
            print(f"{p.a_company!r} <-> {p.b_company!r}  (similarity={p.score:.2f})")
            for label, leads in (("A", p.a_leads), ("B", p.b_leads)):
                for lead in leads:
                    print(
                        f"  {label}: {lead['title']!r:45s} status={lead['status']:18s} "
                        f"first_seen={lead['first_seen']}  key={lead['normalized_key']!r}"
                    )
            print()
        print("Nothing was changed. If a pair is the same company but different postings (the common")
        print("case), relabel one to match the other:")
        print("    merge-leads --rename-from '<company>' --rename-to '<company>'")
        print("If a pair really is the SAME posting (titles match too), merge it instead:")
        print("    merge-leads --keep <key-to-keep> --absorb <key-to-absorb>")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
