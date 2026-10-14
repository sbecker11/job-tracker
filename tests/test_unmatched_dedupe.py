"""One open unmatched row per conversation (2026-10-14): the Gmail-API and
IMAP copies of one LinkedIn notification, follow-ups in the same LinkedIn
conversation, the `linkedin_thread` match tier, and the dedupe CLI."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_tracker.cli.dedupe_unmatched import dedupe_unmatched, main as dedupe_main
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.models import JobConversation, JobLead, UnmatchedMessage, normalize_key
from job_tracker.pipeline.store import (
    add_job_conversation,
    connect,
    dismiss_unmatched_message,
    find_job_by_linkedin_thread,
    get_unmatched_message,
    linkedin_thread_key,
    list_unmatched_messages,
    record_unmatched_message,
    resolve_unmatched_message,
    upsert_lead,
)

RELAY = "inmail-hit-reply@linkedin.com"
THREAD_A = "2-NDRmZmM3ZjItMjk2Ni00Yjg3LTlhNzgtZGEwZTFlNWZhMWY0XzEwMA=="
THREAD_B = "2-ZWRkNTU0YWQtMjUzMi00YzAwLTg3"
CLEVANOO = normalize_key("Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
HYATT = normalize_key("Hyatt", "Machine Learning Engineer")


def _body(thread: str, text: str = "Hi Shawn, I came across your profile.") -> str:
    link = f"https://www.linkedin.com/messaging/thread/{thread}/" if thread else ""
    return f"Carlin Brown\nReply\n{link}\n\n{text}"


def _park(conn, mid: str, *, subject: str, thread: str = THREAD_A, at: str, **kw) -> None:
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id=mid,
            thread_id=mid,
            direction="inbound",
            from_address=kw.get("from_address", RELAY),
            subject=subject,
            body_text=_body(thread, kw.get("text", "Hi Shawn, I came across your profile.")),
            detected_at=at,
        ),
    )


def _open_ids(conn) -> list[str]:
    return sorted(r["message_id"] for r in list_unmatched_messages(conn))


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "leads.db")
    for company, title, mid in (
        ("Clevanoo LLC", "Senior Full Stack AI/ML Engineer", "m0"),
        ("Hyatt", "Machine Learning Engineer", "m1"),
    ):
        upsert_lead(c, JobLead(company=company, title=title, source_message_id=mid, source_label="linkedin_message"))
    yield c
    c.close()


def test_linkedin_thread_key_extracts_conversation_id():
    assert linkedin_thread_key(_body(THREAD_A)) == THREAD_A
    assert linkedin_thread_key("no link here") == ""
    assert linkedin_thread_key("") == ""


def test_gmail_and_imap_copies_collapse_to_newest(conn):
    _park(conn, "1a13b2ce7bcc629b", subject="Senior/ Lead SWE", at="2026-10-14T16:10:21+00:00")
    _park(conn, "imap:<117904588@linkedin.com>", subject="Senior/ Lead SWE", at="2026-10-14T16:10:27+00:00")

    assert _open_ids(conn) == ["imap:<117904588@linkedin.com>"]
    assert get_unmatched_message(conn, "1a13b2ce7bcc629b")["superseded_by"] == "imap:<117904588@linkedin.com>"


def test_copies_without_linkedin_link_collapse_on_sender_and_subject(conn):
    _park(conn, "g1", subject="Robert Bosch GmbH Recruitment Plan", thread="", at="2026-09-22T23:03:54+00:00")
    _park(conn, "imap:<i1>", subject="Robert Bosch GmbH Recruitment Plan", thread="", at="2026-09-22T23:07:20+00:00")

    assert _open_ids(conn) == ["imap:<i1>"]


def test_same_subject_hours_apart_without_link_stays_separate(conn):
    _park(conn, "g1", subject="Hi, Shawn", thread="", at="2026-09-19T08:00:00+00:00")
    _park(conn, "g2", subject="Hi, Shawn", thread="", at="2026-09-19T18:00:00+00:00")

    assert _open_ids(conn) == ["g1", "g2"]


def test_follow_up_in_same_linkedin_conversation_supersedes_older(conn):
    _park(conn, "g1", subject="Remote Senior Data Engineer", at="2026-08-01T10:00:00+00:00")
    _park(conn, "g2", subject="Message replied: Remote Senior Data Engineer", at="2026-08-05T10:00:00+00:00")
    _park(conn, "g3", subject="Message replied: Remote Senior Data Engineer", at="2026-08-09T10:00:00+00:00")

    assert _open_ids(conn) == ["g3"]
    assert get_unmatched_message(conn, "g1")["superseded_by"] == "g3"
    assert get_unmatched_message(conn, "g2")["superseded_by"] == "g3"


def test_different_conversations_from_shared_relay_stay_separate(conn):
    _park(conn, "g1", subject="Data Engineer", thread=THREAD_A, at="2026-10-01T10:00:00+00:00")
    _park(conn, "g2", subject="ML Engineer", thread=THREAD_B, at="2026-10-01T10:01:00+00:00")

    assert _open_ids(conn) == ["g1", "g2"]


def test_second_copy_of_dismissed_email_stays_dismissed(conn):
    _park(conn, "g1", subject="Snowflake Lead Developer", at="2026-10-09T22:15:00+00:00")
    dismiss_unmatched_message(conn, "g1")
    _park(conn, "imap:<i1>", subject="Snowflake Lead Developer", at="2026-10-09T23:30:00+00:00")

    assert _open_ids(conn) == []
    assert get_unmatched_message(conn, "imap:<i1>")["dismissed_at"]


def test_new_reply_after_dismissal_reopens(conn):
    _park(conn, "g1", subject="Data Engineer", at="2026-10-01T10:00:00+00:00")
    dismiss_unmatched_message(conn, "g1")
    _park(conn, "g2", subject="Message replied: Data Engineer", at="2026-10-06T10:00:00+00:00")

    assert _open_ids(conn) == ["g2"]


def test_dismiss_and_resolve_cascade_to_superseded_rows(conn):
    _park(conn, "g1", subject="Data Engineer", at="2026-10-01T10:00:00+00:00")
    _park(conn, "imap:<i1>", subject="Data Engineer", at="2026-10-01T10:00:05+00:00")
    dismiss_unmatched_message(conn, "imap:<i1>")
    assert get_unmatched_message(conn, "g1")["dismissed_at"]

    _park(conn, "g2", subject="ML Engineer", thread=THREAD_B, at="2026-10-02T10:00:00+00:00")
    _park(conn, "imap:<i2>", subject="ML Engineer", thread=THREAD_B, at="2026-10-02T10:00:05+00:00")
    resolve_unmatched_message(conn, "imap:<i2>", CLEVANOO)
    assert get_unmatched_message(conn, "g2")["resolved_job_key"] == CLEVANOO
    assert conn.execute("SELECT COUNT(*) FROM job_conversations").fetchone()[0] == 1


def test_find_job_by_linkedin_thread_requires_a_single_lead(conn):
    add_job_conversation(conn, JobConversation(job_key=CLEVANOO, direction="inbound", summary="x", body_text=_body(THREAD_A)))
    assert find_job_by_linkedin_thread(conn, THREAD_A) == CLEVANOO
    assert find_job_by_linkedin_thread(conn, THREAD_B) is None
    assert find_job_by_linkedin_thread(conn, "") is None

    add_job_conversation(conn, JobConversation(job_key=HYATT, direction="inbound", summary="y", body_text=_body(THREAD_A)))
    assert find_job_by_linkedin_thread(conn, THREAD_A) is None


def test_match_tier_linkedin_thread_links_follow_up_inmail(conn):
    add_job_conversation(conn, JobConversation(job_key=CLEVANOO, direction="inbound", summary="x", body_text=_body(THREAD_A)))
    message = EmailMessage(
        id="new-1",
        from_address=RELAY,
        subject="Message replied: Senior Full Stack",
        body_plain=_body(THREAD_A, "Following up on my note."),
        thread_id="fresh-email-thread",
    )

    outcome = match_message_to_job(conn, message, direction="inbound")
    assert outcome.job_key == CLEVANOO
    assert outcome.tier == "linkedin_thread"


def _park_raw(conn, mid: str, *, subject: str, thread: str, at: str) -> None:
    """Insert without intake collapsing, like rows parked before 2026-10-14."""
    conn.execute(
        "INSERT INTO unmatched_messages (message_id, direction, from_address, subject, body_text, detected_at) "
        "VALUES (?, 'inbound', ?, ?, ?, ?)",
        (mid, RELAY, subject, _body(thread), at),
    )
    conn.commit()


def test_dedupe_cli_collapses_resolves_and_dismisses_quiet(conn):
    _park_raw(conn, "a1", subject="Data Engineer", thread=THREAD_A, at="2026-10-10T10:00:00+00:00")
    _park_raw(conn, "imap:<a2>", subject="Data Engineer", thread=THREAD_A, at="2026-10-10T10:00:05+00:00")
    _park_raw(conn, "b1", subject="ML Engineer", thread=THREAD_B, at="2026-08-01T10:00:00+00:00")
    _park_raw(conn, "imap:<b2>", subject="ML Engineer", thread=THREAD_B, at="2026-08-01T10:00:05+00:00")
    _park_raw(conn, "c1", subject="Java Developer", thread="", at="2026-10-12T10:00:00+00:00")
    add_job_conversation(conn, JobConversation(job_key=HYATT, direction="inbound", summary="x", body_text=_body(THREAD_A)))
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    dry = dedupe_unmatched(conn, dismiss_quiet_days=21, dry_run=True, now=now)
    assert dry == {
        "open_before": 5,
        "conversations": 3,
        "superseded": 2,
        "resolved": 1,
        "dismissed": 1,
        "open_after": 1,
    }
    assert len(list_unmatched_messages(conn)) == 5

    dedupe_unmatched(conn, dismiss_quiet_days=21, now=now)
    assert _open_ids(conn) == ["c1"]
    assert get_unmatched_message(conn, "a1")["resolved_job_key"] == HYATT
    assert get_unmatched_message(conn, "b1")["dismissed_at"]

    again = dedupe_unmatched(conn, dismiss_quiet_days=21, now=now)
    assert again["superseded"] == again["resolved"] == again["dismissed"] == 0


def test_dedupe_cli_main_dry_run_prints_summary(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    c = connect(db)
    _park_raw(c, "a1", subject="Data Engineer", thread=THREAD_A, at="2026-10-10T10:00:00+00:00")
    _park_raw(c, "imap:<a2>", subject="Data Engineer", thread=THREAD_A, at="2026-10-10T10:00:05+00:00")
    c.close()

    assert dedupe_main(["--db", str(db), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] 2 open unmatched row(s) → 1 conversation(s)" in out
    c = connect(db)
    assert len(list_unmatched_messages(c)) == 2
    c.close()
