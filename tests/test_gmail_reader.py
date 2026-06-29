"""Offline tests for Gmail message parsing (no API calls)."""

from __future__ import annotations

import base64

from job_tracker.email.gmail_reader import build_query, parse_gmail_message


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _sample_message(*, plain: str = "", html: str = "", subject: str = "Hello") -> dict:
    parts = []
    if plain:
        parts.append(
            {
                "mimeType": "text/plain",
                "body": {"data": _b64(plain)},
            }
        )
    if html:
        parts.append(
            {
                "mimeType": "text/html",
                "body": {"data": _b64(html)},
            }
        )
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "From", "value": "Recruiter <recruiter@example.com>"},
            {"name": "To", "value": "shawnbecker.recruiting@gmail.com"},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Sun, 28 Jun 2026 10:00:00 -0400"},
        ],
        "parts": parts,
    }
    return {
        "id": "msg-123",
        "threadId": "thread-456",
        "snippet": "Preview text",
        "labelIds": ["UNREAD", "INBOX"],
        "payload": payload,
    }


def test_parse_plain_body():
    raw = _sample_message(plain="Apply here: https://boards.greenhouse.io/stripe/jobs/1")
    message = parse_gmail_message(raw)
    assert message.id == "msg-123"
    assert message.from_address == "recruiter@example.com"
    assert "greenhouse.io" in message.body_plain
    assert message.label_ids == ["UNREAD", "INBOX"]


def test_parse_html_fallback_when_no_plain():
    raw = _sample_message(html="<p>Senior <b>Software Engineer</b></p>")
    message = parse_gmail_message(raw)
    assert "Senior Software Engineer" in message.body_plain
    assert message.body_html


def test_build_query_appends_newer_than():
    assert build_query("is:unread in:inbox", newer_than_days=7) == "is:unread in:inbox newer_than:7d"
