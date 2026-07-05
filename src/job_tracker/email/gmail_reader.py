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
# Write-capable scope, used only by the recruiter-inbox triage flow
# (pipeline/triage.py, scripts/triage_recruiter_inbox.py) to relabel and
# archive messages on the default recruiting-funnel account. Kept as a
# separate scope/token pair from GMAIL_SCOPES above rather than upgrading
# the existing readonly token in place, so every other command in this repo
# (run_pipeline.py, classify_inbox.py, etc.) keeps working unchanged with
# its narrower, already-granted readonly token.
GMAIL_SCOPES_MODIFY = ("https://www.googleapis.com/auth/gmail.modify",)
DEFAULT_QUERY = "is:unread in:inbox"
CONFIG_DIR = Path.home() / ".config" / "job-tracker"

# Additional named accounts this reader can pull from, beyond the default
# recruiting-funnel account above. Each gets its own credentials/token pair
# under CONFIG_DIR/<account>/, and its own one-time OAuth consent — job-
# tracker only ever requests read-only access, so it needs its own token
# even for an account comms-migration's classifier already has (broader)
# gmail.modify access to.
#
# `personal_hub` (scbboston@gmail.com) exists to close a gap: recruiter/job
# mail sometimes lands there instead of the recruiting funnel. comms-
# migration's classifier labels it `Category/recruiter_job` there but never
# moves it; pointing job-tracker at this account with
# `--query "label:Category/recruiter_job is:unread"` lets it pick up exactly
# that pre-filtered set through the normal pipeline, without ever touching
# the rest of the personal inbox.
KNOWN_ACCOUNTS = ("personal_hub",)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _account_config_dir(account: str | None) -> Path:
    return CONFIG_DIR / account if account else CONFIG_DIR


def default_credentials_path(account: str | None = None) -> Path:
    env = os.environ.get("JOB_TRACKER_GMAIL_CREDENTIALS")
    if env and not account:
        return Path(env).expanduser()
    config_path = _account_config_dir(account) / "credentials.json"
    if config_path.is_file():
        return config_path
    if not account:
        local = repo_root() / "credentials.json"
        if local.is_file():
            return local
    return config_path


def default_token_path(account: str | None = None, *, writable: bool = False) -> Path:
    env_var = "JOB_TRACKER_GMAIL_TOKEN_MODIFY" if writable else "JOB_TRACKER_GMAIL_TOKEN"
    env = os.environ.get(env_var)
    if env and not account:
        return Path(env).expanduser()
    filename = "token_modify.json" if writable else "token.json"
    config_path = _account_config_dir(account) / filename
    if config_path.is_file():
        return config_path
    if not account:
        local = repo_root() / filename
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
    *,
    account: str | None = None,
    scopes: tuple[str, ...] = GMAIL_SCOPES,
):
    """Build an authenticated Gmail API client (opens browser on first run).

    `scopes` defaults to read-only; pass `GMAIL_SCOPES_MODIFY` for the
    triage flow's write access. Always pair a wider scope with its own
    `token_path` (see `default_token_path(..., writable=True)`) — a token
    cached under the old scope won't gain new permissions just because a
    wider scope is requested here; Google will 403 the write call instead.
    """
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
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                # Most common cause while this OAuth app is in Google's
                # "Testing" publishing status: refresh tokens for unverified
                # apps hard-expire after 7 days, regardless of use. Fall
                # through to a fresh interactive login instead of raising a
                # confusing invalid_grant/RefreshError.
                label = f"'{account}'" if account else "the recruiting funnel"
                print(
                    f"Cached Gmail token for {label} is no longer valid ({exc}). "
                    "Re-opening browser for a fresh login — this is expected "
                    "roughly weekly while this app is in Google's 'Testing' "
                    "publishing status (unverified-app tokens expire after 7 "
                    "days). Sign in as the same account you used before."
                )
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_gmail_service_writable(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
    *,
    account: str | None = None,
):
    """Build a Gmail client with `gmail.modify` (read/write) access.

    Used only by the recruiter-inbox triage flow to relabel and archive
    messages. Reuses the same OAuth client (`credentials.json`) as the
    read-only flows, but a separate cached token (`token_modify.json`) and
    its own one-time consent screen, since a write scope needs explicit
    user approval a readonly token never got.
    """
    credentials_path = credentials_path or default_credentials_path(account)
    token_path = token_path or default_token_path(account, writable=True)
    return get_gmail_service(
        credentials_path, token_path, account=account, scopes=GMAIL_SCOPES_MODIFY
    )


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
    account: str | None = None,
) -> list[EmailMessage]:
    """Fetch unread inbox messages as EmailMessage objects.

    `account`, when set, reads from a named account registered in
    KNOWN_ACCOUNTS instead of the default recruiting-funnel account
    (credentials/token resolved from CONFIG_DIR/<account>/ unless overridden
    by credentials_path/token_path).
    """
    credentials_path = credentials_path or default_credentials_path(account)
    token_path = token_path or default_token_path(account)
    service = get_gmail_service(credentials_path, token_path, account=account)
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
    account: str | None = None,
) -> EmailMessage:
    credentials_path = credentials_path or default_credentials_path(account)
    token_path = token_path or default_token_path(account)
    service = get_gmail_service(credentials_path, token_path, account=account)
    return fetch_message(service, message_id)
