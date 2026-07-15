"""CLI: attach a document (signed RTR, JD PDF, NDA, or anything else) to an
existing job (docs/JOB_CRM_VISION.md UC-4). Records the given path/URL as-is
— job-tracker doesn't copy or manage the underlying file, just remembers
where it lives and versions repeats of the same `--doc-type` for one job
(see `store.add_job_document`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.models import JobDocument
from job_tracker.pipeline.store import DEFAULT_DB_PATH, add_job_document, connect, find_similar_jobs, get_job


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Attach a local file or URL as a document on an existing job.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument(
        "--doc-type",
        required=True,
        help="Free-text document type — common values: jd_snapshot, resume, cover_letter, rtr, "
        "availability, nda, other",
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

        path_or_url = str(args.file) if args.file else args.url
        doc_id = add_job_document(
            conn,
            JobDocument(job_key=job["normalized_key"], doc_type=args.doc_type, path_or_url=path_or_url),
        )
        row = conn.execute("SELECT version FROM job_documents WHERE id = ?", (doc_id,)).fetchone()
        print(f"Attached {args.doc_type} (v{row['version']}) to {args.title} @ {args.company}: {path_or_url}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
