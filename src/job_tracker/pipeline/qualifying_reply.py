"""Template qualifying replies for LinkedIn InMail pitches (2026-08-04).

Goal: make the "ask W2 / end client / remote before sending a résumé" loop
zero-compose for Shawn — draft text + thread URL land in pending-actions
with a one-click Copy button. Never auto-sends.

Also: free heuristic extraction of Position + agency/company from common
InMail shapes (e.g. Madhvendra / KPG99) so those pitches become stub leads
without waiting on LLM fallback.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from job_tracker.email.models import ExtractedRole
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline import store
from job_tracker.scoring.scorer import score_jd

_THREAD_URL_RE = re.compile(
    r"(https://www\.linkedin\.com/messaging/thread/\S+)", re.I
)
_SENDER_NAME_RE = re.compile(
    r"\n\s*([A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){0,3})\n\s*Reply\n",
)
_POSITION_RE = re.compile(
    r"(?im)^(?:Position|Role|Title)\s*[:\-]\s*(.+?)\s*$"
)
_COMPANY_LINE_RE = re.compile(
    r"(?m)^([A-Z0-9][\w.&,'/\- ]{1,60}(?:Inc\.?|LLC|Corp\.?|Ltd\.?|INC))\s*$"
)
_RECRUITER_AT_RE = re.compile(
    r"(?im)^(?:Sr\.?\s+|Senior\s+)?(?:Technical\s+)?Recruiter\s+at\s+(.+?)\s*$"
)
_VIA_AGENCY_RE = re.compile(
    r"(?im)^(?:Best regards|Thanks|Thank you|Regards),?\s*\n"
    r"[A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){0,3}\s*\n"
    r".*\n"
    r"([A-Z0-9][\w.&'/\- ]{1,60}(?:Inc\.?|LLC|Corp\.?|Ltd\.?|INC))\s*$",
)

# Signals that engagement type / location / client are already answered.
_ENGAGEMENT_KNOWN_RE = re.compile(
    r"\b(w2|1099|c2c|corp[\s\-]?to[\s\-]?corp|full[\s\-]?time|fte|permanent)\b",
    re.I,
)
_REMOTE_KNOWN_RE = re.compile(
    r"^Location\s*[:\-].*\bremote\b|"
    r"\b(100%\s*remote|fully\s*remote|remote\s*only)\b|"
    r"^Type\s*[:\-].*\bremote\b|"
    r"^Location\s*[:\-]\s*Remote\b",
    re.I | re.M,
)
_END_CLIENT_KNOWN_RE = re.compile(
    r"(?im)^(?:End[\s\-]?[Cc]lient|Client|Company)\s*[:\-]\s*(?!\s*(?:TBD|unknown|unnamed)\b)\S+",
)
_RATE_ASKED_OR_GIVEN_RE = re.compile(
    r"(?i)\b(rate|\$\s*\d|/hr|per\s*hour|salary|compensation\s*range)\b",
)
_JD_SUBSTANCE_RE = re.compile(
    # Deliberately omit bare "job description" — thin InMails say
    # "I've attached the job description" with no substance (Evan T.).
    r"\b(required skills|responsibilities|qualifications|years of experience|"
    r"must have|what you.?ll do)\b|"
    r"^(?:Position|Role|Title)\s*[:\-]",
    re.I | re.M,
)


@dataclass
class PitchGaps:
    needs_jd: bool = False
    needs_end_client: bool = True
    needs_engagement: bool = True
    needs_remote: bool = True
    needs_rate_band: bool = True


@dataclass
class QualifyingDraft:
    recruiter_name: str = ""
    first_name: str = ""
    thread_url: str = ""
    gaps: PitchGaps = field(default_factory=PitchGaps)
    body: str = ""
    company_guess: str = ""
    title_guess: str = ""


def extract_linkedin_thread_url(text: str) -> str:
    if not text:
        return ""
    m = _THREAD_URL_RE.search(text.replace("\r\n", "\n"))
    return (m.group(1).rstrip(").,]'\"")) if m else ""


def extract_linkedin_sender_name(text: str) -> str:
    if not text:
        return ""
    sig = parse_signature(text)
    if sig.name:
        return sig.name
    m = _SENDER_NAME_RE.search("\n" + text.replace("\r\n", "\n"))
    return m.group(1).strip() if m else ""


def extract_role_heuristic(text: str) -> ExtractedRole | None:
    """Free Position:/agency extraction for structured InMail pitches.

    Returns None unless BOTH a title and a company/agency can be recovered —
    same bar as llm_new_lead so we don't create junk stubs.
    """
    if not text:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    title = ""
    m = _POSITION_RE.search(normalized)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip(" -–—|")
    if not title or len(title) < 4 or len(title) > 120:
        return None

    company = ""
    at = _RECRUITER_AT_RE.search(normalized)
    if at:
        company = at.group(1).strip(" ,.-")
    if not company:
        # Prefer Inc/LLC lines near the signature (last 40% of the body).
        tail = normalized[len(normalized) // 2 :]
        hits = list(_COMPANY_LINE_RE.finditer(tail))
        if hits:
            company = hits[-1].group(1).strip(" ,.-")
    if not company:
        via = _VIA_AGENCY_RE.search(normalized)
        if via:
            company = via.group(1).strip(" ,.-")

    # Email domain fallback (kpgtech.com → KPG Tech) only when we already
    # have a title and a recruiter corporate email — weaker than an Inc line.
    if not company:
        sig = parse_signature(normalized)
        if sig.email and "@" in sig.email:
            domain = sig.email.split("@", 1)[1].lower()
            root = domain.split(".")[0]
            if root not in {"gmail", "yahoo", "outlook", "hotmail", "linkedin", "aol"}:
                company = root.upper() if len(root) <= 6 else root.title()

    if not company or len(company) < 2:
        return None

    # Prefer JD slice starting at Position: when present.
    snippet = normalized
    pos = _POSITION_RE.search(normalized)
    if pos:
        snippet = normalized[pos.start() :].strip()
        # Cut LinkedIn footer chrome
        for marker in ("This email was intended", "Learn why we included", "Unsubscribe:"):
            idx = snippet.find(marker)
            if idx > 80:
                snippet = snippet[:idx].strip()
                break

    return ExtractedRole(
        company=company,
        title=title,
        source="linkedin_heuristic",
        confidence=0.7,
        snippet=snippet[:8000],
    )


def detect_pitch_gaps(text: str, *, subject: str = "") -> PitchGaps:
    blob = f"{subject}\n{text or ''}"
    skill_hits = len(
        re.findall(r"(?i)\b(aws|python|java|spark|sql|llm|data|glue|emr)\b", blob)
    )
    has_jd = bool(_JD_SUBSTANCE_RE.search(blob)) or skill_hits >= 4
    gaps = PitchGaps(
        needs_jd=not has_jd,
        needs_end_client=not bool(_END_CLIENT_KNOWN_RE.search(blob)),
        needs_engagement=not bool(_ENGAGEMENT_KNOWN_RE.search(blob)),
        needs_remote=not bool(_REMOTE_KNOWN_RE.search(blob)),
        needs_rate_band=not bool(_RATE_ASKED_OR_GIVEN_RE.search(blob)),
    )
    # Location: Remote settles remote; Type: Contract does NOT settle W2 vs C2C.
    if re.search(r"(?im)^Location\s*[:\-]\s*Remote\b", blob):
        gaps.needs_remote = False
    if re.search(r"(?i)\b(w2|1099|c2c)\b", blob):
        gaps.needs_engagement = False
    return gaps


def draft_qualifying_reply(text: str, *, subject: str = "", known_recruiter_name: str = "") -> QualifyingDraft:
    """`known_recruiter_name` (added 2026-08-25): when the caller already has
    a confirmed name for this recruiter on file (`job_contacts.name`, via
    `pending_workflow._recruiter_contact`), pass it here so the greeting uses
    it instead of relying solely on this function's own regex/signature
    extraction from the raw message text — that extraction is tuned for
    LinkedIn InMail chrome ("Name\\nReply\\n") and a plain email's sign-off
    often doesn't match it, silently falling back to a bare "Hi," even
    though the lead's contact card already has the right name (surfaced by
    a PurpleLab/Carlos Delgado lead whose reply greeted no one by name)."""
    extracted_name = extract_linkedin_sender_name(text)
    name = known_recruiter_name.strip() or extracted_name
    first = name.split()[0] if name else ""
    # Drop trailing initial like "Evan" from "Evan T."
    if first.endswith("."):
        first = first[:-1]
    gaps = detect_pitch_gaps(text, subject=subject)
    role = extract_role_heuristic(text)

    asks: list[str] = []
    if gaps.needs_jd:
        asks.append(
            "Could you paste the job description into the thread (LinkedIn attachments don't always come through on my side)?"
        )
    if gaps.needs_end_client:
        asks.append("Who is the end client / hiring company?")
    if gaps.needs_engagement:
        asks.append(
            "Is the engagement W2 or 1099 direct-to-individual? I'm not set up for C2C."
        )
    if gaps.needs_remote:
        asks.append("Is the role fully remote (US), or is there an onsite/hybrid requirement?")
    if gaps.needs_rate_band:
        asks.append("What's the rate or salary band for the role?")

    if not asks:
        asks.append(
            "Happy to take a look — could you confirm end client, W2 vs 1099-direct (not C2C), remote status, and the rate/salary band?"
        )

    if len(asks) == 1:
        ask_block = asks[0]
    else:
        ask_block = "A few quick clarifiers before I send a résumé:\n" + "\n".join(
            f"- {a}" for a in asks
        )

    greeting = f"Hi {first}," if first else "Hi,"
    interest = "Thanks for reaching out"
    if role and role.title:
        interest += f" about the {role.title} role"
    interest += "."

    body = f"{greeting}\n\n{interest} {ask_block}\n\nBest, Shawn"
    # House-rule safety: never invent a dollar figure; asking for their band is fine.
    return QualifyingDraft(
        recruiter_name=name,
        first_name=first,
        thread_url=extract_linkedin_thread_url(text),
        gaps=gaps,
        body=body,
        company_guess=role.company if role else "",
        title_guess=role.title if role else "",
    )


def promote_heuristic_unmatched(conn: sqlite3.Connection) -> list[str]:
    """Backfill: unmatched InMails with clear Position+company → stub lead + resolve.

    Called from pending-actions render so parked Madhvendra-class pitches
    don't wait for the next scan cycle. Returns promoted job_keys.
    """
    promoted: list[str] = []
    for row in store.list_unmatched_messages(conn):
        body = row["body_text"] or ""
        role = extract_role_heuristic(body)
        if role is None:
            continue
        existing = store.find_matching_job(conn, role.company, role.title)
        if existing:
            job_key = existing.normalized_key
        else:
            jd_text = (role.snippet or body).strip()
            score = score_jd(jd_text) if jd_text else None
            lead = JobLead(
                company=role.company,
                title=role.title,
                source_message_id=row["message_id"],
                source_label="linkedin_message",
                extraction_confidence=role.confidence,
                jd_resolved=bool(jd_text),
                jd_source="email_body" if jd_text else "",
                jd_text=jd_text,
                match_pct=score.match_pct if score else 0.0,
                matched_skills=list(score.matched_skills) if score else [],
                verdict=score.verdict if score else "review",
                rationale=list(score.rationale) if score else [
                    "Promoted from unmatched LinkedIn pitch (heuristic Position/company)"
                ],
            )
            store.upsert_lead(conn, lead)
            job_key = lead.normalized_key

        sig = parse_signature(body)
        store.resolve_unmatched_message(
            conn,
            row["message_id"],
            job_key,
            contact_name=sig.name or extract_linkedin_sender_name(body),
            contact_email=sig.email or "",
            contact_phone=sig.phone or "",
        )
        promoted.append(job_key)
    return promoted
