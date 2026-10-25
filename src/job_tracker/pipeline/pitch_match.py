"""One pitch seen twice: a pasted LinkedIn thread and the Gmail notification
of it, in either order.

The copies do not share a message id, a subject, or a sender address. They
match when the recruiter names agree and they share a long stretch of the
pitch that includes the role. A later copy is kept on the lead so both
thread histories can be written. A thread link it carries is written onto
the lead the card already has.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _job_folder
from job_tracker.pipeline.models import JobConversation, JobDocument
from job_tracker.pipeline.qualifying_reply import extract_linkedin_sender_name, extract_linkedin_thread_url
from job_tracker.pipeline.store import (
    add_job_conversation,
    add_job_document,
    get_sibling_titles,
    mark_duplicate,
    record_message_processed,
    utc_now_iso,
)

_MIN_SPAN = 60
_SENT_YOU_RE = re.compile(r"^\s*(.+?)\s+sent you a message\b", re.I)
_TBD_VIA_RE = re.compile(r"(?i)^end-client tbd via\s+(.+)$")
# The shared stretch has to name the role. A long greeting on its own is
# not enough to call two pitches the same.
_ROLE_RE = re.compile(
    r"\b(?:engineers?|developers?|architects?|scientists?|analysts?|programmers?|"
    r"administrators?|dbas?|sres?|fullstack|full stack)\b"
)


def normalized_pitch(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (name or "").lower())


def names_compatible(a: str, b: str) -> bool:
    """`Jaideep` with `Jaideep S`, and `Jaideep S` with `Jaideep Singh`.

    `John Smith` does not match `John Doe`."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb or ta[0] != tb[0] or len(ta[0]) < 2:
        return False
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) == 1 or short == long[: len(short)]:
        return True
    return (
        len(short[-1]) == 1
        and short[:-1] == long[: len(short) - 1]
        and long[len(short) - 1].startswith(short[-1])
    )


def pitches_overlap(a: str, b: str) -> bool:
    """True when the two texts are one recruiter pitch, even if one is only
    a snippet wrapped in LinkedIn's notification chrome.

    The shared stretch has to include the role. Two notes that only share
    a greeting stay separate.
    """
    na, nb = normalized_pitch(a), normalized_pitch(b)
    if len(na) < 40 or len(nb) < 40:
        return False
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= _MIN_SPAN and short in long and _ROLE_RE.search(short):
        return True
    span = SequenceMatcher(None, short, long, autojunk=False).find_longest_match(
        0, len(short), 0, len(long)
    )
    if span.size < _MIN_SPAN:
        return False
    shared = short[span.a : span.a + span.size]
    return _ROLE_RE.search(shared) is not None


def recruiter_name_for_mail(subject: str, body: str) -> str:
    match = _SENT_YOU_RE.search(subject or "")
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip(" -–—")
    return extract_linkedin_sender_name(body or "")


def find_lead_for_same_pitch(conn: sqlite3.Connection, *, name: str, text: str) -> str | None:
    """The one live lead whose recruiter and pitch are this message.

    None when the name is missing, the pitch is too short, or two leads
    both fit."""
    if not names_compatible(name, name) or len(normalized_pitch(text)) < 40:
        return None
    keys = _keys_for_recruiter(conn, name)
    hits = [key for key in keys if _lead_pitch_overlaps(conn, key, text)]
    if len(hits) == 1:
        return hits[0]
    return None


def remember_thread_url(conn: sqlite3.Connection, job_key: str, text: str) -> bool:
    """Append a messaging link from `text` onto the lead when it has none yet."""
    url = extract_linkedin_thread_url(text or "")
    if not url:
        return False
    rows = conn.execute(
        "SELECT id, message_id, body_text FROM job_conversations WHERE job_key = ? ORDER BY id",
        (job_key,),
    ).fetchall()
    if not rows or any(url in (row["body_text"] or "") for row in rows):
        return False
    target = next((row for row in rows if (row["message_id"] or "").startswith("paste:")), rows[0])
    conn.execute(
        "UPDATE job_conversations SET body_text = ? WHERE id = ?",
        ((target["body_text"] or "").rstrip() + "\n" + url, target["id"]),
    )
    conn.commit()
    return True


