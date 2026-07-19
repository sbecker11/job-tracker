#!/usr/bin/env python3
"""One-time backfill: replace stale/expiring LinkedIn email-notification apply
links with the durable ATS-resolved canonical URL, for every already-stored
lead affected by the `choose_apply_url` bug fixed 2026-07-19 (see
`pipeline/run.py`'s docstring) — before the fix, a LinkedIn tracking link
extracted from the source email always won over an available ATS URL, even
though those links are single-use and expire into a bare LinkedIn *search*
for the URL text itself ("...did not match any documents" — the Clover
Health / Senior Software Engineer lead that surfaced this).

For every lead whose stored `apply_url` is a linkedin.com link, re-resolves
the company/title against the public ATS board APIs (the same lookup
`triage_recruiter_inbox.py` already does at ingest time) and, if a
confident match is found, updates `apply_url` to that canonical URL. Leads
where the ATS lookup can't confidently re-resolve (posting closed since
first seen, no public board, etc.) are left untouched and reported
separately — those still need a human to go find a working link.

Also re-renders JobDescription.docx for any affected lead that already has
one on disk (deterministic, zero LLM cost) so the on-disk artifact's
stamped "Apply URL:" line matches the corrected DB value — this matters
most for leads already at status='package_generated', since those are
exactly the ones sitting in the "Ready to apply" dashboard funnel.

Usage:
    python scripts/backfill_apply_urls.py [--dry-run] [--db PATH] [--output-root PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename, render_job_description  # noqa: E402
from job_tracker.pipeline.run import choose_apply_url, resolve_jd_text  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_sibling_titles  # noqa: E402


def _lead_folder(out_dir: Path, *, company: str, title: str, multi_lead: bool) -> Path:
    """Read-only mirror of `llm_apply._job_folder`'s path logic — see the
    identical helper (and its rationale) in `backfill_jd_and_no_llm_review.py`."""
    company_dir = out_dir / _safe_filename(company)
    if not multi_lead:
        return company_dir
    return company_dir / _safe_filename(f"{company}_{title}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT normalized_key, company, title, status, apply_url, jd_text FROM job_leads "
        "WHERE apply_url LIKE '%linkedin.com%'"
    ).fetchall()

    fixed: list[tuple[str, str, str, str, str]] = []  # company, title, status, old, new
    unresolved: list[tuple[str, str, str]] = []  # company, title, status
    postings_cache: dict[str, list] = {}

    for i, r in enumerate(rows, 1):
        company, title, status, old_url = r["company"], r["title"], r["status"], r["apply_url"]
        print(f"[{i}/{len(rows)}] resolving {company!r} / {title!r}...", file=sys.stderr, flush=True)
        _, _, resolved_url = resolve_jd_text(company, title, postings_cache=postings_cache)
        new_url = choose_apply_url(old_url, resolved_url)
        if not resolved_url or new_url == old_url:
            unresolved.append((company, title, status))
            continue

        fixed.append((company, title, status, old_url, new_url))
        if args.dry_run:
            continue

        conn.execute(
            "UPDATE job_leads SET apply_url = ? WHERE normalized_key = ?",
            (new_url, r["normalized_key"]),
        )

        sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))
        multi_lead = len(sibling_titles) > 0
        folder = _lead_folder(args.output_root, company=company, title=title, multi_lead=multi_lead)
        jd_doc = folder / "JobDescription.docx"
        if jd_doc.is_file():
            render_job_description(
                r["jd_text"] or "",
                company=company, title=title, apply_url=new_url,
                out_dir=args.output_root, multi_lead=multi_lead, sibling_titles=sibling_titles,
            )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"Leads with a linkedin.com apply_url: {len(rows)}")
    print(f"{'Would fix' if args.dry_run else 'Fixed'}: {len(fixed)}")
    for company, title, status, old, new in fixed:
        print(f"  {status:18s} {company!r:30s} {title!r}")
        print(f"    old: {old}")
        print(f"    new: {new}")
    print(f"Could not re-resolve (left as-is, needs a human look): {len(unresolved)}")
    for company, title, status in unresolved:
        print(f"  {status:18s} {company!r:30s} {title!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
