"""Find recruiter mail, leads, and package documents.

Used by the Pending Actions "Find JD email" box. Hostinger's own search only
looks at the currently open folder (usually Inbox), so a message the 3-minute
tick already moved into JobTracker.* is invisible there. This module:

- **before the tick** — IMAP TEXT/FROM/SUBJECT across every Spexture folder
- **after the tick** — every ``leads.db`` table and text column, every lead
  status, plus résumé-package files on disk (JD, reviews, communications, …)

Never logs IMAP passwords. IMAP select is readonly (does not \\Seen or move).
"""

from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from job_tracker.email.imap_reader import ImapAccount, connect as imap_connect, fetch_message

HOSTINGER_MAILBOX_BASE = "https://mail.hostinger.com/0/mailboxes"

# Fallback if IMAP LIST fails. The live search walks every folder.
FIND_MAIL_FOLDERS = (
    "INBOX",
    "INBOX.JobTracker.Linked",
    "INBOX.JobTracker.NEEDS_REVIEW",
    "INBOX.JobTracker.SKIP",
    "INBOX.JobTracker.PURSUE",
    "INBOX.JobTracker.NeedsFollowup",
    "INBOX.Junk",
    "INBOX.Sent",
    "INBOX.Archive",
    "INBOX.Drafts",
    "INBOX.Trash",
)

_MAX_IMAP_PER_FOLDER = 8
_SNIPPET_LEN = 400
_MIN_QUERY_LEN = 2
_MAX_FILE_BYTES = 4_000_000
_MAX_FILE_CONTENT_SCANS = 500
_TEXT_SUFFIXES = {".txt", ".log", ".md", ".json", ".csv", ".html", ".xml", ".webloc", ".eml"}
_OFFICE_SUFFIXES = {".docx", ".odt", ".dotx"}

# (table, job-key column). job_leads uses normalized_key.
_LEAD_TABLES: tuple[tuple[str, str], ...] = (
    ("job_leads", "normalized_key"),
    ("job_contacts", "job_key"),
    ("job_conversations", "job_key"),
    ("job_documents", "job_key"),
    ("job_meetings", "job_key"),
    ("job_offers", "job_key"),
)


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
    status: str = "",
    match_fields: list[str] | None = None,
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
        "status": status or "",
        "match_fields": list(match_fields or []),
        "hostinger_url": hostinger_mailbox_url(folder) if folder else "",
        "lead_href": lead_href(job_key) if job_key else "",
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


def _cell_matches(value: object, needle: str, needle_digits: str) -> bool:
    if value is None:
        return False
    text = str(value)
    if needle and needle in text.lower():
        return True
    if len(needle_digits) >= 7 and needle_digits in digits_only(text):
        return True
    return False


def _row_match_fields(
    row: sqlite3.Row, columns: list[str], needle: str, needle_digits: str, *, table: str
) -> tuple[list[str], str]:
    fields: list[str] = []
    snippet = ""
    for col in columns:
        try:
            value = row[col]
        except (IndexError, KeyError):
            continue
        if not _cell_matches(value, needle, needle_digits):
            continue
        fields.append(f"{table}.{col}")
        if not snippet:
            snippet = str(value)
    return fields, snippet