def claim_unmatched_same_pitch(
    conn: sqlite3.Connection,
    job_key: str,
    name: str,
    text: str,
    *,
    output_root: Path | None = None,
) -> bool:
    """Resolve parked mail that is this same pitch onto the lead, and keep
    that mail in the lead's email thread."""
    wrote = False
    rows = conn.execute(
        """
        SELECT message_id, subject, body_text, thread_id, direction, detected_at
        FROM unmatched_messages
        WHERE resolved_at IS NULL
          AND (dismissed_at IS NULL OR dismissed_at = '')
          AND (superseded_by IS NULL OR superseded_by = '')
        """
    ).fetchall()
    stamp = utc_now_iso()
    for row in rows:
        if not _unmatched_is_same(name, text, row["subject"] or "", row["body_text"] or ""):
            continue
        conn.execute(
            "UPDATE unmatched_messages SET resolved_job_key = ?, resolved_at = ? WHERE message_id = ?",
            (job_key, stamp, row["message_id"]),
        )
        conn.commit()
        _ensure_conversation(
            conn,
            job_key,
            message_id=row["message_id"],
            channel="email",
            direction=row["direction"] or "inbound",
            summary=row["subject"] or "",
            body=row["body_text"] or "",
            occurred_at=row["detected_at"] or stamp,
            thread_id=row["thread_id"] or "",
        )
        wrote = remember_thread_url(conn, job_key, row["body_text"] or "") or True
    if wrote:
        archive_thread_histories(conn, job_key, output_root=output_root)
    return wrote


def absorb_linked_copy(
    conn: sqlite3.Connection,
    job_key: str,
    message_id: str,
    text: str,
    *,
    subject: str = "",
    from_address: str = "",
    channel: str = "email",
    output_root: Path | None = None,
) -> bool:
    """Keep this copy on the lead and refresh both thread files."""
    wrote = remember_thread_url(conn, job_key, text)
    record_message_processed(
        conn,
        message_id,
        outcome="LINKED",
        subject=subject,
        from_address=from_address,
        lead_keys=[job_key],
    )
    _ensure_conversation(
        conn,
        job_key,
        message_id=message_id,
        channel=channel,
        direction="inbound",
        summary=subject,
        body=text,
    )
    archive_thread_histories(conn, job_key, output_root=output_root)
    return wrote


def archive_thread_histories(
    conn: sqlite3.Connection, job_key: str, *, output_root: Path | None = None
) -> list[Path]:
    """Write the LinkedIn thread and the email thread under
    `<Company>/<Title>/communications/`."""
    lead = conn.execute(
        "SELECT company, title FROM job_leads WHERE normalized_key = ?", (job_key,)
    ).fetchone()
    if lead is None:
        return []
    company, title = lead["company"] or "", lead["title"] or ""
    if not company or not title:
        return []
    root = output_root or DEFAULT_OUTPUT_ROOT
    siblings = tuple(get_sibling_titles(conn, company, exclude_title=title))
    comms = _job_folder(
        root, company=company, title=title, multi_lead=bool(siblings), sibling_titles=siblings
    ) / "communications"
    convos = conn.execute(
        """
        SELECT message_id, channel, direction, summary, occurred_at, body_text
        FROM job_conversations
        WHERE job_key = ?
        ORDER BY occurred_at, id
        """,
        (job_key,),
    ).fetchall()
    linkedin: list[str] = []
    email: list[str] = []
    for convo in convos:
        block = _format_history_entry(convo)
        message_id = convo["message_id"] or ""
        channel = (convo["channel"] or "").lower()
        if channel == "linkedin" or message_id.startswith("paste:"):
            linkedin.append(block)
        else:
            email.append(block)
    written: list[Path] = []
    if linkedin:
        written.append(_write_thread_file(conn, job_key, comms, "LinkedIn_thread.txt", "linkedin_thread", linkedin))
    if email:
        written.append(_write_thread_file(conn, job_key, comms, "Email_thread.txt", "email_thread", email))
    return written


def link_historic_same_pitches(
    conn: sqlite3.Connection, *, apply: bool = True, output_root: Path | None = None
) -> list[dict]:
    """Pair leads and parked mail already in the database.

    A pasted thread and the Gmail notification of it can both already be
    stored, under two leads or with the mail still unmatched. The paste
    lead is the one kept when either side is a paste. The other lead is
    skipped as its duplicate, and a thread link on the skipped lead is
    copied onto the paste. Parked mail that is the same pitch is resolved
    onto that lead without logging a second conversation.
    """
    leads = _load_live_leads(conn)
    parent = {key: key for key in leads}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    keys = list(leads)
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            if _same_stored_pitch(leads[left], leads[right]):
                union(left, right)

    actions: list[dict] = []
    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(find(key), []).append(key)
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = _pick_survivor(leads, group)
        for other in group:
            if other == keeper or not _same_stored_pitch(leads[keeper], leads[other]):
                continue
            if apply:
                _rehome_lead(conn, keeper, other)
                for body in leads[other]["bodies"]:
                    remember_thread_url(conn, keeper, body)
                _copy_reply_to(conn, keeper, other)
                archive_thread_histories(conn, keeper, output_root=output_root)
                mark_duplicate(conn, other, duplicate_of_key=keeper)
            actions.append({"kept": keeper, "skipped": other})

    for key, info in _load_live_leads(conn).items():
        if not info["names"]:
            continue
        if apply:
            wrote = claim_unmatched_same_pitch(
                conn, key, info["names"][0], info["text"], output_root=output_root
            )
        else:
            wrote = _would_claim_unmatched(conn, info["names"][0], info["text"])
        if wrote:
            actions.append({"kept": key, "claimed_unmatched": True})
    return actions


