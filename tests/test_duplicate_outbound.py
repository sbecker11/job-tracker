"""One sent reply is logged once (REQUIREMENTS LOG-4)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import MARKED_SENT_PREFIX, add_job_conversation, connect, upsert_lead

KEY = "artech::software engineer"
DRAFT = "Hi Priyanshu,\n\nThanks for reaching out on the Software Engineer role.\n\nBest, Shawn\nshawn.becker@spexture.com"
SENT = "RE: Artech Software Engineer " + DRAFT.replace("\n", " ") + " On Oct 21, 2026, Priyanshu wrote: > Hi Shawn ..."


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(conn, JobLead(company="Artech", title="Software Engineer", source_message_id="m1", source_label="single-jd"))
    return conn


def _outbound(message_id: str, body: str, when: str) -> JobConversation:
    return JobConversation(
        job_key=KEY, message_id=message_id, channel="email", direction="outbound",
        summary="RE: Artech Software Engineer", body_text=body, occurred_at=when,
    )


def _rows(conn):
    return conn.execute(
        "SELECT message_id, body_text FROM job_conversations WHERE job_key = ? AND direction = 'outbound'", (KEY,)
    ).fetchall()


def test_one_send_copied_into_two_mailboxes_is_logged_once(tmp_path: Path):
    conn = _conn(tmp_path)
    first = add_job_conversation(conn, _outbound("19ff66ff674e93e6", SENT, "2026-10-22T15:13:46+00:00"))
    second = add_job_conversation(conn, _outbound("19ff66f987d8eb73", SENT, "2026-10-22T15:13:47+00:00"))
    assert second == first and len(_rows(conn)) == 1


def test_marked_sent_row_takes_the_gmail_copy_id_and_body(tmp_path: Path):
    conn = _conn(tmp_path)
    marked = f"{MARKED_SENT_PREFIX}{KEY}:2026-10-22T15:10:00+00:00"
    first = add_job_conversation(conn, _outbound(marked, DRAFT, "2026-10-22T15:10:00+00:00"))
    second = add_job_conversation(conn, _outbound("1a1642c133a44756", SENT, "2026-10-22T15:13:46+00:00"))
    assert second == first
    assert [tuple(r) for r in _rows(conn)] == [("1a1642c133a44756", SENT)]


def test_marked_sent_after_the_gmail_copy_is_not_logged_again(tmp_path: Path):
    conn = _conn(tmp_path)
    first = add_job_conversation(conn, _outbound("1a1642c133a44756", SENT, "2026-10-22T15:13:46+00:00"))
    marked = f"{MARKED_SENT_PREFIX}{KEY}:2026-10-22T15:20:00+00:00"
    assert add_job_conversation(conn, _outbound(marked, DRAFT, "2026-10-22T15:20:00+00:00")) == first
    assert [r["message_id"] for r in _rows(conn)] == ["1a1642c133a44756"]


def test_two_different_replies_minutes_apart_are_both_logged(tmp_path: Path):
    conn = _conn(tmp_path)
    add_job_conversation(conn, _outbound("a1", "I confirm the rate. Attached is the RTR. > quoted thread", "2026-08-12T17:18:21+00:00"))
    add_job_conversation(conn, _outbound("a2", "Who is the client for this role? > quoted thread", "2026-08-12T17:28:54+00:00"))
    assert len(_rows(conn)) == 2
