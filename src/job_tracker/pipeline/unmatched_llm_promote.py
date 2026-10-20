"""LLM catch-up pass for parked unmatched InMails (2026-10-19).

The 3-minute `comms_fast_cycle.py` tick runs `scan_communications.py` and
`triage_imap_inbox.py` without `--llm-fallback`, so a pitch the free
heuristic can't parse (no `Position:` line, no Inc/LLC/Corporation line) is
parked in `unmatched_messages` within minutes. The hourly run does pass
`--llm-fallback`, but `is_communication_seen` makes it skip anything already
parked, and IMAP copies have also been moved out of INBOX by then — so the
LLM extraction never ran on them (Ed Soucie / Intentsify, Grant Naudet /
Remy Corporation).

This pass works from the stored `unmatched_messages.body_text` alone, so it
covers Gmail and IMAP copies alike and never touches a mailbox. Each open
inbound row gets at most one successful LLM extraction: rows that already
have an `llm_extraction_cache` entry are skipped, and a zero-role answer is
cached too. Every extracted company + title becomes (or matches) a lead —
a recruiter pitching two roles gets two leads — via the same
`promote_unmatched_row` the free heuristic promotion uses.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline import store
from job_tracker.pipeline.comms_match import outcome_from_role
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL, extract_roles_llm_cached
from job_tracker.pipeline.qualifying_reply import promote_unmatched_row


@dataclass
class PromoteResult:
    message_id: str
    subject: str
    action: str
    job_keys: list[str] = field(default_factory=list)


def _eligible_rows(conn: sqlite3.Connection, *, newer_than_days: int | None) -> list[sqlite3.Row]:
    cutoff = None
    if newer_than_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=newer_than_days)).isoformat()
    rows = []
    for row in store.list_unmatched_messages(conn):
        if row["direction"] != "inbound":
            continue
        if cutoff and (row["detected_at"] or "") < cutoff:
            continue
        if store.get_llm_cache(conn, row["message_id"]) is not None:
            continue
        rows.append(row)
    return rows


def _message_from_row(row: sqlite3.Row) -> EmailMessage:
    return EmailMessage(
        id=row["message_id"],
        from_address=row["from_address"] or "",
        to_address=row["to_address"] or "",
        subject=row["subject"] or "",
        body_plain=row["body_text"] or "",
        thread_id=row["thread_id"] or "",
    )


def promote_unmatched_with_llm(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    newer_than_days: int | None = 21,
    limit: int | None = 20,
    dry_run: bool = False,
    client=None,
) -> list[PromoteResult]:
    """Run one cached LLM extraction per eligible parked message and promote
    every company + title it finds. `dry_run` lists eligible rows without
    calling the LLM or writing anything."""
    rows = _eligible_rows(conn, newer_than_days=newer_than_days)
    if limit is not None:
        rows = rows[:limit]

    results: list[PromoteResult] = []
    for row in rows:
        mid = row["message_id"]
        subject = row["subject"] or ""
        if dry_run:
            results.append(PromoteResult(mid, subject, "would extract (dry run)"))
            continue

        roles = extract_roles_llm_cached(conn, _message_from_row(row), model=model, client=client)
        targets: list[tuple] = []
        seen_keys: set[str] = set()
        for role in sorted(roles, key=lambda r: r.confidence, reverse=True):
            outcome = outcome_from_role(conn, role, source="llm")
            if outcome is None or not (outcome.matched or outcome.is_new_lead_candidate):
                continue
            if outcome.job_key and outcome.job_key in seen_keys:
                continue
            if outcome.job_key:
                seen_keys.add(outcome.job_key)
            targets.append((role, outcome.job_key))

        if not targets:
            results.append(PromoteResult(mid, subject, "still unmatched (no usable company + title)"))
            continue

        # Resolve the highest-confidence role last so it ends up as the
        # row's resolved_job_key; every role still gets its own conversation.
        job_keys = [
            promote_unmatched_row(conn, row, role, job_key=job_key) for role, job_key in reversed(targets)
        ]
        job_keys.reverse()
        results.append(PromoteResult(mid, subject, f"promoted to {len(job_keys)} lead(s)", job_keys))
    return results