def _rehome_lead(conn: sqlite3.Connection, keep: str, absorb: str) -> None:
    """Move the absorbed lead's history onto the lead that is kept."""
    for table in (
        "job_contacts",
        "job_conversations",
        "job_documents",
        "job_meetings",
        "job_offers",
        "lead_skills",
        "lead_skill_extractions",
    ):
        try:
            conn.execute(f"UPDATE {table} SET job_key = ? WHERE job_key = ?", (keep, absorb))
        except sqlite3.OperationalError:
            continue
    try:
        conn.execute(
            "UPDATE unmatched_messages SET resolved_job_key = ? WHERE resolved_job_key = ?",
            (keep, absorb),
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _ensure_conversation(
    conn: sqlite3.Connection,
    job_key: str,
    *,
    message_id: str,
    channel: str,
    direction: str,
    summary: str,
    body: str,
    occurred_at: str = "",
    thread_id: str = "",
) -> None:
    if not message_id:
        return
    existing = conn.execute(
        "SELECT 1 FROM job_conversations WHERE job_key = ? AND message_id = ?",
        (job_key, message_id),
    ).fetchone()
    if existing is not None:
        return
    add_job_conversation(
        conn,
        JobConversation(
            job_key=job_key,
            message_id=message_id,
            channel=channel,
            direction=direction,
            summary=(summary or "")[:180],
            occurred_at=occurred_at or utc_now_iso(),
            thread_id=thread_id,
            body_text=body,
        ),
    )


def _format_history_entry(convo) -> str:
    return "\n".join(
        [
            f"[{convo['occurred_at'] or ''}] {convo['direction'] or 'inbound'} {convo['channel'] or ''}".rstrip(),
            f"Message-Id: {convo['message_id'] or ''}",
            f"Summary: {convo['summary'] or ''}",
            "",
            convo["body_text"] or "",
        ]
    ).rstrip()


def _write_thread_file(
    conn: sqlite3.Connection,
    job_key: str,
    comms: Path,
    filename: str,
    doc_type: str,
    blocks: list[str],
) -> Path:
    comms.mkdir(parents=True, exist_ok=True)
    path = comms / filename
    path.write_text("\n\n----\n\n".join(blocks) + "\n", encoding="utf-8")
    already = conn.execute(
        "SELECT 1 FROM job_documents WHERE job_key = ? AND path_or_url = ?",
        (job_key, str(path)),
    ).fetchone()
    if already is None:
        add_job_document(
            conn, JobDocument(job_key=job_key, doc_type=doc_type, path_or_url=str(path))
        )
    return path


def _keys_for_recruiter(conn: sqlite3.Connection, name: str) -> set[str]:
    rows = _contact_rows(conn)
    keys: set[str] = set()
    for row in rows:
        via = _TBD_VIA_RE.match((row["company"] or "").strip())
        via_name = via.group(1).strip() if via else ""
        if names_compatible(name, row["name"] or "") or names_compatible(name, via_name):
            keys.add(row["job_key"])
    return keys


def _contact_rows(conn: sqlite3.Connection):
    queries = (
        """
        SELECT c.job_key, c.name, l.company
        FROM job_contacts c
        JOIN job_leads l ON l.normalized_key = c.job_key
        WHERE ifnull(l.deleted_at, '') = ''
          AND ifnull(l.status, '') != 'skipped'
          AND ifnull(l.duplicate_of_key, '') = ''
        """,
        """
        SELECT c.job_key, c.name, l.company
        FROM job_contacts c
        JOIN job_leads l ON l.normalized_key = c.job_key
        WHERE ifnull(l.deleted_at, '') = ''
          AND ifnull(l.status, '') != 'skipped'
        """,
        """
        SELECT c.job_key, c.name, l.company
        FROM job_contacts c
        JOIN job_leads l ON l.normalized_key = c.job_key
        """,
    )
    for query in queries:
        try:
            return conn.execute(query).fetchall()
        except sqlite3.OperationalError:
            continue
    return []


def _load_live_leads(conn: sqlite3.Connection) -> dict[str, dict]:
    queries = (
        """
        SELECT normalized_key, company, first_seen, source_message_id
        FROM job_leads
        WHERE ifnull(deleted_at, '') = ''
          AND ifnull(status, '') != 'skipped'
          AND ifnull(duplicate_of_key, '') = ''
        """,
        """
        SELECT normalized_key, company, first_seen, source_message_id
        FROM job_leads
        WHERE ifnull(deleted_at, '') = ''
          AND ifnull(status, '') != 'skipped'
        """,
    )
    rows = None
    for query in queries:
        try:
            rows = conn.execute(query).fetchall()
            break
        except sqlite3.OperationalError:
            continue
    if rows is None:
        return {}
    leads: dict[str, dict] = {}
    for row in rows:
        key = row["normalized_key"]
        contacts = conn.execute(
            "SELECT name FROM job_contacts WHERE job_key = ?", (key,)
        ).fetchall()
        convos = conn.execute(
            """
            SELECT message_id, summary, body_text FROM job_conversations
            WHERE job_key = ? AND direction = 'inbound'
            """,
            (key,),
        ).fetchall()
        bodies = [c["body_text"] or "" for c in convos if (c["body_text"] or "").strip()]
        via = _TBD_VIA_RE.match((row["company"] or "").strip())
        names = [c["name"].strip() for c in contacts if (c["name"] or "").strip()]
        if via:
            names.append(via.group(1).strip())
        paste = (row["source_message_id"] or "").startswith("paste:") or any(
            (c["message_id"] or "").startswith("paste:") for c in convos
        )
        text = "\n".join(bodies)
        url = extract_linkedin_thread_url(text)
        leads[key] = {
            "names": names,
            "bodies": bodies,
            "text": text,
            "paste": paste,
            "url": url,
            "first_seen": row["first_seen"] or "",
        }
    return leads


def _same_stored_pitch(left: dict, right: dict) -> bool:
    if not _pair_names_ok(left, right):
        return False
    return any(pitches_overlap(a, b) for a in left["bodies"] for b in right["bodies"])


def _pair_names_ok(left: dict, right: dict) -> bool:
    left_names, right_names = left["names"], right["names"]
    if left_names and right_names:
        return any(names_compatible(a, b) for a in left_names for b in right_names)
    named, other = (left, right) if left_names else (right, left)
    if not named["names"]:
        return False
    first = _tokens(named["names"][0])
    if not first or len(first[0]) < 3:
        return False
    blob = " ".join(other["bodies"]).lower()
    return re.search(rf"\b{re.escape(first[0])}\b", blob) is not None


def _pick_survivor(leads: dict[str, dict], group: list[str]) -> str:
    """Keep the pasted lead when one side was pasted. Otherwise keep the
    earliest lead, preferring one that already has the messaging link."""
    return min(
        group,
        key=lambda key: (
            0 if leads[key]["paste"] else 1,
            0 if leads[key]["url"] else 1,
            leads[key]["first_seen"],
            key,
        ),
    )


def _copy_reply_to(conn: sqlite3.Connection, keep: str, absorb: str) -> None:
    try:
        incoming = conn.execute(
            "SELECT linkedin_reply_to FROM job_contacts WHERE job_key = ?", (absorb,)
        ).fetchall()
        reply = next((row["linkedin_reply_to"].strip() for row in incoming if (row["linkedin_reply_to"] or "").strip()), "")
        if not reply:
            return
        have = conn.execute(
            "SELECT id, linkedin_reply_to FROM job_contacts WHERE job_key = ?", (keep,)
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if not have or any((row["linkedin_reply_to"] or "").strip() for row in have):
        return
    conn.execute(
        "UPDATE job_contacts SET linkedin_reply_to = ? WHERE id = ?",
        (reply, have[0]["id"]),
    )
    conn.commit()


def _would_claim_unmatched(conn: sqlite3.Connection, name: str, text: str) -> bool:
    rows = conn.execute(
        """
        SELECT subject, body_text FROM unmatched_messages
        WHERE resolved_at IS NULL
          AND (dismissed_at IS NULL OR dismissed_at = '')
          AND (superseded_by IS NULL OR superseded_by = '')
        """
    ).fetchall()
    return any(_unmatched_is_same(name, text, row["subject"] or "", row["body_text"] or "") for row in rows)


def _lead_pitch_overlaps(conn: sqlite3.Connection, job_key: str, text: str) -> bool:
    rows = conn.execute(
        "SELECT body_text FROM job_conversations WHERE job_key = ? AND direction = 'inbound'",
        (job_key,),
    )
    return any(pitches_overlap(text, row["body_text"] or "") for row in rows)


def _unmatched_is_same(name: str, text: str, subject: str, body: str) -> bool:
    if not pitches_overlap(text, body):
        return False
    other = recruiter_name_for_mail(subject, body)
    if other:
        return names_compatible(name, other)
    first = _tokens(name)[:1]
    blob = f"{subject}\n{body}".lower()
    return bool(first) and len(first[0]) >= 3 and re.search(rf"\b{re.escape(first[0])}\b", blob) is not None
