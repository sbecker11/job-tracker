"""CLI: draft a short follow-up email (post-interview thank-you, or a
stale-application status check-in) for an existing job, via the same LLM
pipeline that generates résumés/cover letters. Always saved as a plain-text
JobDocument for you to review and send yourself — never sent automatically.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, generate_followup_message
from job_tracker.pipeline.models import JobDocument
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_document,
    connect,
    find_similar_jobs,
    get_job,
    list_job_contacts,
)


def _days_since(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft a thank-you or status-check-in follow-up message for a job.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--kind", required=True, choices=("thank_you", "status_check_in"))
    ap.add_argument("--contact-name", default="", help="Overrides the auto-picked most-recent contact's name")
    ap.add_argument("--context", default="", help="Anything specific to mention (e.g. topics discussed in the interview)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = ap.parse_args(argv)

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
            return 1

        contact_name = args.contact_name
        if not contact_name:
            contacts = list_job_contacts(conn, job["normalized_key"])
            if contacts:
                contact_name = contacts[-1]["name"] or ""

        days_since_contact = _days_since(job["awaiting_response_since"]) if args.kind == "status_check_in" else None

        result = generate_followup_message(
            args.kind,
            company=args.company,
            title=args.title,
            contact_name=contact_name,
            context=args.context,
            days_since_contact=days_since_contact,
            model=args.model,
        )

        print(result.text)
        if result.warnings:
            print("\n⚠ warnings (review before sending):", file=sys.stderr)
            for w in result.warnings:
                print(f"  - {w}", file=sys.stderr)

        out_dir = args.output_root / args.company.replace(" ", "_")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{args.kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        out_path.write_text(result.text + "\n", encoding="utf-8")

        add_job_document(
            conn, JobDocument(job_key=job["normalized_key"], doc_type=args.kind, path_or_url=str(out_path))
        )
        print(f"\nSaved to {out_path}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
