"""CLI: re-sync each already-triaged message's `JobTracker/PURSUE|SKIP|
NEEDS_REVIEW` Gmail label to its linked lead(s)' CURRENT verdict.

Background (2026-07-19): `triage_recruiter_inbox.py` applies its outcome
label exactly once, at initial triage, using whatever verdict was available
in that moment — often just the free rule-based pass, before a full LLM
review has even run. Nothing after that ever revisits the label: a later
`--force-llm-review` run, a `run_full_llm_review_for_pursue_leads.py` batch,
or a human calling `list_leads.py --set-status` can all change a lead's
effective verdict, and the Gmail message just sits there with a now-wrong
label. Verified live (2026-07-19): several leads whose initial rule-based
"pursue" verdict was later overturned to "pass" by the full LLM review still
carried `JobTracker/PURSUE` in Gmail weeks later.

This defeats the entire point of having the label in the first place — the
mailbox owner's stated goal is to stop reviewing recruiting mail directly in
the Gmail client and trust this pipeline (+ its dashboard) instead, which
only works if a Gmail label (and any client-side filter built on top of it)
still means what it claims to mean. This command closes that gap: for every
message `triage_recruiter_inbox.py` has already labeled (tracked in
`processed_messages.lead_keys`), it re-derives today's outcome from the
CURRENT `job_leads` row(s) for those keys — same PURSUE > NEEDS_REVIEW > SKIP
priority rule `pipeline.triage.decide_outcome_from_verdicts` uses at initial
triage, just sourced from the stored `llm_verdict` (falling back to the
rule-based `verdict` if no full review has run yet) instead of a fresh LLM
call — and swaps the label if it no longer matches.

Deliberately narrow in scope:
  - Never re-evaluates anything (no LLM spend, no re-scoring) — purely reads
    `job_leads` and compares against Gmail's current label state.
  - Never touches INBOX/archived state. Whether a message is visible in the
    inbox was decided once at initial triage based on extraction
    completeness (see `triage_recruiter_inbox.py`'s Job-Digests handling),
    which has nothing to do with verdict drift — a pure label swap leaves
    that alone.
  - Only ever considers messages already in `processed_messages` with at
    least one linked lead; a message that was NEEDS_REVIEW with zero
    extracted roles has no lead to resync against and is left as-is.

To find each message's CURRENT Gmail label cheaply (without a `messages.get`
call per message), this lists all three outcome labels up front
(`label:JobTracker/PURSUE` etc. — 3 Gmail API calls total, however many
messages there are) rather than fetching every message individually.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import (
    KNOWN_ACCOUNTS,
    default_credentials_path,
    default_token_path,
    get_gmail_service,
    get_gmail_service_writable,
    list_message_ids,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_lead_labeling_info, list_processed_messages_with_leads
from job_tracker.pipeline.triage import NEEDS_REVIEW, PURSUE, SKIP, decide_outcome_from_verdicts

_OUTCOME_LABEL_NAMES = {
    PURSUE: gmail_writer.PURSUE_LABEL,
    SKIP: gmail_writer.SKIP_LABEL,
    NEEDS_REVIEW: gmail_writer.NEEDS_REVIEW_LABEL,
}


def _effective_lead_verdict(row) -> str:
    """`llm_verdict` (the full JD Match Framework review) if one has run,
    else the free rule-based `verdict`. Any value outside {"pursue",
    "review", "pass"} — in practice only the special "REVIEW NEEDED" marker
    (an unresolvable JD, see PRIMER.md) — is folded into "review" so it
    still maps onto one of the three Gmail outcome labels rather than being
    silently dropped from the verdict set."""
    verdict = row["llm_verdict"] or row["verdict"] or "review"
    return verdict if verdict in ("pursue", "review", "pass") else "review"


def _current_label_map(service) -> dict[str, str]:
    """`{message_id: outcome}` for every message currently carrying one of
    the three JobTracker/* outcome labels, built from 3 `messages.list`
    calls rather than one `messages.get` per candidate message."""
    current: dict[str, str] = {}
    for outcome, label_name in _OUTCOME_LABEL_NAMES.items():
        for message_id in list_message_ids(service, query=f"label:{label_name}"):
            current[message_id] = outcome
    return current


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Re-sync JobTracker/PURSUE|SKIP|NEEDS_REVIEW Gmail labels to each message's linked "
        "lead(s)' CURRENT verdict — see module docstring for why this exists."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--account", choices=KNOWN_ACCOUNTS, default=None)
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="Print what would change; never touch Gmail")
    args = ap.parse_args(argv)

    credentials_path = args.credentials or default_credentials_path(args.account)
    if args.dry_run:
        token_path = args.token or default_token_path(args.account)
        service = get_gmail_service(credentials_path, token_path, account=args.account)
    else:
        token_path = args.token or default_token_path(args.account, writable=True)
        service = get_gmail_service_writable(credentials_path, token_path, account=args.account)

    conn = connect(args.db)
    try:
        processed = list_processed_messages_with_leads(conn)
        if not processed:
            print("No triaged messages with linked leads to check.", file=sys.stderr)
            return 0

        current_labels = _current_label_map(service)

        label_ids: dict[str, str] = {}
        if not args.dry_run:
            label_ids = {
                outcome: gmail_writer.get_or_create_label(service, name) for outcome, name in _OUTCOME_LABEL_NAMES.items()
            }

        checked = 0
        changed = 0
        for entry in processed:
            message_id = entry["message_id"]
            lead_rows = get_lead_labeling_info(conn, entry["lead_keys"])
            if not lead_rows:
                # Every linked lead has since been deleted — nothing left to
                # resync against; leave whatever label is already there.
                continue
            checked += 1
            verdicts = {_effective_lead_verdict(row) for row in lead_rows.values()}
            desired_outcome, reason = decide_outcome_from_verdicts(verdicts)
            current_outcome = current_labels.get(message_id, entry["outcome"])

            if desired_outcome == current_outcome:
                continue

            changed += 1
            print(
                f"[{'would relabel' if args.dry_run else 'relabeling'}] {message_id}: "
                f"{current_outcome} -> {desired_outcome} ({reason})"
            )
            if args.dry_run:
                continue

            desired_label_id = label_ids[desired_outcome]
            stale_label_ids = [lid for outcome, lid in label_ids.items() if outcome != desired_outcome]
            gmail_writer.label_and_archive(
                service, message_id, desired_label_id, remove_label_ids=stale_label_ids, archive=False
            )

        verb = "would need relabeling" if args.dry_run else "were relabeled"
        print(f"\nChecked {checked} triaged message(s) with linked leads: {changed} {verb}.", file=sys.stderr)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
