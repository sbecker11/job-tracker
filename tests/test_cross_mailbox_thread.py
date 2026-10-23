"""MAT-4: Gmail and IMAP copies of one thread resolve to the same lead.

The two mailboxes give one thread different thread ids, so matching also
uses the RFC 822 Message-Id / References / In-Reply-To headers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from job_tracker.email.gmail_reader import parse_gmail_message
from job_tracker.email.imap_reader import parse_rfc822_message
from job_tracker.email.models import EmailMessage, normalize_rfc_id, rfc_id_list
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.models import JobConversation, JobLead, normalize_key
from job_tracker.pipeline.store import (
    add_job_conversation,
    connect,
    find_job_by_rfc_ids,
    upsert_lead,
)

ACME = ("Acme Health", "Senior Data Engineer")
GLOBEX = ("Globex", "Staff Software Engineer")


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(tmp_path / "leads.db")
    for company, title in (ACME, GLOBEX):
        upsert_lead(c, JobLead(company=company, title=title, source_message_id=f"src-{company}", source_label="email"))
    yield c
    c.close()


def _email(**kw) -> EmailMessage:
    base = dict(id="g1", from_address="nobody@unknown.example", subject="Re: role", body_plain="hi")
    base.update(kw)
    return EmailMessage(**base)


def test_normalize_and_list_ids():
    assert normalize_rfc_id(" abc@x ") == "<abc@x>"
    assert normalize_rfc_id("") == ""
    assert rfc_id_list("<a@x> <b@x>", "<b@x>") == ["<a@x>", "<b@x>"]


def test_gmail_reader_reads_message_id_headers():
    data = base64.urlsafe_b64encode(b"hello").decode()
    raw = {
        "id": "18f",
        "threadId": "18e",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": data},
            "headers": [
                {"name": "Message-ID", "value": "<reply@mail.example>"},
                {"name": "References", "value": "<first@mail.example>"},
                {"name": "In-Reply-To", "value": "<first@mail.example>"},
                {"name": "Subject", "value": "Re: role"},
            ],
        },
    }
    msg = parse_gmail_message(raw)
    assert msg.rfc_message_id == "<reply@mail.example>"
    assert msg.references == ["<first@mail.example>"]


def test_imap_reader_reads_in_reply_to():
    raw = (
        b"Message-Id: <reply@mail.example>\r\n"
        b"In-Reply-To: <first@mail.example>\r\n"
        b"Subject: Re: role\r\n\r\nhello\r\n"
    )
    msg = parse_rfc822_message(raw, uid="7", folder="INBOX")
    assert msg.rfc_message_id == "<reply@mail.example>"
    assert msg.references == ["<first@mail.example>"]


def test_imap_reply_matches_lead_logged_from_gmail(conn):
    key = normalize_key(*ACME)
    add_job_conversation(
        conn,
        JobConversation(job_key=key, message_id="18f", thread_id="gmail-thread", rfc_message_id="<first@mail.example>"),
    )
    reply = _email(
        id="imap:<reply@mail.example>",
        thread_id="imap:<first@mail.example>",
        rfc_message_id="<reply@mail.example>",
        references=["<first@mail.example>"],
    )
    outcome = match_message_to_job(conn, reply)
    assert outcome.job_key == key
    assert outcome.tier == "message_id_ref"


def test_gmail_reply_matches_lead_logged_from_imap_before_column_existed(conn):
    key = normalize_key(*ACME)
    conn.execute(
        "INSERT INTO job_conversations (job_key, message_id, direction, thread_id, occurred_at) "
        "VALUES (?, 'imap:<first@mail.example>', 'inbound', 'imap:<first@mail.example>', '2026-10-01T00:00:00Z')",
        (key,),
    )
    reply = _email(id="18f", thread_id="gmail-thread", rfc_message_id="<r@x>", references=["<first@mail.example>"])
    assert find_job_by_rfc_ids(conn, reply.rfc_message_id, reply.references) == key


def test_ids_pointing_at_two_leads_do_not_match(conn):
    add_job_conversation(conn, JobConversation(job_key=normalize_key(*ACME), rfc_message_id="<a@x>"))
    add_job_conversation(conn, JobConversation(job_key=normalize_key(*GLOBEX), rfc_message_id="<b@x>"))
    assert find_job_by_rfc_ids(conn, "<c@x>", ["<a@x>", "<b@x>"]) is None
    assert find_job_by_rfc_ids(conn, "", []) is None


def test_same_email_from_other_mailbox_is_not_logged_twice(conn):
    key = normalize_key(*ACME)
    first = add_job_conversation(
        conn, JobConversation(job_key=key, message_id="18f", rfc_message_id="<one@x>", summary="A", body_text="x")
    )
    again = add_job_conversation(
        conn, JobConversation(job_key=key, message_id="imap:<one@x>", summary="B", body_text="different text")
    )
    assert again == first
    assert conn.execute("SELECT count(*) FROM job_conversations").fetchone()[0] == 1
