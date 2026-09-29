"""Google Calendar — scheduling source of truth (not Calendly).

Gmail is already the mail pipe; interviews and follow-ups belong on the same
Google account's calendar so missed-meeting warnings don't depend on Calendly
reminders.

Read/write Calendar API uses a *separate* OAuth token from Gmail
(``~/.config/job-tracker/token_calendar.json``) so existing gmail.readonly /
gmail.modify tokens stay valid. Until that consent exists, the UI still
opens a Calendar event template in the browser.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from job_tracker.email.gmail_reader import CONFIG_DIR, default_credentials_path

CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar.events",)
CALENDAR_TOKEN_NAME = "token_calendar.json"


def default_calendar_token_path() -> Path:
    return CONFIG_DIR / CALENDAR_TOKEN_NAME


def google_calendar_template_url(*, title: str, details: str = "", location: str = "") -> str:
    """Pre-filled Google Calendar event (no extra OAuth). User sets time + alert."""
    params = {"action": "TEMPLATE", "text": title or "Interview"}
    if details:
        params["details"] = details
    if location:
        params["location"] = location
    return f"https://calendar.google.com/calendar/render?{urlencode(params)}"


def calendar_token_present() -> bool:
    return default_calendar_token_path().is_file()


def get_calendar_service(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
):
    """Calendar API client. First run opens a browser for calendar.events consent.

    Pair with ``token_calendar.json`` — do not reuse the Gmail token.
    """
    credentials_path = credentials_path or default_credentials_path()
    token_path = token_path or default_calendar_token_path()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not credentials_path.is_file():
        raise FileNotFoundError(f"OAuth client missing at {credentials_path}")

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), CALENDAR_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), CALENDAR_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def match_event_to_lead_keys(
    *,
    summary: str,
    attendees: list[str],
    leads: list[dict],
) -> str | None:
    """Best-effort: company/title in the event title, or recruiter email in attendees."""
    blob = f"{summary or ''} {' '.join(attendees)}".lower()
    if not blob.strip():
        return None
    for lead in leads:
        company = (lead.get("company") or "").strip().lower()
        title = (lead.get("title") or "").strip().lower()
        email = (lead.get("recruiterEmail") or "").strip().lower()
        key = lead.get("normalizedKey") or ""
        if not key:
            continue
        if email and email in blob:
            return key
        if company and company in blob:
            return key
        if title and len(title) > 8 and title in blob:
            return key
    return None
