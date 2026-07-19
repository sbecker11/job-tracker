#!/usr/bin/env python3
"""One-time backfill: write the `ApplyURL.webloc` Finder shortcut (added
2026-07-19, see `pipeline/llm_apply.render_apply_url_webloc`) into every
lead's package folder that already has a `JobDescription.docx` on disk but
predates this feature.

Every NEW `render_job_description()` call writes this automatically now —
this script only exists to catch up folders generated before that change
(and any lead whose apply_url didn't change during
`backfill_apply_urls.py`'s pass, so its JobDescription.docx never got
re-rendered either). Zero network calls, zero LLM cost — purely a local
filesystem + DB read, cheap enough to re-run any time.

Usage:
    python scripts/backfill_apply_url_weblocs.py [--dry-run] [--db PATH] [--output-root PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    _safe_filename,
    render_apply_url_webloc,
)
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
    ap.add_argument("--dry-run", action="store_true", help="Report what would be written, write nothing")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT normalized_key, company, title, status, apply_url FROM job_leads "
        "WHERE apply_url IS NOT NULL AND apply_url != ''"
    ).fetchall()

    wrote = 0
    already_present = 0
    no_jd_folder = 0

    for r in rows:
        company, title = r["company"], r["title"]
        sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))
        multi_lead = len(sibling_titles) > 0
        folder = _lead_folder(args.output_root, company=company, title=title, multi_lead=multi_lead)

        if not (folder / "JobDescription.docx").is_file():
            no_jd_folder += 1
            continue
        if (folder / "ApplyURL.webloc").is_file():
            already_present += 1
            continue

        print(f"{'[dry-run] would write' if args.dry_run else 'writing'} ApplyURL.webloc: {company} / {title}")
        if not args.dry_run:
            render_apply_url_webloc(
                r["apply_url"], company=company, title=title, out_dir=args.output_root,
                multi_lead=multi_lead, sibling_titles=sibling_titles,
            )
        wrote += 1

    conn.close()

    print()
    print(f"total leads with a non-empty apply_url: {len(rows)}")
    print(f"already had ApplyURL.webloc: {already_present}")
    print(f"ApplyURL.webloc {'would be ' if args.dry_run else ''}written: {wrote}")
    print(f"skipped, no JobDescription.docx on disk yet: {no_jd_folder}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
