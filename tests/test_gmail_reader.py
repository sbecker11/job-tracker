"""Offline tests for Gmail message parsing (no API calls)."""

from __future__ import annotations

import base64

from job_tracker.email.gmail_reader import (
    CONFIG_DIR,
    build_query,
    default_credentials_path,
    default_token_path,
    parse_gmail_message,
)


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


def test_default_credentials_path_no_account_uses_config_root(monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_GMAIL_CREDENTIALS", raising=False)
    assert default_credentials_path() == CONFIG_DIR / "credentials.json"


def test_default_credentials_path_with_account_uses_subdirectory(monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_GMAIL_CREDENTIALS", raising=False)
    assert default_credentials_path("personal_hub") == CONFIG_DIR / "personal_hub" / "credentials.json"


def test_default_token_path_with_account_uses_subdirectory(monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_GMAIL_TOKEN", raising=False)
    assert default_token_path("personal_hub") == CONFIG_DIR / "personal_hub" / "token.json"


def test_env_override_ignored_when_account_is_set(monkeypatch):
    # The env var override is only for the default (no-account) recruiting
    # funnel path — an explicit account must not be silently redirected by a
    # leftover env var meant for the default account.
    monkeypatch.setenv("JOB_TRACKER_GMAIL_CREDENTIALS", "/tmp/should-not-be-used.json")
    assert default_credentials_path("personal_hub") == CONFIG_DIR / "personal_hub" / "credentials.json"
    monkeypatch.delenv("JOB_TRACKER_GMAIL_CREDENTIALS", raising=False)


def test_html_fallback_strips_style_and_script_blocks():
    """Regression: HTML-only marketing mail (e.g. CareerBuilder) embeds a <style>
    block with raw CSS before any real content; it must not leak into body_plain."""
    html = (
        "<style>p, a, td { mso-line-height-rule: exactly; } "
        ".ExternalClass { width: 100%; }</style>"
        "<script>trackOpen('abc123');</script>"
        "<p>New role: Senior Software Engineer at Acme Corp</p>"
    )
    raw = _sample_message(html=html)
    message = parse_gmail_message(raw)
    assert "mso-line-height-rule" not in message.body_plain
    assert "trackOpen" not in message.body_plain
    assert "Senior Software Engineer at Acme Corp" in message.body_plain