def search_db(
    conn: sqlite3.Connection,
    query: str,
    *,
    output_root: Path | None = None,
    skip_files: bool = False,
) -> list[dict[str, Any]]:
    """After-tick hits: every lead, every CRM/text column, package files."""
    q = query.strip()
    if len(q) < _MIN_QUERY_LEN:
        return []
    like = _like(q)
    needle = q.lower()
    needle_digits = digits_only(q)
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    fields_by_key: dict[str, list[str]] = {}
    snippet_by_key: dict[str, str] = {}

    def add(hit: dict[str, Any]) -> None:
        key = (
            hit["source"],
            hit.get("job_key", ""),
            hit.get("from_address", ""),
            hit.get("subject", ""),
            hit.get("date", ""),
        )
        if key in seen:
            return
        seen.add(key)
        hits.append(hit)
        job_key = (hit.get("job_key") or "").strip()
        if job_key:
            fields_by_key.setdefault(job_key, []).extend(hit.get("match_fields") or [])
            if hit.get("snippet") and job_key not in snippet_by_key:
                snippet_by_key[job_key] = hit["snippet"]

    def remember(job_key: str, fields: list[str], snippet: str) -> None:
        job_key = (job_key or "").strip()
        if not job_key or not fields:
            return
        fields_by_key.setdefault(job_key, []).extend(fields)
        if snippet and job_key not in snippet_by_key:
            snippet_by_key[job_key] = snippet

    for table, key_col in _LEAD_TABLES:
        columns = _table_columns(conn, table)
        if not columns or key_col not in columns:
            continue
        where = " OR ".join(f"lower(ifnull({col}, '')) LIKE ?" for col in columns)
        params: list[str] = [like] * len(columns)
        try:
            rows = conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            fields, snippet = _row_match_fields(row, columns, needle, needle_digits, table=table)
            remember(row[key_col] or "", fields, snippet)

    if len(needle_digits) >= 7:
        contact_cols = _table_columns(conn, "job_contacts")
        if "phone" in contact_cols:
            for row in conn.execute(
                "SELECT * FROM job_contacts WHERE "
                "replace(replace(replace(replace(ifnull(phone,''),'-',''),'(',''),')',''),' ','') LIKE ?",
                (f"%{needle_digits}%",),
            ):
                fields, snippet = _row_match_fields(
                    row, contact_cols, needle, needle_digits, table="job_contacts"
                )
                remember(row["job_key"] or "", fields or ["job_contacts.phone"], snippet or row["phone"])

    unmatched_cols = _table_columns(conn, "unmatched_messages")
    if unmatched_cols:
        where = " OR ".join(f"lower(ifnull({col}, '')) LIKE ?" for col in unmatched_cols)
        try:
            unmatched_rows = conn.execute(
                f"SELECT * FROM unmatched_messages WHERE {where}",
                tuple([like] * len(unmatched_cols)),
            ).fetchall()
        except sqlite3.OperationalError:
            unmatched_rows = []
        for row in unmatched_rows:
            fields, snippet = _row_match_fields(
                row, unmatched_cols, needle, needle_digits, table="unmatched_messages"
            )
            job_key = ""
            if "resolved_job_key" in unmatched_cols:
                job_key = row["resolved_job_key"] or ""
            if job_key:
                remember(job_key, fields, snippet)
            company = title = status = ""
            if job_key:
                lead = conn.execute(
                    "SELECT company, title, status FROM job_leads WHERE normalized_key = ?",
                    (job_key,),
                ).fetchone()
                if lead:
                    company, title, status = lead["company"], lead["title"], lead["status"] or ""
            add(
                _hit(
                    source="db",
                    when="after_tick",
                    from_address=row["from_address"] if "from_address" in unmatched_cols else "",
                    subject=row["subject"] if "subject" in unmatched_cols else "",
                    date=row["detected_at"] if "detected_at" in unmatched_cols else "",
                    snippet=snippet or (row["body_text"] if "body_text" in unmatched_cols else ""),
                    company=company,
                    title=title,
                    job_key=job_key,
                    status=status,
                    match_fields=fields,
                )
            )

    processed_cols = _table_columns(conn, "processed_messages")
    if processed_cols:
        where = " OR ".join(f"lower(ifnull({col}, '')) LIKE ?" for col in processed_cols)
        try:
            processed_rows = conn.execute(
                f"SELECT * FROM processed_messages WHERE {where}",
                tuple([like] * len(processed_cols)),
            ).fetchall()
        except sqlite3.OperationalError:
            processed_rows = []
        for row in processed_rows:
            fields, snippet = _row_match_fields(
                row, processed_cols, needle, needle_digits, table="processed_messages"
            )
            keys: list[str] = []
            raw_keys = row["lead_keys"] if "lead_keys" in processed_cols else ""
            if raw_keys:
                try:
                    parsed = json.loads(raw_keys)
                    if isinstance(parsed, list):
                        keys = [str(k) for k in parsed if k]
                    elif parsed:
                        keys = [str(parsed)]
                except (TypeError, json.JSONDecodeError):
                    keys = [str(raw_keys)]
            for job_key in keys:
                remember(job_key, fields, snippet)

    _search_document_contents(conn, needle, fields_by_key, snippet_by_key)
    if not skip_files and output_root is not None:
        _search_package_files(conn, output_root, needle, fields_by_key, snippet_by_key)

    for job_key, fields in fields_by_key.items():
        lead = conn.execute(
            "SELECT company, title, status, last_seen FROM job_leads WHERE normalized_key = ?",
            (job_key,),
        ).fetchone()
        if not lead:
            continue
        add(
            _hit(
                source="db",
                when="after_tick",
                subject=f"{lead['title']} @ {lead['company']}",
                date=lead["last_seen"] or "",
                snippet=snippet_by_key.get(job_key) or f"Lead on file: {lead['title']} @ {lead['company']}",
                company=lead["company"],
                title=lead["title"],
                job_key=job_key,
                status=lead["status"] or "",
                match_fields=fields,
            )
        )

    collapsed = _collapse_db_hits_to_leads(conn, hits)
    for hit in collapsed:
        job_key = (hit.get("job_key") or "").strip()
        if job_key and fields_by_key.get(job_key):
            hit["match_fields"] = sorted(set(fields_by_key[job_key]))
            extra = "matched in " + ", ".join(_short_field(f) for f in hit["match_fields"][:8])
            snippet = hit.get("snippet") or ""
            if extra not in snippet:
                hit["snippet"] = _snippet((snippet + " · " + extra).strip(" ·"))
    return collapsed


