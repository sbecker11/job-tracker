#!/usr/bin/env python3
"""Backfill `JobDescription.docx` + `no-LLM-review.docx` for every lead that
has stored `jd_text` but is missing either file on disk.

Zero LLM cost — `no-LLM-review.docx` is the deterministic rule-based tier
(`scoring.scorer.score_jd`), and `JobDescription.docx` just re-renders the
already-stored JD text. Never touches `full-LLM-review.docx`, résumé, or
cover-letter files, and never regenerates files that already exist.

Enforces the standing invariant (2026-07-12): every lead with a recoverable
JD should have a JobDescription.docx + no-LLM-review.docx, regardless of
status. Leads with no/thin stored jd_text (<200 chars) are skipped and
reported — those need the JD manually re-attached before this can help.

Usage:
    python scripts/backfill_jd_and_no_llm_review.py [--dry-run] [--db PATH] [--output-root PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    _safe_filename,
    render_job_description,
    render_no_llm_review_docx,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, get_sibling_titles  # noqa: E402
from job_tracker.scoring.scorer import score_jd  # noqa: E402

MIN_JD_LEN = 200


def _lead_folder(out_dir: Path, *, company: str, title: str, multi_lead: bool) -> Path:
    """Read-only mirror of `llm_apply._job_folder`'s path logic, scoped to
    THIS lead specifically rather than the whole company folder.

    Deliberately does NOT call `_job_folder` itself for existence checks:
    that function creates directories and auto-migrates flat files into a
    subfolder as a side effect, which we don't want to trigger just to look.
    Bug fixed 2026-07-12: the previous version checked
    `company_folder.rglob(...)` for existence, which for a multi-lead
    company found a *sibling* lead's file and incorrectly treated the
    current lead as already complete, silently skipping it."""
    company_dir = out_dir / _safe_filename(company)
    if not multi_lead:
        return company_dir
    return company_dir / _safe_filename(f"{company}_{title}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--dry-run", action="store_true", help="Report what would be written, write nothing")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT normalized_key, company, title, status, jd_text FROM job_leads").fetchall()

    no_jd_text: list[tuple[str, str, str]] = []
    wrote_jd = 0
    wrote_review = 0
    already_complete = 0

    for r in rows:
        jd_text = r["jd_text"] or ""
        if len(jd_text) < MIN_JD_LEN:
            no_jd_text.append((r["company"], r["title"], r["status"]))
            continue

        company, title = r["company"], r["title"]
        sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))
        multi_lead = len(sibling_titles) > 0

        folder = _lead_folder(args.output_root, company=company, title=title, multi_lead=multi_lead)
        has_jd_doc = (folder / "JobDescription.docx").is_file()
        has_review_doc = (folder / "no-LLM-review.docx").is_file()

        if has_jd_doc and has_review_doc:
            already_complete += 1
            continue

        if not has_jd_doc:
            print(f"{'[dry-run] would write' if args.dry_run else 'writing'} JobDescription.docx: {company} / {title}")
            if not args.dry_run:
                render_job_description(
                    jd_text, company=company, title=title, out_dir=args.output_root,
                    multi_lead=multi_lead, sibling_titles=sibling_titles,
                )
            wrote_jd += 1

        if not has_review_doc:
            print(f"{'[dry-run] would write' if args.dry_run else 'writing'} no-LLM-review.docx: {company} / {title}")
            if not args.dry_run:
                score = score_jd(jd_text)
                render_no_llm_review_docx(
                    score, company=company, title=title, out_dir=args.output_root,
                    multi_lead=multi_lead, sibling_titles=sibling_titles,
                )
            wrote_review += 1

    print()
    print(f"total leads: {len(rows)}")
    print(f"already complete (JD + no-LLM-review present): {already_complete}")
    print(f"JobDescription.docx {'would be ' if args.dry_run else ''}written: {wrote_jd}")
    print(f"no-LLM-review.docx {'would be ' if args.dry_run else ''}written: {wrote_review}")
    print(f"skipped, no/thin jd_text stored (<{MIN_JD_LEN} chars): {len(no_jd_text)}")
    if no_jd_text:
        print()
        print("--- skipped (no recoverable JD text) ---")
        for company, title, status in no_jd_text:
            print(f"  {status:12s} {company!r:30s} {title!r}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
