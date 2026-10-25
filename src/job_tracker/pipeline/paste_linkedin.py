"""Turn a pasted LinkedIn thread into one inbound lead.

Shawn copies the thread out of LinkedIn and posts it here (see
`scripts/paste_linkedin_lead.py` and `POST /api/linkedin-lead`). The row
matches a scanned InMail: `source_label=linkedin_message`, the paste stored
as the conversation body, channel `linkedin`. A messaging URL in that body
is the thread Pending Actions opens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from job_tracker.cli.record_unmatched_reply import stub_lead_names
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead
from job_tracker.pipeline.pitch_match import (
    absorb_linked_copy,
    claim_unmatched_same_pitch,
    find_lead_for_same_pitch,
    remember_thread_url,
)
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

logger = logging.getLogger(__name__)

_PASTE_PROMPT = """You read one pasted LinkedIn messaging thread. Shawn Becker is the candidate. The other person is the recruiter.

The paste mixes LinkedIn's own chrome (profile headlines, "sent the following messages", reaction emoji, "View profile", connection degree, pronouns, clock times) with the recruiter's actual messages.

Return ONLY a JSON object with these keys:
- "recruiter_name": the recruiter's name as shown, or ""
- "company": the hiring company or end client, only when the messages name one. The firm a recruiter says they are from, and a product named inside the role ("with Cortex AI Role"), are not the company. "" when the client is not named.
- "title": the role they are offering, in their words, or ""
- "message": one string of the recruiter's messages in order, separated by a blank line, chrome removed. Keep the pitch. Leave out Shawn's side, emoji, and profile boilerplate.
- "apply_url": a job link only if one is in the paste, else ""

