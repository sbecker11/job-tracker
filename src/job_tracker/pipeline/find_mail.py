"""Find the original recruiter JD email on Spexture IMAP and/or in leads.db.

Used by the Pending Actions "Find JD email" box. Hostinger's own search only
looks at the currently open folder (usually Inbox), so a message the 3-minute
tick already moved into JobTracker.* is invisible there. This module:

- **before the tick** — IMAP TEXT/FROM/SUBJECT across Inbox + JobTracker folders
- **after the tick** — job_contacts / job_conversations / unmatched_messages

Never logs IMAP passwords. IMAP select is readonly (does not \\Seen or move).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable
from urllib.parse import quote

from job_tracker.email.imap_reader import ImapAccount, connect as imap_connect, fetch_message

HOSTINGER_MAILBOX_BASE = "https://mail.hostinger.com/0/mailboxes"

# Same folders the Spexture tick actually uses (see triage_imap_inbox.py).
FIND_MAIL_FOLDERS = (
    "INBOX",
    "INBOX.JobTracker.Linked",
    "INBOX.JobTracker.NEEDS_REVIEW",
    "INBOX.JobTracker.SKIP",
    "INBOX.JobTracker.PURSUE",
    "INBOX.JobTracker.NeedsFollowup",
    "INBOX.Junk",
)

_MAX_IMAP_PER_FOLDER = 8
_SNIPPET_LEN = 400
_MIN_QUERY_LEN = 2


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def hostinger_mailbox_url(folder: str) -> str:
    """Deep-link Hostinger webmail to an IMAP folder (unencoded dots, like the UI)."""
    folder = (folder or "INBOX").strip()
    return f"{HOSTINGER_MAILBOX_BASE}/{quote(folder, safe='._-')}"


def lead_href(normalized_key: str) -> str:
    anchor = "lead-" + re.sub(r"[^a-zA-Z0-9_-]", "-", normalized_key or "")
    return f"http://127.0.0.1:3174/#{anchor}"


def _snippet(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "")).strip()
    if len(collapsed) <= _SNIPPET_LEN:
        return collapsed
    return collapsed[: _SNIPPET_LEN - 1] + "…"


def _like(query: str) -> str:
    return f"%{query.lower().strip()}%"


def imap_search_criteria(query: str) -> str:
    """IMAP SEARCH criteria: each whitespace token must match FROM or SUBJECT or TEXT."""
    tokens = [t.replace('"', "").replace("\\", "")[:80] for t in query.split() if t.strip()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return "ALL"

    def one(token: str) -> str:
        return f'(OR FROM "{token}" (OR SUBJECT "{token}" TEXT "{token}"))'

    crit = one(tokens[0])
    for token in tokens[1:]:
        crit = f"({crit} {one(token)})"
    return crit


def _hit(
    *,
    source: str,
    when: str,
    from_address: str = "",
    subject: str = "",
    date: str = "",
    folder: str = "",
    snippet: str = "",
    company: str = "",
    title: str = "",
    job_key: str = "",
    contact_name: str = "",
) -> dict[str, Any]:
    folder = folder or ""
    job_key = job_key or ""
    return {
        "source": source,
        "when": when,
        "from_address": from_address or "",
        "subject": subject or "",
        "date": date or "",
        "folder": folder,
        "snippet": _snippet(snippet),
        "company": company or "",
        "title": title or "",
        "job_key": job_key,
        "contact_name": contact_name or "",
        "hostinger_url": hostinger_mailbox_url(folder) if folder else "",
        "lead_href": lead_href(job_key) if job_key else "",
    }


def search_db(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    """After-tick hits: stored contacts, conversation bodies, unmatched queue."""
    q = query.strip()
    if len(q) < _MIN_QUERY_LEN:
        return []
    like = _like(q)
    needle_digits = digits_only(q)
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    def add(hit: dict[str, Any]) -> None:
        key = (hit["source"], hit.get("job_key", ""), hit.get("from_address", ""), hit.get("subject", ""), hit.get("date", ""))
        if key in seen:
            return
        seen.add(key)
        hits.append(hit)

    contact_sql = """
        SELECT c.name, c.email, c.phone, c.job_key, l.company, l.title
        FROM job_contacts c
        JOIN job_leads l ON l.normalized_key = c.job_key
        WHERE lower(ifnull(c.name, '')) LIKE ?
           OR lower(ifnull(c.email, '')) LIKE ?
           OR ifnull(c.phone, '') LIKE ?
    """
    contact_params: list[str] = [like, like, like]
    if len(needle_digits) >= 7:
        contact_sql += " OR replace(replace(replace(replace(ifnull(c.phone,''),'-',''),'(',''),')',''),' ','') LIKE ?"
        contact_params.append(f"%{needle_digits}%")

    for row in conn.execute(contact_sql, contact_params):
        add(
            _hit(
                source="db",
                when="after_tick",
                from_address=row["email"] or "",
                subject="",
                date="",
                folder="",
                snippet=f"Contact on file: {row['name'] or row['email'] or row['phone']}",
                company=row["company"],
                title=row["title"],
                job_key=row["job_key"],
                contact_name=row["name"] or "",
            )
        )

    for row in conn.execute(
        """
        SELECT conv.summary, conv.body_text, conv.occurred_at, conv.job_key,
               l.company, l.title, k.name AS contact_name, k.email AS contact_email
        FROM job_conversations conv
        JOIN job_leads l ON l.normalized_key = conv.job_key
        LEFT JOIN job_contacts k ON k.id = conv.contact_id
        WHERE lower(ifnull(conv.summary, '')) LIKE ?
           OR lower(ifnull(conv.body_text, '')) LIKE ?
           OR lower(ifnull(k.name, '')) LIKE ?
           OR lower(ifnull(k.email, '')) LIKE ?
           OR lower(ifnull(l.company, '')) LIKE ?
           OR lower(ifnull(l.title, '')) LIKE ?
        ORDER BY conv.occurred_at DESC
        LIMIT 40
        """,
        (like, like, like, like, like, like),
    ):
        add(
            _hit(
                source="db",
                when="after_tick",
                from_address=row["contact_email"] or "",
                subject=row["summary"] or "",
                date=row["occurred_at"] or "",
                folder="",
                snippet=row["body_text"] or row["summary"] or "",
                company=row["company"],
                title=row["title"],
                job_key=row["job_key"],
                contact_name=row["contact_name"] or "",
            )
        )

    for row in conn.execute(
        """
        SELECT l.normalized_key, l.company, l.title, l.last_seen
        FROM job_leads l
        WHERE lower(ifnull(l.company, '')) LIKE ?
           OR lower(ifnull(l.title, '')) LIKE ?
        ORDER BY ifnull(l.last_seen, '') DESC
        LIMIT 20
        """,
        (like, like),
    ):
        latest = conn.execute(
            """
            SELECT conv.summary, conv.body_text, conv.occurred_at, k.name, k.email
            FROM job_conversations conv
            LEFT JOIN job_contacts k ON k.id = conv.contact_id
            WHERE conv.job_key = ?
            ORDER BY conv.occurred_at DESC
            LIMIT 1
            """,
            (row["normalized_key"],),
        ).fetchone()
        add(
            _hit(
                source="db",
                when="after_tick",
                from_address=(latest["email"] if latest else "") or "",
                subject=(latest["summary"] if latest else "") or f"{row['title']} @ {row['company']}",
                date=(latest["occurred_at"] if latest else "") or (row["last_seen"] or ""),
                folder="",
                snippet=(latest["body_text"] if latest else "")
                or f"Lead on file: {row['title']} @ {row['company']}",
                company=row["company"],
                title=row["title"],
                job_key=row["normalized_key"],
                contact_name=(latest["name"] if latest else "") or "",
            )
        )

    try:
        unmatched_rows = conn.execute(
            """
            SELECT from_address, subject, body_text, detected_at, resolved_job_key, thread_id
            FROM unmatched_messages
            WHERE lower(ifnull(from_address, '')) LIKE ?
               OR lower(ifnull(subject, '')) LIKE ?
               OR lower(ifnull(body_text, '')) LIKE ?
            ORDER BY detected_at DESC
            LIMIT 20
            """,
            (like, like, like),
        ).fetchall()
    except sqlite3.OperationalError:
        unmatched_rows = []

    for row in unmatched_rows:
        job_key = row["resolved_job_key"] or ""
        company = title = ""
        if job_key:
            lead = conn.execute(
                "SELECT company, title FROM job_leads WHERE normalized_key = ?", (job_key,)
            ).fetchone()
            if lead:
                company, title = lead["company"], lead["title"]
        add(
            _hit(
                source="db",
                when="after_tick",
                from_address=row["from_address"] or "",
                subject=row["subject"] or "",
                date=row["detected_at"] or "",
                folder="",
                snippet=row["body_text"] or "",
                company=company,
                title=title,
                job_key=job_key,
            )
        )

    return hits


def search_imap_folders(query: str, *, account: ImapAccount | None = None) -> list[dict[str, Any]]:
    """Before-tick (and still-on-server) hits. Readonly. Does not require the DB tick."""
    q = query.strip()
    if len(q) < _MIN_QUERY_LEN:
        return []
    acct = account or ImapAccount.from_env("SPEXTURE")
    imap = imap_connect(acct)
    hits: list[dict[str, Any]] = []
    try:
        criteria = imap_search_criteria(q)
        for folder in FIND_MAIL_FOLDERS:
            try:
                status, _ = imap.select(folder, readonly=True)
            except Exception:
                continue
            if status != "OK":
                continue
            status, data = imap.search(None, criteria)
            if status != "OK" or not data or data[0] is None:
                continue
            uids = [u.decode() for u in data[0].split() if u]
            for uid in uids[-_MAX_IMAP_PER_FOLDER:]:
                try:
                    msg = fetch_message(imap, uid, folder=folder)
                except Exception:
                    continue
                hits.append(
                    _hit(
                        source="imap",
                        when="before_tick",
                        from_address=msg.from_address,
                        subject=msg.subject,
                        date=msg.date,
                        folder=folder,
                        snippet=msg.body_plain or msg.body_html or "",
                    )
                )
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    hits.sort(key=lambda h: h.get("date") or "", reverse=True)
    return hits


def search(
    query: str,
    *,
    conn: sqlite3.Connection,
    imap_search: Callable[[str], list[dict[str, Any]]] | None = None,
    skip_imap: bool = False,
) -> dict[str, Any]:
    q = (query or "").strip()
    db_hits = search_db(conn, q) if len(q) >= _MIN_QUERY_LEN else []
    imap_hits: list[dict[str, Any]] = []
    imap_error = ""
    if skip_imap:
        pass
    elif len(q) < _MIN_QUERY_LEN:
        imap_error = "Type at least 2 characters."
    else:
        search_fn = imap_search or search_imap_folders
        try:
            imap_hits = search_fn(q)
        except Exception as exc:
            imap_error = str(exc)
    return {
        "query": q,
        "imap": imap_hits,
        "db": db_hits,
        "imap_error": imap_error,
    }
