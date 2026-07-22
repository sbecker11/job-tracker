"""Tests for the plain-IMAP reader (job_tracker.email.imap_reader)."""

from __future__ import annotations

import pytest

from job_tracker.email.imap_reader import ImapAccount, parse_rfc822_message


def test_imap_account_from_env_reads_prefixed_vars(monkeypatch):
    monkeypatch.setenv("SPEXTURE_IMAP_HOST", "imap.hostinger.com")
    monkeypatch.setenv("SPEXTURE_IMAP_PORT", "993")
    monkeypatch.setenv("SPEXTURE_IMAP_USER", "shawn.becker@spexture.com")
    monkeypatch.setenv("SPEXTURE_IMAP_PASSWORD", "secret")

    account = ImapAccount.from_env("SPEXTURE")
    assert account.host == "imap.hostinger.com"
    assert account.port == 993
    assert account.user == "shawn.becker@spexture.com"
    assert account.password == "secret"  # pragma: allowlist secret


def test_imap_account_from_env_missing_raises(monkeypatch):
    monkeypatch.delenv("SPEXTURE_IMAP_HOST", raising=False)
    monkeypatch.delenv("SPEXTURE_IMAP_USER", raising=False)
    monkeypatch.delenv("SPEXTURE_IMAP_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="SPEXTURE_IMAP"):
        ImapAccount.from_env("SPEXTURE")


_RAW_PLAIN_MESSAGE = (
    b"From: Cole Keener <cole@crbworkforce.com>\r\n"
    b"To: shawn.becker@spexture.com\r\n"
    b"Subject: Remote Senior Data Engineer - DIRECTV\r\n"
    b"Message-Id: <abc123@crbworkforce.com>\r\n"
    b"References: <parent-thread@crbworkforce.com>\r\n"
    b"Date: Tue, 22 Jul 2026 10:00:00 -0600\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Here's the JD, let me know if you're interested.\r\n"
)


def test_parse_rfc822_message_maps_fields_and_namespaces_id():
    message = parse_rfc822_message(_RAW_PLAIN_MESSAGE, uid="42", folder="INBOX")
    assert message.id == "imap:<abc123@crbworkforce.com>"
    assert message.thread_id == "imap:<parent-thread@crbworkforce.com>"
    assert message.from_address == "cole@crbworkforce.com"
    assert message.to_address == "shawn.becker@spexture.com"
    assert message.subject == "Remote Senior Data Engineer - DIRECTV"
    assert "let me know if you're interested" in message.body_plain
    assert message.label_ids == ["INBOX"]


def test_parse_rfc822_message_falls_back_to_uid_when_no_message_id():
    raw = (
        b"From: someone@example.com\r\n"
        b"To: shawn.becker@spexture.com\r\n"
        b"Subject: No Message-Id here\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body text.\r\n"
    )
    message = parse_rfc822_message(raw, uid="7", folder="INBOX")
    assert message.id == "imap-uid:INBOX:7"
    assert message.thread_id == ""


def test_parse_rfc822_message_never_collides_with_gmail_hex_ids():
    """IMAP ids are always prefixed `imap:`/`imap-uid:` — see module
    docstring — so they can never collide with Gmail's hex message ids in
    the shared `processed_messages`/`job_conversations` tables."""
    message = parse_rfc822_message(_RAW_PLAIN_MESSAGE, uid="1", folder="INBOX")
    assert message.id.startswith("imap:")
