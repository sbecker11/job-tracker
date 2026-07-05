"""CLI: triage the recruiter-inbox — classify, extract, resolve JD, LLM-score
(+ auto-generate a résumé/cover letter on a "pursue" verdict), then relabel
the source Gmail message ACCEPT / DENY / NEEDS_REVIEW and archive it.

This is a different, higher-stakes command than `run_pipeline.py`: it
requires the `gmail.modify` OAuth scope (one-time consent — see
`job_tracker.email.gmail_reader.get_gmail_service_writable`) and, unless
`--dry-run` is given, both spends money on the Anthropic API for every
message it touches and mutates the mailbox (label + archive). It only ever
looks at mail comms-migration has already labeled `Category/recruiter_job`
on the default recruiting-funnel account, and skips anything this repo has
already triaged (tracked in `processed_messages` and by the `JobTracker/*`
labels themselves, so a message is never double-billed or double-labeled).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import (
    default_credentials_path,
    default_token_path,
    fetch_message,
    get_gmail_service,
    get_gmail_service_writable,
    list_message_ids,
)
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    advance_status,
    connect,
    is_message_processed,
    record_message_processed,
    upsert_lead,
)
from job_tracker.pipeline.triage import ACCEPT, DENY, NEEDS_REVIEW, triage_message

DEFAULT_QUERY = (
    "label:Category/recruiter_job in:inbox "
    "-label:JobTracker/ACCEPT -label:JobTracker/DENY -label:JobTracker/NEEDS_REVIEW"
)

_OUTCOME_LABELS = {
    ACCEPT: gmail_writer.ACCEPT_LABEL,
    DENY: gmail_writer.DENY_LABEL,
    NEEDS_REVIEW: gmail_writer.NEEDS_REVIEW_LABEL,
}


def _print_result(result, *, dry_run: bool) -> None:
    print(f"\n[{result.outcome}] {result.subject}  <{result.from_address}>  ({result.message_id})")
    print(f"  classifier: {result.classifier_label}  —  {result.reason}")
    for role_outcome in result.roles:
        ev = role_outcome.package.evaluation
        print(f"    {role_outcome.lead.title} @ {role_outcome.lead.company}: {ev.verdict.upper()} ({ev.match_pct:.0f}%)")
        if role_outcome.package.resume_path:
            print(f"      resume:       {role_outcome.package.resume_path}")
            print(f"      cover letter: {role_outcome.package.cover_letter_path}")
        if role_outcome.package.warnings:
            print(f"      \u26a0 warnings: {role_outcome.package.warnings}")
    if dry_run:
        print("  (dry run — no Gmail label/archive applied, no lead stored)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Triage Category/recruiter_job mail: LLM-score each JD, auto-generate a "
        "résumé/cover letter on 'pursue', then relabel ACCEPT/DENY/NEEDS_REVIEW and archive."
    )
    ap.add_argument("--query", default=DEFAULT_QUERY, help=f"Gmail search query (default: {DEFAULT_QUERY!r})")
    ap.add_argument("--limit", type=int, help="Max messages to process this run")
    ap.add_argument("--newer-than", type=int, metavar="DAYS")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id/alias (default: {DEFAULT_MODEL})")
    ap.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Résumé/cover-letter output root (default: {DEFAULT_OUTPUT_ROOT})",
    )
    ap.add_argument(
        "--no-generate",
        action="store_true",
        help="Score with the LLM but never generate a résumé/cover letter, even on a pursue verdict",
    )
    ap.add_argument("--offline", action="store_true", help="Skip live ATS lookups; score against email body text only")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print what would happen, but never touch Gmail (no label/archive) or write to the DB",
    )
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a report")
    args = ap.parse_args(argv)

    credentials_path = args.credentials or default_credentials_path()
    if args.dry_run:
        # Scoring-only preview never mutates the mailbox — no reason to force
        # the separate, one-time-consent write token just to look.
        token_path = args.token or default_token_path()
        service = get_gmail_service(credentials_path, token_path)
    else:
        token_path = args.token or default_token_path(writable=True)
        service = get_gmail_service_writable(credentials_path, token_path)

    query = args.query
    if args.newer_than:
        query = f"{query} newer_than:{args.newer_than}d"
    message_ids = list_message_ids(service, query=query, limit=args.limit)

    if not message_ids:
        print("No messages matched the query.", file=sys.stderr)
        return 0

    conn = connect(args.db)
    postings_cache: dict[str, list] = {}
    results = []
    skipped_already_processed = 0

    try:
        for message_id in message_ids:
            if not args.dry_run and is_message_processed(conn, message_id):
                skipped_already_processed += 1
                continue

            message = fetch_message(service, message_id)
            result = triage_message(
                message,
                model=args.model,
                generate=not args.no_generate,
                output_root=args.output_root,
                resolve_full_jd=not args.offline,
                postings_cache=postings_cache,
            )
            results.append(result)
            _print_result(result, dry_run=args.dry_run)

            if args.dry_run:
                continue

            lead_keys = []
            for role_outcome in result.roles:
                upsert_lead(conn, role_outcome.lead)
                key = role_outcome.lead.normalized_key
                lead_keys.append(key)
                # Advance the lead's lifecycle stage (models.LEAD_STAGES) —
                # "package_generated" implies "approved" already happened,
                # so it's the only stamp needed on a pursue-and-generated
                # lead; everything past this point (applied, interviewing,
                # offered, ...) is a human reporting real-world progress via
                # `list_leads.py --set-status`, not something triage infers.
                if role_outcome.package.resume_path is not None:
                    advance_status(conn, key, "package_generated")
                elif result.outcome == ACCEPT:
                    advance_status(conn, key, "approved")
                elif result.outcome == DENY:
                    advance_status(conn, key, "passed")

            label_id = gmail_writer.get_or_create_label(service, _OUTCOME_LABELS[result.outcome])
            gmail_writer.label_and_archive(service, message_id, label_id)
            record_message_processed(
                conn,
                message_id,
                outcome=result.outcome,
                subject=result.subject,
                from_address=result.from_address,
                lead_keys=lead_keys,
                label_applied=_OUTCOME_LABELS[result.outcome],
                archived=True,
            )
    finally:
        conn.close()

    if skipped_already_processed:
        print(f"\n(skipped {skipped_already_processed} message(s) already triaged in a prior run)", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "message_id": r.message_id,
                        "subject": r.subject,
                        "from": r.from_address,
                        "outcome": r.outcome,
                        "reason": r.reason,
                        "classifier_label": r.classifier_label,
                        "roles": [
                            {
                                "company": ro.lead.company,
                                "title": ro.lead.title,
                                "verdict": ro.package.evaluation.verdict,
                                "match_pct": ro.package.evaluation.match_pct,
                                "resume_path": str(ro.package.resume_path) if ro.package.resume_path else None,
                                "cover_letter_path": str(ro.package.cover_letter_path)
                                if ro.package.cover_letter_path
                                else None,
                                "warnings": ro.package.warnings,
                            }
                            for ro in r.roles
                        ],
                    }
                    for r in results
                ],
                indent=2,
            )
        )

    counts = {ACCEPT: 0, DENY: 0, NEEDS_REVIEW: 0}
    for r in results:
        counts[r.outcome] += 1
    print(
        f"\nProcessed {len(results)} message(s): "
        f"{counts[ACCEPT]} ACCEPT, {counts[DENY]} DENY, {counts[NEEDS_REVIEW]} NEEDS_REVIEW",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
