"""card_audit: each check fires on the bad card state and stays quiet on a good one."""

from __future__ import annotations

import sqlite3

from job_tracker.pipeline.card_audit import audit_cards, summarize

THREAD = "https://www.linkedin.com/messaging/thread/2-abc==/"


def _checks(cards: list[dict], conn: sqlite3.Connection | None = None) -> set[str]:
    return {i.check for i in audit_cards({"stages": {"clarify": cards}}, conn)}


def _conn(rows: list[tuple[str, str, str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE job_conversations (job_key TEXT, direction TEXT, occurred_at TEXT, body_text TEXT, summary TEXT)")
    conn.executemany(
        "INSERT INTO job_conversations (job_key, direction, occurred_at, body_text) VALUES (?, ?, ?, ?)", rows
    )
    return conn


def test_good_linkedin_card_is_clean():
    card = {"id": "a", "channel": "linkedin", "threadUrl": THREAD, "recruiterName": "Carlson Nkwain", "draftReply": "Hi Carlson,\n"}
    assert _checks([card]) == set()


def test_linkedin_message_on_email_card_without_real_email():
    card = {"id": "a", "channel": "email", "threadUrl": THREAD, "recruiterEmail": "jobs@my.theladders.com"}
    assert "email_on_linkedin" in _checks([card])
    # A real recruiter inbox makes email a fair channel.
    assert "email_on_linkedin" not in _checks([{**card, "recruiterEmail": "tanmay.v@diverselynx.com"}])


def test_missing_thread_and_nameless_greeting():
    card = {"id": "a", "channel": "linkedin", "messageBody": f"Reply\n{THREAD}", "draftReply": "Hi,\n\nThanks"}
    assert {"missing_thread", "nameless_greeting"} <= _checks([card])


def test_email_as_name_and_bad_email():
    card = {"id": "a", "recruiterName": "aman@pinnacle99inc.com", "recruiterEmail": "linkedin.com/in/someone"}
    assert {"email_as_name", "bad_email"} <= _checks([card])
    assert "email_as_name" not in _checks([{"id": "b", "recruiterName": "jobs@my.theladders.com"}])


def test_text_to_office_line():
    card = {"id": "a", "recruiterPhone": "973-295-4571", "messageBody": "Priyanshu\nOffice: 973-295-4571\n"}
    assert "text_to_office_line" in _checks([card])
    assert "text_to_office_line" not in _checks([{**card, "messageBody": "Mobile: 973-295-4571"}])


def test_followup_draft_checks_use_the_conversation():
    conn = _conn(
        [
            ("k", "inbound", "2026-10-12T18:11:03+00:00", "Lead Python role"),
            ("k", "outbound", "2026-10-21T18:59:00+00:00", "A few quick clarifiers"),
            ("k", "inbound", "2026-10-21T20:01:00+00:00", "rate is $80/hr on w2 max, Remote no Travel"),
            ("k", "inbound", "2026-10-21T20:01:00", "rate is $80/hr on w2 max, Remote no Travel"),
        ]
    )
    card = {
        "id": "a",
        "normalizedKey": "k",
        "stage": "clarify",
        "channel": "linkedin",
        "threadUrl": THREAD,
        "recruiterName": "Rajneesh Bhardwaj",
        "draftReply": "Hi Rajneesh,\n\nThanks for reaching out.\n- Is the role fully remote (US)?\n- Is the engagement W2 or 1099?",
    }
    issues = audit_cards({"stages": {"clarify": [card]}}, conn)
    assert {i.check for i in issues} == {"reasks_answered", "first_touch_on_followup", "duplicate_inbound"}
    reask = next(i for i in issues if i.check == "reasks_answered")
    assert reask.detail == "remote, engagement"
    assert "Rajneesh Bhardwaj" in summarize(issues)


def test_card_listed_in_two_stages_is_checked_once():
    card = {"id": "a", "recruiterName": "x@y.com"}
    issues = audit_cards({"stages": {"clarify": [card], "wait_schedule": [card]}})
    assert len(issues) == 1
