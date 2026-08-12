"""Resolve the best continuity reply link when a pending-action card lacks one.

LinkedIn InMails/replies often leave `threadUrl` empty and `message_id` blank
in `job_conversations`, while the same ping still exists in recruiting Gmail as
`hit-reply@linkedin.com` / `inmail-hit-reply@linkedin.com` mail. This module
searches that mailbox (read-only) and returns a recruiting-account Gmail deep
link (+ LinkedIn thread URL when present in the body).
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlencode

from job_tracker.pipeline.qualifying_reply import extract_linkedin_thread_url

RECRUITING_GMAIL_USER = "shawnbecker.recruiting@gmail.com"
_GMAIL_API_ID_RE = re.compile(r"^[0-9a-f]{10,}$", re.I)
_LI_FROM = "(from:hit-reply@linkedin.com OR from:inmail-hit-reply@linkedin.com)"
_PAREN_RE = re.compile(r"\([^)]*\)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'+-]{2,}")


def recruiting_gmail_message_url(message_id: str | None) -> str:
    """Gmail web URL for a Gmail API message id, pinned to the recruiting account."""
    mid = (message_id or "").strip()
    if not mid or not _GMAIL_API_ID_RE.fullmatch(mid):
        return ""
    continue_url = f"https://mail.google.com/mail/u/0/#all/{mid}"
    return "https://accounts.google.com/AccountChooser?" + urlencode(
        {"Email": RECRUITING_GMAIL_USER, "continue": continue_url}
    )


def _clean_person_name(name: str) -> str:
    return " ".join(_PAREN_RE.sub(" ", name or "").split())


def _company_token(company: str) -> str:
    cleaned = _PAREN_RE.sub(" ", company or "").strip()
    if not cleaned:
        return ""
    for tok in cleaned.replace("/", " ").split():
        if len(tok) >= 3 and tok.lower() not in {"llc", "inc", "ltd", "the", "and"}:
            return tok
    return cleaned.split()[0] if cleaned.split() else ""


def build_continuity_gmail_queries(
    *,
    recruiter_name: str = "",
    company: str = "",
    subject: str = "",
    recruiter_email: str = "",
    newer_than_days: int = 60,
) -> list[str]:
    """Ordered Gmail search queries — most specific first."""
    nt = f"newer_than:{max(1, int(newer_than_days))}d"
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    person = _clean_person_name(recruiter_name)
    if person:
        add(f'{_LI_FROM} "{person}" {nt}')
        parts = person.split()
        if len(parts) >= 2:
            add(f'{_LI_FROM} "{parts[0]} {parts[-1]}" {nt}')

    co = _company_token(company)
    if co:
        add(f"{_LI_FROM} {co} {nt}")
        if person:
            add(f'{_LI_FROM} "{person}" {co} {nt}')

    email = (recruiter_email or "").strip()
    if email and "@" in email and "linkedin.com" not in email.lower():
        add(f"from:{email} {nt}")
        add(f"to:{email} {nt}")

    skip = {
        "message",
        "replied",
        "remote",
        "opportunity",
        "linkedin",
        "follow",
        "up",
        "the",
        "and",
        "for",
        "with",
        "from",
    }
    subj_words = [
        w for w in _WORD_RE.findall(subject or "") if w.lower() not in skip and len(w) >= 4
    ][:4]
    if subj_words and person:
        add(f'{_LI_FROM} "{person}" {" ".join(subj_words[:2])} {nt}')
    elif subj_words and co:
        add(f"{_LI_FROM} {co} {' '.join(subj_words[:2])} {nt}")

    return queries


def resolve_reply_continuity(
    *,
    recruiter_name: str = "",
    company: str = "",
    subject: str = "",
    recruiter_email: str = "",
    preferred_message_id: str = "",
    existing_gmail_url: str = "",
    existing_thread_url: str = "",
    newer_than_days: int = 60,
    search_gmail: Callable[[str, int], list[str]] | None = None,
    fetch_body: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Return ``{gmailUrl, threadUrl, messageId, source}`` — empty strings when unresolved."""
    out = {
        "gmailUrl": (existing_gmail_url or "").strip(),
        "threadUrl": (existing_thread_url or "").strip(),
        "messageId": "",
        "source": "",
    }
    if out["gmailUrl"] and out["threadUrl"]:
        return out

    pref = (preferred_message_id or "").strip()
    if not out["gmailUrl"] and _GMAIL_API_ID_RE.fullmatch(pref):
        out["gmailUrl"] = recruiting_gmail_message_url(pref)
        out["messageId"] = pref
        out["source"] = "preferred_message_id"
        if out["threadUrl"]:
            return out

    need_search = (not out["gmailUrl"]) or (not out["threadUrl"])
    if not need_search:
        return out

    if search_gmail is None or fetch_body is None:
        live = _live_gmail_helpers()
        if live is None:
            return out
        search_gmail, fetch_body = live

    queries = build_continuity_gmail_queries(
        recruiter_name=recruiter_name,
        company=company,
        subject=subject,
        recruiter_email=recruiter_email,
        newer_than_days=newer_than_days,
    )
    tried: set[str] = set()
    for q in queries:
        try:
            ids = search_gmail(q, 5)
        except Exception:
            continue
        for mid in ids:
            if mid in tried:
                continue
            tried.add(mid)
            body = ""
            try:
                body = fetch_body(mid) or ""
            except Exception:
                body = ""
            thread = extract_linkedin_thread_url(body) or out["threadUrl"]
            gmail_url = out["gmailUrl"] or recruiting_gmail_message_url(mid)
            if not gmail_url and not thread:
                continue
            return {
                "gmailUrl": gmail_url,
                "threadUrl": thread,
                "messageId": mid,
                "source": f"gmail_search:{q[:80]}",
            }

    return out


