"""Gmail write operations for the recruiter-inbox triage flow.

Deliberately separate from `gmail_reader.py`: every other command in this
repo only ever reads Gmail (see that module's docstring), and keeping the
one place that mutates a mailbox in its own file makes it easy to audit
exactly what `--account` / which OAuth scope can write anything.

Labels applied here live under the `JobTracker/` prefix, distinct from
comms-migration's `Category/*` taxonomy — this repo triages messages
comms-migration has already classified `Category/recruiter_job`, it never
edits or removes that upstream label.
"""

from __future__ import annotations

LABEL_PREFIX = "JobTracker/"

# Renamed 2026-07-07 (were ACCEPT/DENY) to match the LLM Match Framework's
# own verdict language (evaluate_lead()'s "pursue"/"pass"/"review") instead
# of a separate accept/deny vocabulary for the same three outcomes.
PURSUE_LABEL = f"{LABEL_PREFIX}PURSUE"
SKIP_LABEL = f"{LABEL_PREFIX}SKIP"
NEEDS_REVIEW_LABEL = f"{LABEL_PREFIX}NEEDS_REVIEW"

ALL_OUTCOME_LABELS = (PURSUE_LABEL, SKIP_LABEL, NEEDS_REVIEW_LABEL)

# Added 2026-07-19 for `cli/scan_communications.py`'s LinkedIn-reply traffic
# (Category/social, never touched by the PURSUE/SKIP/NEEDS_REVIEW labels
# above, which only ever apply to Category/recruiter_job mail). The goal:
# make Gmail's own label state trustworthy enough for the mailbox owner to
# stop reviewing recruiting mail directly and rely on this pipeline (+ its
# dashboard) instead — see PRIMER.md's "Recruiter contact extraction" /
# "Communications archival" sections for the full rationale. LINKED means
# fully archived into the pipeline (job matched, conversation + contact
# recorded) — nothing left for a human to do via Gmail. NEEDS_FOLLOWUP means
# the opposite: parked in `unmatched_messages` because no job could be
# confidently matched, so it's deliberately left in the inbox (not archived)
# and still needs a human's eyes, either directly or via
# `resolve_communication.py`.
LINKED_LABEL = f"{LABEL_PREFIX}Linked"
NEEDS_FOLLOWUP_LABEL = f"{LABEL_PREFIX}NeedsFollowup"


def find_label_id(service, label_name: str) -> str | None:
    """Look up an existing label's id by name without creating it if
    missing — unlike `get_or_create_label`, appropriate for checking against
    a label this repo doesn't own (e.g. the recruiting account's own
    Gmail-filter-created "Job-Digests" label)."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == label_name:
            return label["id"]
    return None


def get_or_create_label(service, label_name: str) -> str:
    """Return the Gmail label id for `label_name`, creating it if needed."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == label_name:
            return label["id"]
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    return created["id"]


def label_and_archive(
    service, message_id: str, label_id: str, *, remove_label_ids: list[str] | None = None, archive: bool = True
) -> None:
    """Add `label_id` and, by default, remove INBOX so the message leaves the
    inbox but stays searchable under that label.

    `remove_label_ids` additionally strips other label ids (e.g. a stale
    JobTracker/NEEDS_REVIEW from a prior run being re-triaged after a
    classifier fix) — harmless to include ids the message never had, Gmail's
    `messages.modify` silently no-ops those.

    `archive=False` still applies the label (and strips `remove_label_ids`)
    but leaves INBOX alone — used for a message whose extraction was judged
    incomplete (see `pipeline/triage.py`'s `extraction_complete`) so it stays
    visible for a human instead of getting filed away as if fully handled.
    """
    remove_ids = ["INBOX", *(remove_label_ids or [])] if archive else list(remove_label_ids or [])
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id], "removeLabelIds": remove_ids},
    ).execute()