Never invent a company or title that the text does not state. Respond with the JSON object only."""

_MIN_CHARS = 40
_TBD_TITLE = "Role TBD"
# Messaging-pane copies use a single initial ("Jaideep S") and glue pronouns
# and a clock time onto the same line ("Jaideep S  (He/Him)  6:00 PM").
_NAME_TOKEN = r"[A-Z][\w.'’\-]*"
_PERSON_LINE_RE = re.compile(rf"^{_NAME_TOKEN}(?: {_NAME_TOKEN}){{1,3}}$")
_NOT_A_NAME_RE = re.compile(
    r"(?i)\b(engineer|developer|recruiter|position|remote|contract|linkedin|hiring|opportunity|profile|message|connection)\b"
)
_PRONOUN_RE = re.compile(r"\s*\((?:He|She|They)/[\w]+\)", re.I)
_CLOCK_RE = re.compile(r"\s+\d{1,2}:\d{2}\s*[AP]M\s*$", re.I)
_SENT_MESSAGES_RE = re.compile(r"\s+sent the following messages\b.*$", re.I)
_ROLE_NOUN = (
    r"(?:Engineers|Developers|Architects|Scientists|Analysts|Programmers|Administrators|Engineer|"
    r"Developer|Architect|Scientist|Analyst|Programmer|Administrator|DBA|SRE)"
)
# "I'm looking Data Engineer (Databricks & Cloud Analytics), Salt Lake City..."
_LOOKING_TITLE_RE = re.compile(
    rf"(?i)\b(?:looking(?:\s+for)?|hiring(?:\s+for)?|opening(?:\s+for)?)\s+(?:an?\s+)?"
    rf"((?-i:[A-Z])[\w+.#&/()'’ \-]{{0,60}}?{_ROLE_NOUN}(?:\s*\([^)]{{0,80}}\))?)"
)


def paste_message_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    return f"paste:{digest}"


def post_pasted_thread(
    conn, text: str, *, use_llm: bool = True, llm_client=None, output_root: Path | None = None
) -> dict:
    """Insert the paste as an inbound LinkedIn conversation, or report that
    this thread is already on a lead. One LLM read interprets the thread.
    The sender-line and Position/agency checks fill any field that read
    leaves blank, and they are the whole read when the call fails."""
    text = _normalize_paste(text)
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
    seen = conn.execute(
        "SELECT lead_keys FROM processed_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    if seen is not None and seen["lead_keys"] and seen["lead_keys"] != "[]":
        keys = json.loads(seen["lead_keys"])
        if keys:
            return _already(conn, keys[0], thread_url=thread_url, message_id=message_id)

    sig = parse_signature(text)
    read = interpret_pasted_thread(text, client=llm_client) if use_llm else {}
    role = extract_role_heuristic(text)
    name = (read.get("recruiter_name") or _recruiter_name(text, sig.name)).strip()
    title = (read.get("title") or (role.title if role else "") or _title_from_paste(text)).strip()
    company = _hiring_company((read.get("company") or (role.company if role else "")).strip(), text)
    if not company and (name or title or thread_url):
        company = stub_lead_names(name)[0]
    if not title and (name or thread_url or company):
        title = _TBD_TITLE
    if not (company and title):
        return {
            "ok": False,
            "error": "Couldn't find a company and title, or a recruiter, in that paste.",
        }
    confidence = 0.8 if read.get("title") or read.get("company") or read.get("recruiter_name") else (
        role.confidence if role else 0.4
    )
    pitch = (read.get("message") or "").strip()
    body = pitch or text
    if thread_url and thread_url not in body:
        body = f"{body}\n{thread_url}"
    apply_url = (read.get("apply_url") or (role.apply_url if role else "") or "").strip()
    pitch_text = pitch or text
    existing = find_lead_for_same_pitch(conn, name=name, text=pitch_text)
    if existing:
        wrote = remember_thread_url(conn, existing, text)
        wrote = claim_unmatched_same_pitch(
            conn, existing, name, pitch_text, output_root=output_root
        ) or wrote
        absorb_linked_copy(
            conn,
            existing,
            message_id,
            text,
            subject=title,
            channel="linkedin",
            output_root=output_root,
        )
        result = _already(conn, existing, thread_url=thread_url or extract_linkedin_thread_url(text), message_id=message_id)
        result["changed"] = wrote
        result["recruiterName"] = name
        return result

    real_role = title != _TBD_TITLE and not company.startswith("End-client TBD")
    match = find_matching_job(conn, company, title) if real_role else None
    created = False
    if match is not None:
        job_key = match.normalized_key
        company, title = match.company, match.title
    else:
        jd_text = _strip_linkedin_footer(pitch or text).strip()
        score = score_jd(jd_text) if jd_text else None
        lead = JobLead(
            company=company,
            title=title,
            source_message_id=message_id,
            source_label="linkedin_message",
            apply_url=apply_url,
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
            body_text=body,
        ),
    )
    claim_unmatched_same_pitch(conn, job_key, name, pitch_text, output_root=output_root)
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


def interpret_pasted_thread(text: str, *, client=None, model: str | None = None) -> dict:
    """One model read of a pasted thread. Empty fields on any failure, so the
    caller can fall back to the sender-line and Position checks."""
    from job_tracker.pipeline.llm_call_log import log_call
    from job_tracker.pipeline.llm_extract import DEFAULT_MODEL, _client, _cost_usd

    empty = {"recruiter_name": "", "company": "", "title": "", "message": "", "apply_url": ""}
    try:
        client = client or _client()
        model = model or DEFAULT_MODEL
        response = client.messages.create(
            model=model,
            max_tokens=1200,
            system=_PASTE_PROMPT,
            messages=[{"role": "user", "content": text[:12_000]}],
        )
    except Exception:
        logger.warning("Pasted LinkedIn thread could not be read", exc_info=True)
        return empty
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    log_call(
        step="paste_linkedin",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_cost_usd(model, input_tokens, output_tokens),
    )
    raw = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    data = _parse_object(raw)
    if not isinstance(data, dict):
        return empty
    return {key: _as_text(data.get(key)) for key in empty}


def _as_text(value) -> str:
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    return str(value or "").strip()


def _parse_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    if not text:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = next((item for item in data if isinstance(item, dict)), None)
    return data if isinstance(data, dict) else None


def _normalize_paste(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("’", "'")
    return text.strip()


def _hiring_company(company: str, text: str) -> str:
    """Drop a firm the recruiter works at, or a product named inside the role.

    The model sometimes returns those because they are the only proper nouns
    in the paste. The end client is then left blank for the TBD stub.
    """
    firm = (company or "").strip()
    if not firm or firm.lower().startswith("end-client tbd"):
        return firm
    head = firm.split()[0]
    if len(head) >= 4 and re.search(rf"(?i)\bfrom\s+{re.escape(head)}\b", text):
        if not re.search(rf"(?i)\b(?:at|for)\s+{re.escape(head)}\b", text):
            return ""
    if re.search(rf"(?i)\bwith\s+{re.escape(firm)}\s+role\b", text):
        return ""
    return firm


def _title_from_paste(text: str) -> str:
    match = _LOOKING_TITLE_RE.search(text)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip(" -–—,;:")
    return title[:120]


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
    for raw in text.splitlines():
        line = _PRONOUN_RE.sub("", raw)
        line = _CLOCK_RE.sub("", line)
        line = _SENT_MESSAGES_RE.sub("", line).strip()
        if _PERSON_LINE_RE.match(line) and not _NOT_A_NAME_RE.search(line):
            return line
    return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "LinkedIn message"
