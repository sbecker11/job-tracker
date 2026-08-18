"""CLI: attach a document (signed RTR, JD PDF, NDA, or anything else) to an
existing job (docs/JOB_CRM_VISION.md UC-4).

For a local `--file`, archives an immutable timestamped copy under
`<job folder>/document_snapshots/` and records *that* copy's path — not the
original — so a later edit/overwrite of the source file (e.g. regenerating
`Shawn_Becker_Resume_<Company>_<Role>.docx` in place, or reusing the same
filename for a different lead) can never silently rewrite this job's
history. Added 2026-08-17 after the NeoTek résumé was attached by live path
and the concern was raised that the live file could change later. Pass
`--no-snapshot` to record `--file`'s path as-is instead. `--url` sources are
never copied (nothing local to snapshot). Versioning of repeats of the same
`--doc-type` for one job is unaffected — see `store.add_job_document`.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _job_folder
from job_tracker.pipeline.models import JobDocument
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_document,
    connect,
    find_similar_jobs,
    get_job,
    get_sibling_titles,
)


def _snapshot_file(
    src: Path, *, output_root: Path, company: str, title: str, doc_type: str, conn: sqlite3.Connection
) -> Path:
    """Copy `src` into this job's `document_snapshots/` folder under a
    timestamped, collision-proof name and return the copy's path."""
    multi_lead = len(get_sibling_titles(conn, company, exclude_title=title)) > 0
    job_dir = _job_folder(output_root, company=company, title=title, multi_lead=multi_lead)
    snapshots_dir = job_dir / "document_snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = snapshots_dir / f"{doc_type}_{stamp}_{src.name}"
    shutil.copy2(src, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Attach a local file or URL as a document on an existing job.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Where per-job folders (and document_snapshots/) live (default: {DEFAULT_OUTPUT_ROOT})",
    )
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument(
        "--doc-type",
        required=True,
        help="Free-text document type — common values: jd_snapshot, resume, cover_letter, rtr, "
        "availability, nda, other",
    )
    ap.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Record --file's path as-is instead of archiving an immutable timestamped copy "
        "under document_snapshots/ (default: snapshot). Ignored for --url.",
    )

    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Local file path")
    source.add_argument("--url", help="A URL instead of a local file")

    args = ap.parse_args(argv)

    if args.file and not args.file.exists():
        print(f"Warning: {args.file} does not exist on disk — recording the path anyway.", file=sys.stderr)

    conn = connect(args.db)
    try:
        job = get_job(conn, args.company, args.title)
        if job is None:
            print(f"No job found for {args.title!r} @ {args.company!r}.", file=sys.stderr)
            candidates = find_similar_jobs(conn, args.company, args.title)
            if candidates:
                print("Did you mean one of these?", file=sys.stderr)
                for m in candidates[:5]:
                    print(f"  {m.title} @ {m.company}  (score={m.combined_score:.2f})", file=sys.stderr)
            else:
                print("Use scripts/add_job.py to create it first if this is a new job.", file=sys.stderr)
            return 1

        snapshot_path: Path | None = None
        if args.file and args.file.exists() and not args.no_snapshot:
            snapshot_path = _snapshot_file(
                args.file,
                output_root=args.output_root,
                company=args.company,
                title=args.title,
                doc_type=args.doc_type,
                conn=conn,
            )

        path_or_url = str(snapshot_path) if snapshot_path else (str(args.file) if args.file else args.url)
        doc_id = add_job_document(
            conn,
            JobDocument(job_key=job["normalized_key"], doc_type=args.doc_type, path_or_url=path_or_url),
        )
        row = conn.execute("SELECT version FROM job_documents WHERE id = ?", (doc_id,)).fetchone()
        if snapshot_path is not None:
            print(
                f"Attached {args.doc_type} (v{row['version']}) to {args.title} @ {args.company}: "
                f"archived immutable copy at {path_or_url} (source: {args.file})"
            )
        else:
            print(f"Attached {args.doc_type} (v{row['version']}) to {args.title} @ {args.company}: {path_or_url}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
