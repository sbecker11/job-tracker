#!/usr/bin/env python3
"""Build the global skills analysis page from `lead_skills`.

Dry run by default: shows how many skill names still need merging / matching
and the page it would build from the current caches. --apply runs the two
cached LLM steps for new names first.

Usage:
    python scripts/analyze_skills.py              # dry run + build page from caches
    python scripts/analyze_skills.py --apply      # merge + match new names, then build
    python scripts/analyze_skills.py --apply --copy-to ~/Desktop/Resumes/2026/Skills_Analysis.html
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline import db_lock  # noqa: E402
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, _load_candidate_profile  # noqa: E402
from job_tracker.pipeline.models import utc_now_iso  # noqa: E402
from job_tracker.pipeline.skills_analysis import (  # noqa: E402
    DEFAULT_HTML,
    MAP_CACHE,
    _read,
    aggregate,
    load_rows,
    pending_match,
    pending_merge,
    render_html,
    run_match,
    run_merge,
    standard_of,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--apply", action="store_true", help="Run the merge and match LLM steps for new names")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", type=Path, default=DEFAULT_HTML)
    ap.add_argument("--copy-to", type=Path, help="Also copy the page here")
    args = ap.parse_args(argv)

    lock = db_lock.try_acquire(MAP_CACHE.parent / ".analyze.lock") if args.apply else None
    if args.apply and lock is None:
        print("Another analyze_skills run is in progress; skipping.")
        return 0

    conn = connect(args.db)
    rows = load_rows(conn)
    names = {r.canonical for r in rows}
    to_merge = pending_merge(names)
    print(f"{len({r.job_key for r in rows})} leads, {len(names)} skill names; {len(to_merge)} not merged yet")
    spent = 0.0
    if args.apply and to_merge:
        counts = Counter(r.canonical for r in rows)
        spent += run_merge(names, model=args.model, counts=counts)
    cache = _read(MAP_CACHE)
    standards = {standard_of(n, cache) for n in names}
    to_match = pending_match(standards)
    print(f"{len(standards)} standard skills; {len(to_match)} not matched against the profile yet")
    if args.apply and to_match:
        spent += run_match(standards, profile=_load_candidate_profile(), model=args.model)
    if args.apply:
        print(f"LLM spend ~${spent:.2f}")

    agg = aggregate(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(agg, generated=utc_now_iso()[:16].replace("T", " ") + " UTC"), encoding="utf-8")
    print(f"Wrote {args.out}")
    if args.copy_to:
        dest = args.copy_to.expanduser()
        shutil.copyfile(args.out, dest)
        print(f"Copied to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