def _short_field(field: str) -> str:
    if field.startswith("package_file."):
        return f"package file ({field.split('.', 1)[1]})"
    name = field.split(".")[-1].replace("_", " ")
    aliases = {
        "jd text": "JD",
        "body text": "message body",
        "path or url": "document",
        "from address": "from",
        "llm job summary": "LLM review",
        "llm rationale": "LLM review",
        "rejection email text": "rejection",
        "package file": "package file",
    }
    return aliases.get(name, name)


def _office_xml_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            chunks: list[str] = []
            for candidate in ("word/document.xml", "content.xml"):
                if candidate not in names:
                    continue
                xml = zf.read(candidate)
                chunks.append(re.sub(rb"<[^>]+>", b" ", xml).decode("utf-8", "replace"))
            return " ".join(chunks)
    except Exception:
        return ""


def _file_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
    except OSError:
        return ""
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if suffix in _OFFICE_SUFFIXES:
        return _office_xml_text(path)
    return ""


def _search_package_files(
    conn: sqlite3.Connection,
    output_root: Path,
    needle: str,
    fields_by_key: dict[str, list[str]],
    snippet_by_key: dict[str, str],
) -> None:
    root = Path(output_root)
    if not needle or not root.is_dir():
        return
    from job_tracker.pipeline.llm_apply import _safe_filename

    index: dict[tuple[str, str], str] = {}
    for row in conn.execute("SELECT normalized_key, company, title FROM job_leads"):
        index[(_safe_filename(row["company"]).lower(), _safe_filename(row["title"]).lower())] = (
            row["normalized_key"]
        )

    scans = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        if len(parts) < 2:
            continue
        company_safe, title_safe = parts[0].lower(), parts[1].lower()
        job_key = index.get((company_safe, title_safe), "")
        if not job_key:
            continue
        name_blob = f"{path.name} {rel.as_posix()}".lower()
        matched = needle in name_blob
        snippet = path.name
        if not matched and path.suffix.lower() in _TEXT_SUFFIXES | _OFFICE_SUFFIXES:
            if scans >= _MAX_FILE_CONTENT_SCANS:
                continue
            scans += 1
            text = _file_text(path)
            if needle in text.lower():
                matched = True
                snippet = text
        if matched:
            fields_by_key.setdefault(job_key, []).append(f"package_file.{path.name}")
            if job_key not in snippet_by_key:
                snippet_by_key[job_key] = snippet


def _search_document_contents(
    conn: sqlite3.Connection,
    needle: str,
    fields_by_key: dict[str, list[str]],
    snippet_by_key: dict[str, str],
) -> None:
    """Read files recorded on job_documents, including paths outside the package tree."""
    doc_cols = _table_columns(conn, "job_documents")
    if "path_or_url" not in doc_cols or not needle:
        return
    scans = 0
    for row in conn.execute("SELECT job_key, path_or_url FROM job_documents"):
        raw = (row["path_or_url"] or "").strip()
        if not raw or raw.lower().startswith(("http://", "https://")):
            continue
        path = Path(raw).expanduser()
        if not path.is_file():
            continue
        if needle in path.name.lower() or needle in str(path).lower():
            fields_by_key.setdefault(row["job_key"], []).append("job_documents.path_or_url")
            snippet_by_key.setdefault(row["job_key"], path.name)
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES | _OFFICE_SUFFIXES:
            continue
        if scans >= _MAX_FILE_CONTENT_SCANS:
            continue
        scans += 1
        text = _file_text(path)
        if needle in text.lower():
            fields_by_key.setdefault(row["job_key"], []).append("job_documents.path_or_url")
            snippet_by_key.setdefault(row["job_key"], text)


