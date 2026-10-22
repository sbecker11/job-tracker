"""One recruiter message is logged once, even when Gmail and IMAP both read it."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead

KEY = "direct client::lead python full stack engineer"
SUBJECT = "Message replied:  Position for a Lead AI Python Fullstack Remote with Direct client"
GMAIL_BODY = SUBJECT + " Hi Shawn rate on this is $80/hr on w2 max , Remote no Travel  View message"
IMAP_BODY = SUBJECT.replace(" with", "\r\n with") + "\nHi Shawn rate on this is $80/hr on w2 max ,\r\nRemote no Travel\n\nView message"


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(conn, JobLead(company="Direct client", title="Lead Python Full Stack Engineer", source_message_id="m1", source_label="single-jd"))
    return conn


def _inbound(message_id: str, body: str, when: str, summary: str = SUBJECT) -> JobConversation:
    return JobConversation(
        job_key=KEY, message_id=message_id, channel="linkedin", direction="inbound",
        summary=summary, body_text=body, occurred_at=when,
    )


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM job_conversations WHERE job_key = ?", (KEY,)).fetchone()[0]


def test_gmail_and_imap_copies_are_logged_once(tmp_path: Path):
    conn = _conn(tmp_path)
    first = add_job_conversation(conn, _inbound("1a1600ed2857b084", GMAIL_BODY, "2026-10-21T20:01:00+00:00"))
    second = add_job_conversation(conn, _inbound("imap:<1378@linkedin.com>", IMAP_BODY, "2026-10-21T20:09:00+00:00"))
    assert second == first
    assert _count(conn) == 1


def test_same_id_twice_is_logged_once(tmp_path: Path):
    conn = _conn(tmp_path)
    add_job_conversation(conn, _inbound("abc", GMAIL_BODY, "2026-10-21T20:01:00+00:00"))
    add_job_conversation(conn, _inbound("abc", GMAIL_BODY, "2026-10-22T09:00:00+00:00"))
    assert _count(conn) == 1


def test_late_copy_does_not_undo_your_reply(tmp_path: Path):
    conn = _conn(tmp_path)
    add_job_conversation(conn, _inbound("g1", GMAIL_BODY, "2026-10-21T20:01:00+00:00"))
    add_job_conversation(conn, JobConversation(job_key=KEY, direction="outbound", summary="Re: rate", occurred_at="2026-10-21T20:30:00+00:00"))
    add_job_conversation(conn, _inbound("imap:<x>", IMAP_BODY, "2026-10-21T20:40:00+00:00"))
    waiting = conn.execute("SELECT awaiting_response_since FROM job_leads WHERE normalized_key = ?", (KEY,)).fetchone()[0]
    assert waiting == "2026-10-21T20:30:00+00:00"


def test_different_messages_in_one_thread_are_both_logged(tmp_path: Path):
    conn = _conn(tmp_path)
    add_job_conversation(conn, _inbound("g1", GMAIL_BODY, "2026-10-21T20:01:00+00:00"))
    add_job_conversation(conn, _inbound("g2", SUBJECT + " Also, can you share two references and a good time for a call tomorrow?", "2026-10-21T20:05:00+00:00"))
    add_job_conversation(conn, _inbound("g3", GMAIL_BODY, "2026-10-22T02:00:00+00:00"))
    assert _count(conn) == 3
