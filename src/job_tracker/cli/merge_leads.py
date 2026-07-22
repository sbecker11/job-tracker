"""CLI: reconcile two leads flagged by `scripts/find_duplicate_companies.py`
(or `find_duplicate_titles.py`), in one of two ways depending on what's
actually true about the pair — nothing here happens without confirmation.

  1. `--keep`/`--absorb` (MERGE mode): the two leads are genuinely the same
     job posting (same or near-duplicate title too, e.g. a re-posted req
     with a slightly different company spelling) — merges all CRM history
     (contacts, conversations, documents, meetings, offers) from `--absorb`
     into `--keep`, then hard-deletes the absorbed row. See
     `store.merge_leads()`'s docstring for exactly what survives.

  2. `--rename-from`/`--rename-to` (RENAME mode): the two leads are the
     same *company*, but different, genuinely distinct job postings (most
     `find_duplicate_companies.py` hits are actually this — e.g. two
     different open reqs at "Reddit" vs "Reddit, Inc."). Merging these
     would wrongly fold one posting's real history into the other's.
     Instead, this just relabels every lead currently under `--rename-from`
     to `--rename-to`'s spelling, fixing dashboard/folder grouping without
     touching any lead's own identity or CRM history. See
     `store.rename_company()`'s docstring.

For MERGE mode, this computes and shows a JD-text similarity score (plain
difflib.SequenceMatcher ratio over each lead's stored `jd_text`,
whitespace-normalized) as the *default confidence signal* before you
confirm — two leads with matching company/title-ish names but genuinely
different job descriptions are probably NOT the same posting even if a
fuzzy match thought otherwise; conversely a high JD-text match is strong
independent evidence they really are.

Neither mode touches the filesystem — if a lead already has a documents
folder generated under its old company name, move those files by hand
afterward.

Usage:
    merge-leads --keep <normalized_key> --absorb <normalized_key>
    merge-leads --keep <normalized_key> --absorb <normalized_key> --yes
    merge-leads --rename-from "Reddit, Inc." --rename-to "Reddit"
    merge-leads --rename-from "Reddit, Inc." --rename-to "Reddit" --yes
"""

from __future__ import annotations

import argparse
import sys
from difflib import SequenceMatcher
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, merge_leads, rename_company


def jd_text_similarity(a: str, b: str) -> float:
    """Plain whitespace-normalized similarity ratio between two JD bodies —
    deliberately no abbreviation expansion or suffix stripping (compare
    find_duplicate_titles.normalize_title / find_duplicate_companies.
    normalize_company): JD text is prose, not a short label, so those
    normalizations don't apply. Returns 0.0 if either side is empty (an
    empty jd_text tells you nothing about whether two leads are the same
    posting)."""
    na = " ".join((a or "").split())
    nb = " ".join((b or "").split())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _describe(row) -> str:
    return (
        f"{row['title']} @ {row['company']}  "
        f"(status={row['status']}, first_seen={row['first_seen']}, key={row['normalized_key']!r})"
    )


def _run_merge(conn, args, *, input_func) -> int:
    keep_row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (args.keep,)).fetchone()
    absorb_row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (args.absorb,)).fetchone()
    if keep_row is None:
        print(f"--keep key not found in job_leads: {args.keep!r}", file=sys.stderr)
        return 1
    if absorb_row is None:
        print(f"--absorb key not found in job_leads: {args.absorb!r}", file=sys.stderr)
        return 1
    if keep_row["normalized_key"] == absorb_row["normalized_key"]:
        print("--keep and --absorb must refer to two different leads.", file=sys.stderr)
        return 1

    score = jd_text_similarity(keep_row["jd_text"], absorb_row["jd_text"])

    print("KEEP:   " + _describe(keep_row))
    print("ABSORB: " + _describe(absorb_row))
    print(f"\nJD-text similarity: {score:.2f}", end="")
    if not keep_row["jd_text"] or not absorb_row["jd_text"]:
        print("  (one or both leads have no stored jd_text — score is not meaningful)")
    elif score < 0.5:
        print("  \u26a0\ufe0f  low — double-check these are really the same posting, not just the")
        print("  same company with two different open reqs (if so, use --rename-from/--rename-to instead).")
    else:
        print("")

    if not args.yes:
        answer = input_func("\nMerge ABSORB into KEEP? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not merged.")
            return 0

    counts = merge_leads(conn, keep_key=args.keep, absorb_key=args.absorb)
    print(
        f"\nMerged. Moved: contacts={counts['contacts']}, conversations={counts['conversations']}, "
        f"documents={counts['documents']}, meetings={counts['meetings']}, offers={counts['offers']}, "
        f"unmatched_messages={counts['unmatched_messages']}."
    )
    print("Absorbed lead row deleted. Nothing on the filesystem was touched — if the absorbed")
    print("lead had its own documents folder, move/merge those files by hand.")
    return 0


def _run_rename(conn, args, *, input_func) -> int:
    rows = conn.execute(
        "SELECT normalized_key, title, status FROM job_leads WHERE company = ?", (args.rename_from,)
    ).fetchall()
    if not rows:
        print(f"No leads found with company exactly {args.rename_from!r}.", file=sys.stderr)
        return 1

    print(f"Renaming company {args.rename_from!r} -> {args.rename_to!r} for {len(rows)} lead(s):")
    for r in rows:
        print(f"  {r['title']!r:45s} status={r['status']:18s} key={r['normalized_key']!r}")

    if not args.yes:
        answer = input_func("\nProceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not renamed.")
            return 0

    count = rename_company(conn, from_company=args.rename_from, to_company=args.rename_to)
    print(f"\nRenamed {count} lead(s). Nothing on the filesystem was touched — if any of these leads")
    print(f"already have a documents folder under {args.rename_from!r}, move it to {args.rename_to!r} by hand.")
    return 0


def main(argv: list[str] | None = None, *, input_func=input) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--keep", metavar="NORMALIZED_KEY", help="MERGE mode: lead to keep (survives the merge)")
    ap.add_argument("--absorb", metavar="NORMALIZED_KEY", help="MERGE mode: lead to absorb (deleted after merge)")
    ap.add_argument("--rename-from", metavar="COMPANY", help="RENAME mode: exact current company spelling")
    ap.add_argument("--rename-to", metavar="COMPANY", help="RENAME mode: company spelling to rename it to")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args(argv)

    merge_args_given = bool(args.keep or args.absorb)
    rename_args_given = bool(args.rename_from or args.rename_to)

    if merge_args_given and rename_args_given:
        print("Use either --keep/--absorb (merge mode) or --rename-from/--rename-to (rename mode), not both.", file=sys.stderr)
        return 1
    if merge_args_given and not (args.keep and args.absorb):
        print("Merge mode requires both --keep and --absorb.", file=sys.stderr)
        return 1
    if rename_args_given and not (args.rename_from and args.rename_to):
        print("Rename mode requires both --rename-from and --rename-to.", file=sys.stderr)
        return 1
    if not merge_args_given and not rename_args_given:
        print("Specify either --keep/--absorb (merge mode) or --rename-from/--rename-to (rename mode).", file=sys.stderr)
        return 1

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        if merge_args_given:
            return _run_merge(conn, args, input_func=input_func)
        return _run_rename(conn, args, input_func=input_func)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
