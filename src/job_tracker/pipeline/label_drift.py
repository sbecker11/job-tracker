"""Label↔DB drift detection for JobTracker/* Gmail outcome labels.

Shared by resync_labels.py (apply fixes) and audit_label_drift.py (report only).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from job_tracker.pipeline.store import get_lead_labeling_info, list_processed_messages_with_leads
from job_tracker.pipeline.triage import decide_outcome_from_verdicts


@dataclass(frozen=True)
class LabelDriftEntry:
    message_id: str
    current_outcome: str
    desired_outcome: str
    reason: str


def _effective_lead_verdict(row) -> str:
    verdict = row["llm_verdict"] or row["verdict"] or "review"
    return verdict if verdict in ("pursue", "review", "pass") else "review"


def compute_label_drift(
    conn: sqlite3.Connection,
    current_labels: dict[str, str],
) -> tuple[list[LabelDriftEntry], int, int]:
    """Return (drift entries, checked_count, skipped_imap_count)."""
    processed = list_processed_messages_with_leads(conn)
    entries: list[LabelDriftEntry] = []
    checked = 0
    skipped_imap = 0

    for entry in processed:
        message_id = entry["message_id"]
        if message_id.startswith("imap:") or message_id.startswith("imap-uid:"):
            skipped_imap += 1
            continue
        lead_rows = get_lead_labeling_info(conn, entry["lead_keys"])
        if not lead_rows:
            continue
        checked += 1
        verdicts = {_effective_lead_verdict(row) for row in lead_rows.values()}
        desired_outcome, reason = decide_outcome_from_verdicts(verdicts)
        current_outcome = current_labels.get(message_id, entry["outcome"])
        if desired_outcome == current_outcome:
            continue
        entries.append(
            LabelDriftEntry(
                message_id=message_id,
                current_outcome=current_outcome,
                desired_outcome=desired_outcome,
                reason=reason,
            )
        )

    return entries, checked, skipped_imap
