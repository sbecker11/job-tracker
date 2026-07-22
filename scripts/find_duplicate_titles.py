#!/usr/bin/env python3
"""CLI: detect likely-duplicate job titles within the same company (e.g.
"Senior Software Engineer" vs "Snr Software Engineer") for manual review.

Detection-only, by design — unlike the NICE/NiCE-style company-casing fix
(a pure normalization, safe to auto-merge), title near-duplicates can't be
safely auto-merged: two genuinely different postings can share an
abbreviation ("Sr Backend Engineer" and "Sr Frontend Engineer" both start
with "Sr"), and merging the wrong pair silently discards one lead's real
history (its contacts/conversations/documents, keyed by that lead's own
normalized_key). So this only reports candidate pairs; nothing is ever
written to the DB. Merge manually (pick the row with more real history —
further-along status, documents, contacts) via direct SQL or a future
merge tool.

Approach:
  1. Group leads by exact company match (job_leads.company — case
     inconsistencies should already be fixed; see the 2026-07-21
     NiCE/NICE cleanup).
  2. Within each company, expand common title abbreviations (Snr/Sr ->
     Senior, Jr -> Junior, Eng/Engr -> Engineer, Mgr -> Manager, Dev ->
     Developer, SWE -> Software Engineer, roman numerals <-> digits,
     etc. — see _EXPANSIONS) then lowercase/strip punctuation.
  3. Pairwise-compare every two titles at that company with
     difflib.SequenceMatcher on the *expanded* form — catches
     "Snr Software Engineer" vs "Senior Software Engineer" even though
     the raw strings differ enough to miss store.find_similar_jobs'
     0.75 ambiguous-match threshold (their approach compares raw text,
     not abbreviation-expanded text).
  4. Report any pair scoring >= --threshold (default 0.85) for you to
     look at and decide whether to merge.

Usage:
    python scripts/find_duplicate_titles.py [--db PATH] [--threshold 0.85]
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

# Whole-word abbreviation -> canonical expansion, applied case-insensitively.
# Deliberately not exhaustive — just the recruiting-title abbreviations
# actually seen recurring in this corpus. Add more here as new false
# negatives (missed dupes) turn up.
_EXPANSIONS = {
    "snr": "senior",
    "sr": "senior",
    "jr": "junior",
    "eng": "engineer",
    "engr": "engineer",
    "mgr": "manager",
    "dev": "developer",
    "swe": "software engineer",
    "sde": "software development engineer",
    "ml": "machine learning",
    "fe": "frontend",
    "be": "backend",
    "fs": "full stack",
    "fullstack": "full stack",
    "qa": "quality assurance",
    "devops": "development operations",
    "pm": "product manager",
    "tpm": "technical program manager",
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation to word boundaries, expand common
    recruiting-title abbreviations word-by-word. Deliberately lossy — this
    exists only to detect likely duplicates, never written back anywhere."""
    words = _WORD_RE.findall((title or "").lower())
    expanded = [_EXPANSIONS.get(w, w) for w in words]
    return " ".join(expanded)


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class DuplicatePair:
    company: str
    a: dict
    b: dict
    score: float


def find_duplicate_title_pairs(conn, *, threshold: float = 0.85) -> list[DuplicatePair]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT normalized_key, company, title, status, first_seen FROM job_leads"
        )
    ]
    by_company: defaultdict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_company[r["company"]].append(r)

    pairs: list[DuplicatePair] = []
    for company, leads in by_company.items():
        if len(leads) < 2:
            continue
        for i in range(len(leads)):
            for j in range(i + 1, len(leads)):
                a, b = leads[i], leads[j]
                score = title_similarity(a["title"], b["title"])
                if score >= threshold:
                    pairs.append(DuplicatePair(company, a, b, score))
    pairs.sort(key=lambda p: -p.score)
    return pairs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--threshold", type=float, default=0.85, help="Minimum abbreviation-expanded similarity to report (0-1)"
    )
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        pairs = find_duplicate_title_pairs(conn, threshold=args.threshold)
        if not pairs:
            print(f"No likely-duplicate titles found at threshold={args.threshold}.")
            return 0

        print(f"{len(pairs)} likely-duplicate title pair(s) (threshold={args.threshold}):\n")
        for p in pairs:
            print(f"{p.company!r}  (similarity={p.score:.2f})")
            print(
                f"  A: {p.a['title']!r:55s} status={p.a['status']:18s} "
                f"first_seen={p.a['first_seen']}  key={p.a['normalized_key']!r}"
            )
            print(
                f"  B: {p.b['title']!r:55s} status={p.b['status']:18s} "
                f"first_seen={p.b['first_seen']}  key={p.b['normalized_key']!r}"
            )
            print()
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