def backfill_inbound_gmail_message_id(conn, job_key: str, gmail_id: str) -> bool:
    """Persist a discovered Gmail API id onto the newest inbound with no hex id."""
    if not job_key or not _GMAIL_API_ID_RE.fullmatch((gmail_id or "").strip()):
        return False
    rows = list(
        conn.execute(
            """
            SELECT id, message_id FROM job_conversations
            WHERE job_key = ? AND direction = 'inbound'
            ORDER BY occurred_at DESC
            """,
            (job_key,),
        )
    )
    for row in rows:
        mid = (row["message_id"] or "").strip()
        if not mid:
            conn.execute(
                "UPDATE job_conversations SET message_id = ? WHERE id = ?",
                (gmail_id, row["id"]),
            )
            conn.commit()
            return True
        if mid == gmail_id:
            return False
    return False


def _live_gmail_helpers() -> tuple[Callable[[str, int], list[str]], Callable[[str], str]] | None:
    try:
        from job_tracker.email import gmail_reader

        creds = gmail_reader.default_credentials_path(None)
        token = gmail_reader.default_token_path(None)
        if not creds.is_file() or not token.is_file():
            return None
        service = gmail_reader.get_gmail_service(creds, token, account=None)

        def search(query: str, limit: int) -> list[str]:
            return gmail_reader.list_message_ids(service, query=query, limit=limit)

        def body(message_id: str) -> str:
            raw = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From"],
                )
                .execute()
            )
            snippet = raw.get("snippet") or ""
            if "linkedin.com/messaging/thread" in snippet.lower():
                return snippet
            msg = gmail_reader.fetch_message(service, message_id)
            return msg.combined_text or snippet

        return search, body
    except Exception:
        return None


def enrich_item_reply_links(
    item: dict[str, Any],
    *,
    conn=None,
    search_gmail: Callable[[str, int], list[str]] | None = None,
    fetch_body: Callable[[str], str] | None = None,
    cache: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fill missing ``gmailUrl`` / ``threadUrl`` on a workflow card in place."""
    if item.get("gmailUrl") and item.get("threadUrl"):
        return item

    cache_key = "|".join(
        [
            (item.get("normalizedKey") or "").strip().lower(),
            (item.get("recruiterName") or "").strip().lower(),
            (item.get("company") or "").strip().lower(),
            (item.get("subject") or item.get("title") or "").strip().lower()[:80],
        ]
    )
    if cache is not None and cache_key in cache:
        resolved = cache[cache_key]
    else:
        resolved = resolve_reply_continuity(
            recruiter_name=item.get("recruiterName") or "",
            company=item.get("company") or "",
            subject=item.get("subject") or item.get("title") or "",
            recruiter_email=item.get("recruiterEmail") or "",
            preferred_message_id=item.get("messageId") or "",
            existing_gmail_url=item.get("gmailUrl") or "",
            existing_thread_url=item.get("threadUrl") or "",
            search_gmail=search_gmail,
            fetch_body=fetch_body,
        )
        if cache is not None:
            cache[cache_key] = resolved

    if resolved.get("gmailUrl") and not item.get("gmailUrl"):
        item["gmailUrl"] = resolved["gmailUrl"]
    if resolved.get("threadUrl") and not item.get("threadUrl"):
        item["threadUrl"] = resolved["threadUrl"]
    if resolved.get("messageId") and not (item.get("messageId") or "").strip():
        item["messageId"] = resolved["messageId"]

    job_key = (item.get("normalizedKey") or "").strip()
    gmail_id = (resolved.get("messageId") or "").strip()
    if conn is not None and job_key and gmail_id:
        try:
            backfill_inbound_gmail_message_id(conn, job_key, gmail_id)
        except Exception:
            pass

    return item
