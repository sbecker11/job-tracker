"""IMAP reader for non-Gmail mailboxes.

Added 2026-07-22 after a real gap: `shawn.becker@spexture.com` — the email
address on Shawn's résumé header — is Hostinger-hosted (plain IMAP), not
Gmail/Google Workspace. Cole Keener (DIRECTV recruiter)'s LinkedIn replies
and direct emails landed there and NOT in `shawnbecker.recruiting@gmail.com`,
which is the only mailbox the Gmail-API-based automation
(`gmail_reader.py`/`gmail_writer.py`) can see. One message was confirmed to
have reached `spexture.com` and never reached the recruiting Gmail account
at all — a genuine, silent delivery gap, not just a processing backlog.

This module mirrors `gmail_reader.py`'s `EmailMessage`-producing interface
closely enough that triage/extraction/comms-match code downstream can
consume either source interchangeably, but stays purely IMAP: no OAuth, no
Gmail labels — just a username/password login (`IMAP4_SSL`) and IMAP folders
as the label/archive equivalent.

Credentials come from the environment (populated from the shared `.env`,
see `job_tracker/__init__.py`'s env loader) — `<PREFIX>_IMAP_HOST/PORT/USER/
PASSWORD`. Never hardcode or log a password; `ImapAccount.from_env` reads it
but nothing in this module ever prints `account.password`.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import os
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message as EmailLibMessage
from email.utils import parseaddr

from job_tracker.email.models import EmailMessage
from job_tracker.htmltext import html_to_text

DEFAULT_PORT = 993


@dataclass
class ImapAccount:
    host: str
    port: int
    user: str
    password: str  # pragma: allowlist secret

    @classmethod
    def from_env(cls, prefix: str) -> "ImapAccount":
        """`prefix` e.g. "SPEXTURE" reads SPEXTURE_IMAP_HOST/PORT/USER/PASSWORD."""
        host = os.environ.get(f"{prefix}_IMAP_HOST", "")
        port_raw = os.environ.get(f"{prefix}_IMAP_PORT", "") or str(DEFAULT_PORT)
        user = os.environ.get(f"{prefix}_IMAP_USER", "")
        password = os.environ.get(f"{prefix}_IMAP_PASSWORD", "")  # pragma: allowlist secret
        missing = [name for name, value in (("HOST", host), ("USER", user), ("PASSWORD", password)) if not value]
        if missing:
            raise RuntimeError(
                f"Missing {', '.join(f'{prefix}_IMAP_{m}' for m in missing)} in the environment. "
                "Fill these in directly in the shared .env file — never paste a password into chat."
            )
        return cls(host=host, port=int(port_raw), user=user, password=password)  # pragma: allowlist secret


def connect(account: ImapAccount) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(account.host, account.port)
    conn.login(account.user, account.password)
    return conn


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    decoded = ""
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _extract_bodies(msg: EmailLibMessage) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if part.get_content_type() == "text/plain":
                plain_parts.append(text)
            elif part.get_content_type() == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)
    return "\n".join(plain_parts), "\n".join(html_parts)


def parse_rfc822_message(raw_bytes: bytes, *, uid: str, folder: str = "INBOX") -> EmailMessage:
    """Convert a raw RFC822 message (as returned by IMAP FETCH) into the same
    `EmailMessage` shape `gmail_reader.parse_gmail_message` produces."""
    msg = email_lib.message_from_bytes(raw_bytes)
    body_plain, body_html = _extract_bodies(msg)
    if not body_plain.strip() and body_html.strip():
        body_plain = html_to_text(body_html)

    _, from_address = parseaddr(_decode_header_value(msg.get("From", "")))
    _, to_address = parseaddr(_decode_header_value(msg.get("To", "")))
    message_id = (msg.get("Message-Id") or msg.get("Message-ID") or "").strip()
    references = (msg.get("References") or "").split()

    return EmailMessage(
        # Prefixed so these ids never collide with Gmail's hex message ids in
        # shared tables (job_conversations.message_id, processed_messages).
        id=f"imap:{message_id}" if message_id else f"imap-uid:{folder}:{uid}",
        thread_id=f"imap:{references[0]}" if references else (f"imap:{message_id}" if message_id else ""),
        from_address=from_address,
        to_address=to_address,
        subject=_decode_header_value(msg.get("Subject", "")),
        date=msg.get("Date", ""),
        body_plain=body_plain.strip(),
        body_html=body_html.strip(),
        label_ids=[folder],
    )


def list_message_uids(
    conn: imaplib.IMAP4_SSL, *, folder: str = "INBOX", criteria: str = "ALL", limit: int | None = None
) -> list[str]:
    status, _ = conn.select(folder)
    if status != "OK":
        raise RuntimeError(f"Failed to select IMAP folder {folder!r}")
    status, data = conn.search(None, criteria)
    if status != "OK" or not data or data[0] is None:
        return []
    uids = [u.decode() for u in data[0].split()]
    if limit:
        uids = uids[-limit:]
    return uids


def fetch_message(conn: imaplib.IMAP4_SSL, uid: str, *, folder: str = "INBOX") -> EmailMessage:
    status, data = conn.fetch(uid, "(RFC822)")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Failed to fetch IMAP message uid={uid} in {folder!r}")
    raw_bytes = data[0][1]
    return parse_rfc822_message(raw_bytes, uid=uid, folder=folder)


def fetch_messages(
    account: ImapAccount,
    *,
    folder: str = "INBOX",
    criteria: str = "ALL",
    limit: int | None = None,
) -> list[EmailMessage]:
    """One-shot: connect, list matching uids, fetch each, disconnect."""
    conn = connect(account)
    try:
        uids = list_message_uids(conn, folder=folder, criteria=criteria, limit=limit)
        return [fetch_message(conn, uid, folder=folder) for uid in uids]
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def ensure_folder(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    """Create an IMAP folder (the label equivalent) if it doesn't already exist."""
    try:
        conn.create(folder)
    except imaplib.IMAP4.error:
        pass  # already exists — imaplib has no clean "IF NOT EXISTS", so just swallow


def move_message(conn: imaplib.IMAP4_SSL, uid: str, *, from_folder: str, to_folder: str) -> None:
    """Move a message to another folder — the IMAP equivalent of
    `gmail_writer.label_and_archive`'s label+archive (folder membership
    instead of label ids; leaving INBOX instead of removing the INBOX label)."""
    conn.select(from_folder)
    ensure_folder(conn, to_folder)
    copy_status, _ = conn.copy(uid, to_folder)
    if copy_status != "OK":
        raise RuntimeError(f"Failed to copy uid={uid} from {from_folder!r} to {to_folder!r}")
    conn.store(uid, "+FLAGS", "\\Deleted")
    conn.expunge()