def _collapse_db_hits_to_leads(
    conn: sqlite3.Connection, hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per tracked lead. Unmatched mail with no job_key stays as-is."""
    from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT
    from job_tracker.pipeline.pending_workflow import _company_folder_abs, _package_folder_abs

    by_key: dict[str, dict[str, Any]] = {}
    leftover: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit.get("job_key") or "").strip()
        if not key:
            leftover.append(hit)
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = dict(hit)
            continue
        if (hit.get("date") or "") > (prev.get("date") or ""):
            prev["date"] = hit.get("date") or prev.get("date") or ""
        if not prev.get("from_address") and hit.get("from_address"):
            prev["from_address"] = hit["from_address"]
        if not prev.get("contact_name") and hit.get("contact_name"):
            prev["contact_name"] = hit["contact_name"]
        if not prev.get("status") and hit.get("status"):
            prev["status"] = hit["status"]
        prev_fields = list(prev.get("match_fields") or [])
        prev_fields.extend(hit.get("match_fields") or [])
        prev["match_fields"] = prev_fields

    out: list[dict[str, Any]] = []
    for key, hit in by_key.items():
        contact = conn.execute(
            "SELECT name, email FROM job_contacts WHERE job_key = ? ORDER BY id LIMIT 1",
            (key,),
        ).fetchone()
        if contact:
            if not hit.get("from_address") and contact["email"]:
                hit["from_address"] = contact["email"]
            if not hit.get("contact_name") and contact["name"]:
                hit["contact_name"] = contact["name"]
        company = hit.get("company") or ""
        title = hit.get("title") or ""
        comm_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM job_conversations WHERE job_key = ?", (key,)
            ).fetchone()["n"]
        )
        folder_path, _ready = _package_folder_abs(
            conn,
            company=company or "Unknown",
            title=title or company or "Unknown",
            output_root=DEFAULT_OUTPUT_ROOT,
        )
        company_folder = _company_folder_abs(
            company=company or "Unknown", output_root=DEFAULT_OUTPUT_ROOT
        )
        who = hit.get("contact_name") or hit.get("from_address") or ""
        status = hit.get("status") or ""
        bits = [who, f"{comm_count} communication" + ("" if comm_count == 1 else "s")]
        if status:
            bits.append(status)
        hit["subject"] = " — ".join(p for p in (company, title) if p) or hit.get("subject") or ""
        hit["snippet"] = " · ".join(b for b in bits if b)
        hit["match_fields"] = sorted(set(hit.get("match_fields") or []))
        hit["comm_count"] = comm_count
        hit["folder_path"] = folder_path
        hit["company_folder_path"] = company_folder
        hit["reveal_url"] = f"revealfolder://reveal?path={quote(folder_path)}"
        out.append(hit)
    out.sort(key=lambda h: h.get("date") or "", reverse=True)
    return out + leftover


def _decode_imap_folder(raw: object) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    match = re.search(r' "([^"]+)"\s*$', text) or re.search(r" ([^\s]+)\s*$", text)
    return match.group(1) if match else text


def _list_imap_folders(imap) -> list[str]:
    try:
        status, boxes = imap.list()
    except Exception:
        return list(FIND_MAIL_FOLDERS)
    if status != "OK" or not boxes:
        return list(FIND_MAIL_FOLDERS)
    names = [_decode_imap_folder(box) for box in boxes]
    return [n for n in names if n] or list(FIND_MAIL_FOLDERS)


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
        for folder in _list_imap_folders(imap):
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


def _addr_key(value: str) -> str:
    from email.utils import parseaddr

    _, addr = parseaddr(value or "")
    return (addr or value or "").strip().lower()


def _imap_not_already_tracked(
    conn: sqlite3.Connection,
    imap_hits: list[dict[str, Any]],
    db_hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep IMAP only when it is not already a tracked lead; one row per sender."""
    tracked = {_addr_key(h.get("from_address") or "") for h in db_hits}
    for hit in db_hits:
        key = (hit.get("job_key") or "").strip()
        if not key:
            continue
        for row in conn.execute("SELECT email FROM job_contacts WHERE job_key = ?", (key,)):
            tracked.add(_addr_key(row["email"] or ""))
    tracked.discard("")
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for hit in imap_hits:
        key = _addr_key(hit.get("from_address") or "")
        if key and key in tracked:
            continue
        dedupe = key or (hit.get("subject") or "")
        if dedupe in seen:
            continue
        if dedupe:
            seen.add(dedupe)
        kept.append(hit)
    return kept


def search(
    query: str,
    *,
    conn: sqlite3.Connection,
    imap_search: Callable[[str], list[dict[str, Any]]] | None = None,
    skip_imap: bool = False,
    output_root: Path | None = None,
    skip_files: bool = False,
) -> dict[str, Any]:
    q = (query or "").strip()
    file_root = output_root
    if file_root is None and not skip_files:
        from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT

        file_root = DEFAULT_OUTPUT_ROOT
    db_hits = (
        search_db(conn, q, output_root=file_root, skip_files=skip_files)
        if len(q) >= _MIN_QUERY_LEN
        else []
    )
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
        "imap": _imap_not_already_tracked(conn, imap_hits, db_hits),
        "db": db_hits,
        "imap_error": imap_error,
    }
