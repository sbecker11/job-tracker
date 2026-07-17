"""CLI: triage the recruiter-inbox — classify, extract, resolve JD, LLM-score
(+ auto-generate a résumé/cover letter on a "pursue" verdict), then relabel
the source Gmail message PURSUE / SKIP / NEEDS_REVIEW and archive it.

This is a different, higher-stakes command than `run_pipeline.py`: it
requires the `gmail.modify` OAuth scope (one-time consent — see
`job_tracker.email.gmail_reader.get_gmail_service_writable`) and, unless
`--dry-run` is given, both spends money on the Anthropic API for every
message it touches and mutates the mailbox (label + archive). It only ever
looks at mail comms-migration has already labeled `Category/recruiter_job`
on the default recruiting-funnel account, and skips anything this repo has
already triaged (tracked in `processed_messages` and by the `JobTracker/*`
labels themselves, so a message is never double-billed or double-labeled)
unless `--force` is given — e.g. re-running everything already parked in
`JobTracker/NEEDS_REVIEW` (note: pass `--query 'label:JobTracker/NEEDS_REVIEW'`,
no `in:inbox`, since those messages are already archived) after a classifier
fix, in which case the stale outcome label is replaced rather than stacked.

Every triaged message gets a JobTracker/* outcome label, but archiving
(removing INBOX) is conditional for one case: a message that also carries
the recruiting account's own Gmail-filter "Job-Digests" label only gets
archived if extraction was judged complete (see
`pipeline.triage.MessageTriageResult.extraction_complete`) — otherwise it's
labeled but left visible in the inbox, since silently filing away a digest
that wasn't fully picked apart is worse than a bit of inbox clutter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import (
    KNOWN_ACCOUNTS,
    default_credentials_path,
    default_token_path,
    fetch_message,
    get_gmail_service,
    get_gmail_service_writable,
    list_message_ids,
)
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_EXTRACT_MODEL
from job_tracker.pipeline.models import JobContact, JobConversation
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    DEFAULT_REJECTION_COOLDOWN_DAYS,
    add_job_contact,
    add_job_conversation,
    advance_status,
    connect,
    find_matching_job,
    is_message_processed,
    processed_at,
    record_message_processed,
    update_llm_evaluation,
    upsert_lead,
)
from job_tracker.pipeline.triage import (
    DEFAULT_MAX_LLM_EXTRACTED_ROLES,
    NEEDS_REVIEW,
    PURSUE,
    SKIP,
    triage_message,
)

DEFAULT_QUERY = (
    "label:Category/recruiter_job in:inbox "
    "-label:JobTracker/PURSUE -label:JobTracker/SKIP -label:JobTracker/NEEDS_REVIEW"
)

# The recruiting account's own Gmail filter (not this repo) stamps this
# label on LinkedIn/job-board digest mail (see comms-migration's
# routing-inventory.md). For a message that also carries it, archiving is
# gated on `MessageTriageResult.extraction_complete` (2026-07-07 fix): a
# digest that wasn't fully picked apart should stay visible in the inbox
# rather than silently disappearing as if it had been fully handled — see
# chat history for the incident that prompted this (3 job-alert messages
# found archived by something other than this repo, having never actually
# been triaged at all).
JOB_DIGESTS_LABEL_NAME = "Job-Digests"

_OUTCOME_LABELS = {
    PURSUE: gmail_writer.PURSUE_LABEL,
    SKIP: gmail_writer.SKIP_LABEL,
    NEEDS_REVIEW: gmail_writer.NEEDS_REVIEW_LABEL,
}


def _print_result(result, *, dry_run: bool) -> None:
    print(f"\n[{result.outcome}] {result.subject}  <{result.from_address}>  ({result.message_id})")
    print(f"  classifier: {result.classifier_label}  —  {result.reason}")
    for role_outcome in result.roles:
        ev = role_outcome.package.evaluation
        print(f"    {role_outcome.lead.title} @ {role_outcome.lead.company}: {ev.verdict.upper()} ({ev.match_pct:.0f}%)")
        if role_outcome.package.jd_path:
            print(f"      folder:       {role_outcome.package.jd_path.parent}")
        review = role_outcome.package.full_llm_review_path or role_outcome.package.no_llm_review_path
        if review:
            print(f"      review:       {review}")
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
        "résumé/cover letter on 'pursue', then relabel PURSUE/SKIP/NEEDS_REVIEW and archive."
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
        "--llm-fallback",
        action="store_true",
        help=(
            "For link-only-digest mail (and any SINGLE_JD/MULTI_JD_IN_BODY message the regex "
            "extractor can't parse), fall back to one cheap, cached LLM call per message to pull "
            "out every job listing before giving up to NEEDS_REVIEW. Costs real money per digest "
            "(capped per-message extraction call, cached by message_id) and, for however many "
            "roles it finds (up to --max-llm-extracted-roles), one additional full JD Match "
            "Framework evaluation each — same as any other extracted role."
        ),
    )
    ap.add_argument(
        "--llm-extraction-model",
        default=DEFAULT_LLM_EXTRACT_MODEL,
        help=f"Anthropic model id/alias for the extraction fallback (default: {DEFAULT_LLM_EXTRACT_MODEL})",
    )
    ap.add_argument(
        "--max-llm-extracted-roles",
        type=int,
        default=DEFAULT_MAX_LLM_EXTRACTED_ROLES,
        help=(
            "Cap on how many LLM-extracted roles from one digest get evaluated (top N by "
            f"extraction confidence) — bounds worst-case Sonnet spend on one email (default: "
            f"{DEFAULT_MAX_LLM_EXTRACTED_ROLES})"
        ),
    )
    ap.add_argument(
        "--rejection-cooldown-days",
        type=int,
        default=DEFAULT_REJECTION_COOLDOWN_DAYS,
        help=(
            "Auto-disqualify (skip straight to a 'pass' verdict, no LLM spend) a role whose "
            "exact (company, title) was already confirmed rejected (status='rejected') within "
            f"this many days (default: {DEFAULT_REJECTION_COOLDOWN_DAYS}). Set to 0 to disable."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print what would happen, but never touch Gmail (no label/archive) or write to the DB",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-triage messages even if processed_messages already has a row for them "
            "(e.g. re-running NEEDS_REVIEW mail — label:JobTracker/NEEDS_REVIEW, since it's "
            "already archived — after a classifier fix). Replaces any stale JobTracker/* "
            "outcome label with the freshly computed one instead of stacking both."
        ),
    )
    ap.add_argument(
        "--force-since",
        metavar="ISO8601_TIMESTAMP",
        help=(
            "Resume an interrupted --force batch (e.g. after killing a hung run) without "
            "re-billing whatever it already got through: reprocess a message unless its "
            "processed_messages row's processed_at is already >= this timestamp. Must match "
            "the stored format exactly (UTC with +00:00 offset, e.g. 2026-07-07T03:09:17+00:00 "
            "— models.utc_now_iso()'s output). Implies --force; use instead of --force, not with it."
        ),
    )
    ap.add_argument(
        "--account",
        choices=KNOWN_ACCOUNTS,
        help="Triage a named account other than the default recruiting funnel "
        "(credentials/token resolved from ~/.config/job-tracker/<account>/ unless "
        "--credentials/--token override). E.g. --account personal_hub to catch up on "
        "job-lead mail that landed on scbboston@gmail.com — including historical "
        "backlog from before the recruiting funnel forwards existed (see "
        "comms-migration's routing-inventory.md); remember to drop the default "
        "query's `in:inbox` (--query \"label:Category/recruiter_job -label:JobTracker/PURSUE "
        "-label:JobTracker/SKIP -label:JobTracker/NEEDS_REVIEW\") since that mail may "
        "already be archived/read, and this account needs its own one-time "
        "gmail.modify consent the first time you run this without --dry-run.",
    )
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a report")
    args = ap.parse_args(argv)

    credentials_path = args.credentials or default_credentials_path(args.account)
    if args.dry_run:
        # Scoring-only preview never mutates the mailbox — no reason to force
        # the separate, one-time-consent write token just to look.
        token_path = args.token or default_token_path(args.account)
        service = get_gmail_service(credentials_path, token_path, account=args.account)
    else:
        token_path = args.token or default_token_path(args.account, writable=True)
        service = get_gmail_service_writable(credentials_path, token_path, account=args.account)

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
    errored_message_ids: list[str] = []

    # Resolve every outcome label id once up front (not just the one this
    # message ends up with) so a --force re-triage can strip a stale
    # JobTracker/* label left over from a prior run instead of stacking a
    # second outcome label alongside it.
    outcome_label_ids: dict[str, str] = {}
    job_digests_label_id: str | None = None
    if not args.dry_run:
        outcome_label_ids = {
            outcome: gmail_writer.get_or_create_label(service, label) for outcome, label in _OUTCOME_LABELS.items()
        }
        job_digests_label_id = gmail_writer.find_label_id(service, JOB_DIGESTS_LABEL_NAME)

    try:
        for message_id in message_ids:
            if not args.dry_run:
                if args.force_since:
                    last_processed = processed_at(conn, message_id)
                    if last_processed is not None and last_processed >= args.force_since:
                        skipped_already_processed += 1
                        continue
                elif not args.force and is_message_processed(conn, message_id):
                    skipped_already_processed += 1
                    continue

            try:
                message = fetch_message(service, message_id)
                result = triage_message(
                    message,
                    model=args.model,
                    generate=not args.no_generate,
                    output_root=args.output_root,
                    resolve_full_jd=not args.offline,
                    postings_cache=postings_cache,
                    use_llm_extraction_fallback=args.llm_fallback,
                    llm_extraction_model=args.llm_extraction_model,
                    max_llm_extracted_roles=args.max_llm_extracted_roles,
                    conn=conn,
                    rejection_cooldown_days=args.rejection_cooldown_days,
                )
            except Exception as exc:
                # One bad message (a timed-out API call, a transient Gmail
                # 5xx, ...) must not take the whole batch down with it —
                # there's no cheap way to resume mid-batch otherwise, and a
                # large --force re-triage run can take hours. Left off
                # JobTracker/* and processed_messages entirely (unlike a
                # normal NEEDS_REVIEW outcome) so the next run — with or
                # without --force — picks it back up automatically.
                print(f"\n[ERROR] {message_id}: {exc!r} — skipping, will retry next run", file=sys.stderr)
                errored_message_ids.append(message_id)
                continue

            results.append(result)
            _print_result(result, dry_run=args.dry_run)

            if args.dry_run:
                continue

            lead_keys = []
            for role_outcome in result.roles:
                upsert_lead(conn, role_outcome.lead)
                key = role_outcome.lead.normalized_key
                lead_keys.append(key)
                # Bug fix (2026-07-07): upsert_lead() only writes the base
                # verdict/rationale columns — without this, the full JD
                # review (match_pct, dealbreaker checks, skills alignment,
                # flags, framing guidance) computed by generate_package()
                # above was silently discarded instead of landing in the
                # llm_* columns update_llm_evaluation() exists precisely to
                # fill in.
                update_llm_evaluation(conn, key, role_outcome.package.evaluation)

                # UC-2 (docs/JOB_CRM_VISION.md): does this (company, title)
                # fuzzy-match a *different* job we already track? If so, this
                # is likely the same role via a second recruiter — attach the
                # contact/conversation there instead of only under the new
                # lead's own key, so "multiple recruiters, same job" shows up
                # as one job with several contacts rather than looking like
                # two unrelated leads.
                job_key = key
                match = find_matching_job(conn, role_outcome.lead.company, role_outcome.lead.title)
                if match and match.normalized_key != key:
                    job_key = match.normalized_key
                    print(
                        f"    \u26a0 looks like the same role as an existing job "
                        f"({match.company} / {match.title}, already tracked) — "
                        f"linking this contact there instead of a new job"
                    )

                contact_id = add_job_contact(
                    conn,
                    JobContact(
                        job_key=job_key,
                        email=result.from_address,
                        role="recruiter",
                        source_message_id=message_id,
                    ),
                )
                add_job_conversation(
                    conn,
                    JobConversation(
                        job_key=job_key,
                        contact_id=contact_id,
                        message_id=message_id,
                        direction="inbound",
                        summary=result.subject,
                    ),
                )

                # Advance the lead's lifecycle stage (models.LEAD_STAGES) —
                # "package_generated" implies "pursued" already happened,
                # so it's the only stamp needed on a pursue-and-generated
                # lead; everything past this point (applied, interviewing,
                # offered, ...) is a human reporting real-world progress via
                # `list_leads.py --set-status`, not something triage infers.
                #
                # Bug fix (2026-07-07): a digest fans out into multiple roles,
                # each independently LLM-scored, but `result.outcome` is the
                # *message*-level decision ("at least one role scored
                # pursue" per triage._decide_outcome) — using it here stamped
                # EVERY role from a pursue-worthy digest as "pursued", even
                # siblings whose own verdict was "pass". Each lead's own
                # `role_outcome.package.evaluation.verdict` is the correct,
                # per-role signal. A "review" verdict here deliberately
                # leaves the lead at "new" — that specific role wasn't
                # confidently decided either way, even though a sibling
                # role's "pursue" may have earned the message JobTracker/PURSUE.
                role_verdict = role_outcome.package.evaluation.verdict
                if role_outcome.package.resume_path is not None:
                    advance_status(conn, key, "package_generated")
                elif role_verdict == "pursue":
                    advance_status(conn, key, "pursued")
                elif role_verdict == "pass":
                    advance_status(conn, key, "skipped")

            label_id = outcome_label_ids[result.outcome]
            stale_label_ids = [lid for outcome, lid in outcome_label_ids.items() if outcome != result.outcome]
            is_job_digest = job_digests_label_id is not None and job_digests_label_id in message.label_ids
            should_archive = result.extraction_complete or not is_job_digest
            gmail_writer.label_and_archive(
                service, message_id, label_id, remove_label_ids=stale_label_ids, archive=should_archive
            )
            if is_job_digest and not should_archive:
                print(
                    f"    \u26a0 Job-Digests message left in the inbox — extraction wasn't complete "
                    f"({result.extraction_issue or 'possible truncation'}); labeled "
                    f"{_OUTCOME_LABELS[result.outcome]} but not archived"
                )
            record_message_processed(
                conn,
                message_id,
                outcome=result.outcome,
                subject=result.subject,
                from_address=result.from_address,
                lead_keys=lead_keys,
                label_applied=_OUTCOME_LABELS[result.outcome],
                archived=should_archive,
            )
    finally:
        conn.close()

    if skipped_already_processed:
        print(f"\n(skipped {skipped_already_processed} message(s) already triaged in a prior run)", file=sys.stderr)

    if errored_message_ids:
        print(
            f"\n({len(errored_message_ids)} message(s) errored and were left untouched — "
            f"re-run (no --force needed) to retry just these): {', '.join(errored_message_ids)}",
            file=sys.stderr,
        )

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
                                "jd_path": str(ro.package.jd_path) if ro.package.jd_path else None,
                                "review_path": str(
                                    ro.package.full_llm_review_path or ro.package.no_llm_review_path
                                )
                                if (ro.package.full_llm_review_path or ro.package.no_llm_review_path)
                                else None,
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

    counts = {PURSUE: 0, SKIP: 0, NEEDS_REVIEW: 0}
    for r in results:
        counts[r.outcome] += 1
    print(
        f"\nProcessed {len(results)} message(s): "
        f"{counts[PURSUE]} PURSUE, {counts[SKIP]} SKIP, {counts[NEEDS_REVIEW]} NEEDS_REVIEW",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
