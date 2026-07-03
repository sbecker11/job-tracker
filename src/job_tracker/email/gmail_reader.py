"""Gmail API reader for the recruiting inbox."""

from __future__ import annotations

import base64
import html
import os
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from job_tracker.email.models import EmailMessage
from job_tracker.htmltext import html_to_text

GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
DEFAULT_QUERY = "is:unread in:inbox"
CONFIG_DIR = Path.home() / ".config" / "job-tracker"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_credentials_path() -> Path:
    env = os.environ.get("JOB_TRACKER_GMAIL_CREDENTIALS")
    if env:
        return Path(env).expanduser()
    config_path = CONFIG_DIR / "credentials.json"
    if config_path.is_file():
        return config_path
    local = repo_root() / "credentials.json"
    if local.is_file():
        return local
    return config_path


def default_token_path() -> Path:
    env = os.environ.get("JOB_TRACKER_GMAIL_TOKEN")
    if env:
        return Path(env).expanduser()
    config_path = CONFIG_DIR / "token.json"
    if config_path.is_file():
        return config_path
    local = repo_root() / "token.json"
    if local.is_file():
        return local
    return config_path


def _require_google_libs():
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Gmail support requires google-api-python-client and google-auth-oauthlib. "
            "Install with: pip install google-api-python-client google-auth-oauthlib"
        ) from exc


def get_gmail_service(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
):
    """Build an authenticated Gmail API client (opens browser on first run)."""
    _require_google_libs()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials_path = credentials_path or default_credentials_path()
    token_path = token_path or default_token_path()

    if not credentials_path.is_file():
        raise FileNotFoundError(
            f"Gmail credentials not found at {credentials_path}. "
            "Download OAuth desktop credentials from Google Cloud Console, or set "
            "JOB_TRACKER_GMAIL_CREDENTIALS."
        )

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers") or []
    return {item["name"].lower(): item["value"] for item in headers if "name" in item and "value" in item}


def _decode_part_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _collect_bodies(payload: dict[str, Any], plain_parts: list[str], html_parts: list[str]) -> None:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")
    if data:
        text = _decode_part_data(data)
        if mime_type == "text/plain":
            plain_parts.append(text)
        elif mime_type == "text/html":
            html_parts.append(text)
    for part in payload.get("parts") or []:
        _collect_bodies(part, plain_parts, html_parts)


def parse_gmail_message(raw: dict[str, Any]) -> EmailMessage:
    """Convert a Gmail API messages.get payload into EmailMessage."""
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_bodies(payload, plain_parts, html_parts)

    body_plain = "\n".join(part.strip() for part in plain_parts if part.strip())
    body_html = "\n".join(part.strip() for part in html_parts if part.strip())
    if not body_plain.strip() and body_html.strip():
        # Structure-preserving conversion (paragraph/list breaks kept, not
        # flattened to one line) — many senders are HTML-only, and this text
        # both classification/extraction and (when used as a JD fallback)
        # the stored lead record depend on.
        body_plain = html_to_text(body_html)
    # Some senders ship stray HTML entities (e.g. "&amp;") even inside a
    # nominally plain-text part; unescape defensively so downstream regexes
    # matching on literal text (e.g. "&") aren't broken by "&amp;".
    body_plain = html.unescape(body_plain)

    _, from_address = parseaddr(headers.get("from", ""))
    _, to_address = parseaddr(headers.get("to", ""))

    return EmailMessage(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        from_address=from_address or headers.get("from", ""),
        to_address=to_address or headers.get("to", ""),
        subject=headers.get("subject", ""),
        date=headers.get("date", ""),
        snippet=raw.get("snippet", ""),
        body_plain=body_plain,
        body_html=body_html,
        label_ids=list(raw.get("labelIds") or []),
    )


def build_query(
    base_query: str = DEFAULT_QUERY,
    newer_than_days: int | None = None,
) -> str:
    query = base_query.strip()
    if newer_than_days is not None and newer_than_days > 0:
        query = f"{query} newer_than:{newer_than_days}d".strip()
    return query


def list_message_ids(
    service,
    *,
    query: str = DEFAULT_QUERY,
    limit: int | None = None,
    newer_than_days: int | None = None,
) -> list[str]:
    q = build_query(query, newer_than_days)
    ids: list[str] = []
    page_token = None
    max_results = min(limit, 500) if limit else 100

    while True:
        request = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=max_results, pageToken=page_token)
        )
        response = request.execute()
        for item in response.get("messages") or []:
            ids.append(item["id"])
            if limit is not None and len(ids) >= limit:
                return ids
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        if limit is not None:
            max_results = min(limit - len(ids), 500)

    return ids


def fetch_message(service, message_id: str) -> EmailMessage:
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return parse_gmail_message(raw)


def fetch_unread(
    *,
    limit: int | None = None,
    query: str = DEFAULT_QUERY,
    newer_than_days: int | None = None,
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> list[EmailMessage]:
    """Fetch unread inbox messages as EmailMessage objects."""
    service = get_gmail_service(credentials_path, token_path)
    ids = list_message_ids(
        service,
        query=query,
        limit=limit,
        newer_than_days=newer_than_days,
    )
    return [fetch_message(service, message_id) for message_id in ids]


def fetch_message_by_id(
    message_id: str,
    *,
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> EmailMessage:
    service = get_gmail_service(credentials_path, token_path)
    return fetch_message(service, message_id)
