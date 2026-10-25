"""Turn a pasted LinkedIn thread into one inbound lead.

Shawn copies the thread out of LinkedIn and posts it here (see
`scripts/paste_linkedin_lead.py` and `POST /api/linkedin-lead`). The row
matches a scanned InMail: `source_label=linkedin_message`, the paste stored
as the conversation body, channel `linkedin`. A messaging URL in that body
is the thread Pending Actions opens.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from job_tracker.cli.record_unmatched_reply import stub_lead_names
from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead
from job_tracker.pipeline.qualifying_reply import (
    _strip_linkedin_footer,
    extract_linkedin_sender_name,
    extract_linkedin_thread_url,
    extract_role_heuristic,
)
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    find_job_by_linkedin_thread,
    find_matching_job,
    linkedin_thread_key,
    upsert_lead,
    utc_now_iso,
)
from job_tracker.scoring.scorer import score_jd

_MIN_CHARS = 40
_TBD_TITLE = "Role TBD"
# A messaging-pane copy starts with the person's name and has no "Reply" line.
_PERSON_LINE_RE = re.compile(r"^[A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){1,3}$")
_NOT_A_NAME_RE = re.compile(
    r"(?i)\b(engineer|developer|recruiter|position|remote|contract|linkedin|hiring|opportunity)\b"
)


def paste_message_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"paste:{digest}"


def post_pasted_thread(conn, text: str, *, use_llm: bool = True, llm_client=None) -> dict:
    """Insert the paste as an inbound LinkedIn conversation, or report that
    this thread is already on a lead. `use_llm` asks for a company and title
    only when the free heuristic finds neither."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < _MIN_CHARS or (" " not in text and "\n" not in text):
        return {
            "ok": False,
            "error": "Paste the LinkedIn message, not a bare link. Copy the thread in LinkedIn, then run this again.",
        }

    message_id = paste_message_id(text)
    thread_url = extract_linkedin_thread_url(text)
    thread_key = linkedin_thread_key(text)
    if thread_key:
        existing_key = find_job_by_linkedin_thread(conn, thread_key)
        if existing_key:
            return _already(conn, existing_key, thread_url=thread_url, message_id=message_id)

    prior = conn.execute(
        "SELECT job_key FROM job_conversations WHERE message_id = ?", (message_id,)
    ).fetchone()
    if prior is not None:
        return _already(conn, prior["job_key"], thread_url=thread_url, message_id=message_id)

    sig = parse_signature(text)
    name = _recruiter_name(text, sig.name)
    role = extract_role_heuristic(text)
    if role is None and use_llm:
        role = _role_from_llm(text, message_id, client=llm_client)

    if role is not None and role.company and role.title:
        company, title = role.company, role.title
        confidence = role.confidence
    elif name or thread_url:
        company, title = stub_lead_names(name)
        confidence = 0.4
    else:
        return {
            "ok": False,
            "error": "Couldn't find a company and title, or a recruiter, in that paste.",
        }

    real_role = title != _TBD_TITLE and not company.startswith("End-client TBD")
    match = find_matching_job(conn, company, title) if real_role else None
    created = False
    if match is not None:
        job_key = match.normalized_key
        company, title = match.company, match.title
    else:
        jd_text = _strip_linkedin_footer(text).strip()
        score = score_jd(jd_text) if jd_text else None
        lead = JobLead(
            company=company,
            title=title,
            source_message_id=message_id,
            source_label="linkedin_message",
            apply_url=(role.apply_url if role else "") or "",
            extraction_confidence=confidence,
            jd_resolved=bool(jd_text),
            jd_source="email_body" if jd_text else "",
            jd_text=jd_text,
            match_pct=score.match_pct if score else 0.0,
            matched_skills=list(score.matched_skills) if score else [],
            verdict=score.verdict if score else "review",
            rationale=list(score.rationale) if score else ["Created from a pasted LinkedIn thread"],
        )
        created = upsert_lead(conn, lead)
        job_key = lead.normalized_key

    contact_id = None
    if name or sig.email or sig.phone:
        contact_id = add_job_contact(
            conn,
            JobContact(
                job_key=job_key,
                name=name,
                email=sig.email,
                phone=sig.phone,
                role="recruiter",
                source_message_id=message_id,
            ),
        )

    summary = title if title != _TBD_TITLE else _first_line(text)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=job_key,
            contact_id=contact_id,
            message_id=message_id,
            channel="linkedin",
            direction="inbound",
            summary=summary[:180],
            occurred_at=utc_now_iso(),
            body_text=text,
        ),
    )
    status = _status(conn, job_key)
    return {
        "ok": True,
        "created": created,
        "changed": True,
        "jobKey": job_key,
        "company": company,
        "title": title,
        "status": status,
        "threadUrl": thread_url,
        "recruiterName": name,
        "messageId": message_id,
    }


def refresh_pending_actions(db_path: Path | None = None) -> bool:
    """Regenerate Pending Actions JSON so a paste shows up without waiting
    for the 3-minute tick. Returns False when the renderer fails; the lead
    is already stored either way."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "render_pending_actions.py"
    cmd = [sys.executable, str(script), "--no-rescore"]
    if db_path is not None:
        cmd += ["--db", str(db_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.returncode == 0


def _role_from_llm(text: str, message_id: str, *, client=None) -> ExtractedRole | None:
    from job_tracker.pipeline.llm_extract import extract_roles_llm

    message = EmailMessage(id=message_id, from_address="", subject="", body_plain=text)
    for role in extract_roles_llm(message, client=client):
        if role.company and role.title:
            return role
    return None


def _already(conn, job_key: str, *, thread_url: str, message_id: str) -> dict:
    row = conn.execute(
        "SELECT company, title, status FROM job_leads WHERE normalized_key = ?", (job_key,)
    ).fetchone()
    company = row["company"] if row else ""
    title = row["title"] if row else ""
    status = row["status"] if row else ""
    return {
        "ok": True,
        "created": False,
        "changed": False,
        "jobKey": job_key,
        "company": company,
        "title": title,
        "status": status,
        "threadUrl": thread_url,
        "recruiterName": "",
        "messageId": message_id,
    }


def _status(conn, job_key: str) -> str:
    row = conn.execute(
        "SELECT status FROM job_leads WHERE normalized_key = ?", (job_key,)
    ).fetchone()
    return row["status"] if row else ""


def _recruiter_name(text: str, signature_name: str) -> str:
    name = (signature_name or extract_linkedin_sender_name(text)).strip()
    if name:
        return name
    for line in text.splitlines():
        line = line.strip()
        if _PERSON_LINE_RE.match(line) and not _NOT_A_NAME_RE.search(line):
            return line
    return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "LinkedIn message"
