"""Dismiss / marked replied — DB side for pending-actions LinkedIn cards."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.store import (
    add_job_conversation,
    connect,
    dismiss_linkedin_reply,
    list_job_conversations,
    list_unmatched_messages,
    record_unmatched_message,
    upsert_lead,
)


def test_dismiss_unmatched_hides_from_list(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="imap:evan-1",
            direction="inbound",
            from_address="inmail-hit-reply@linkedin.com",
            subject="AI Engineer Job Opportunity",
            body_text="Hey Shawn…",
        ),
    )
    assert len(list_unmatched_messages(conn)) == 1
    dismiss_linkedin_reply(conn, kind="unmatched", message_id="imap:evan-1")
    assert list_unmatched_messages(conn) == []
    assert len(list_unmatched_messages(conn, include_dismissed=True)) == 1
    conn.close()


def test_dismiss_lead_adds_outbound_conversation(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="KPG99,INC",
        title="LLM Data Engineer",
        source_message_id="imap:madh-1",
        source_label="linkedin_message",
        jd_text="Position: LLM Data Engineer",
    )
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="imap:madh-1",
            channel="linkedin",
            direction="inbound",
            summary="Pitch",
            body_text="Position: LLM Data Engineer",
        ),
    )
    dismiss_linkedin_reply(
        conn,
        kind="lead",
        normalized_key=lead.normalized_key,
        message_id="imap:madh-1",
    )
    convs = list_job_conversations(conn, lead.normalized_key)
    assert any(c["direction"] == "outbound" and "Marked replied" in (c["summary"] or "") for c in convs)
    # idempotent
    dismiss_linkedin_reply(conn, kind="lead", normalized_key=lead.normalized_key, message_id="imap:madh-1")
    outbound = [c for c in list_job_conversations(conn, lead.normalized_key) if c["direction"] == "outbound"]
    assert len(outbound) == 1
    conn.close()


def test_dismiss_lead_also_dismisses_matching_unmatched(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="Acme",
        title="SWE",
        source_message_id="msg-1",
        source_label="linkedin_message",
    )
    upsert_lead(conn, lead)
    record_unmatched_message(
        conn,
        UnmatchedMessage(message_id="msg-1", direction="inbound", subject="Hi", body_text="x"),
    )
    dismiss_linkedin_reply(conn, kind="lead", normalized_key=lead.normalized_key, message_id="msg-1")
    assert list_unmatched_messages(conn) == []
    conn.close()


def test_dismiss_unmatched_missing_is_idempotent_noop(tmp_path: Path):
    """HTML/URL corruption used to surface 'no unmatched_messages row for
    message_id=…' alerts — dismiss must not hard-fail when the row is gone."""
    conn = connect(tmp_path / "leads.db")
    result = dismiss_linkedin_reply(conn, kind="unmatched", message_id="does-not-exist")
    assert result.startswith("unmatched-missing:")
    conn.close()


def test_dismiss_unmatched_falls_back_to_lead_via_conversation(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = JobLead(
        company="KPG99,INC",
        title="LLM Data Engineer",
        source_message_id="imap:<123@host>",
        source_label="linkedin_message",
    )
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id="imap:<123@host>",
            channel="linkedin",
            direction="inbound",
            summary="Pitch",
        ),
    )
    result = dismiss_linkedin_reply(conn, kind="unmatched", message_id="imap:<123@host>")
    assert result.startswith("lead:")
    convs = list_job_conversations(conn, lead.normalized_key)
    assert any(c["direction"] == "outbound" for c in convs)
    conn.close()
