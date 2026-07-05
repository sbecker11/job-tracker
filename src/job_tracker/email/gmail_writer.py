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

ACCEPT_LABEL = f"{LABEL_PREFIX}ACCEPT"
DENY_LABEL = f"{LABEL_PREFIX}DENY"
NEEDS_REVIEW_LABEL = f"{LABEL_PREFIX}NEEDS_REVIEW"

ALL_OUTCOME_LABELS = (ACCEPT_LABEL, DENY_LABEL, NEEDS_REVIEW_LABEL)


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


def label_and_archive(service, message_id: str, label_id: str) -> None:
    """Add `label_id` and remove INBOX so the message leaves the inbox but
    stays searchable under that label."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
    ).execute()
