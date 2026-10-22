"""Template qualifying replies for LinkedIn InMail pitches (2026-08-04).

Two first-touch tracks:

- **JD-first** (default): ask W2 / end client / remote / rate (and a pasted
  JD when the pitch is thin) before sending a résumé. Used when the InMail
  already has real JD substance, or when the recruiter is not asking for a
  résumé/call as the next step.
- **Screen-first**: recruiter wants a latest résumé and/or a call *before*
  they will share a JD (or even the end client). Send the generic latest
  résumé — never a JD-tailored package and never the indexed master file —
  offer to talk, and only name standing structural constraints (no C2C;
  remote-first from Lehi). JD evaluation (§11) waits until they actually
  send a description.

Never auto-sends.

Also: free heuristic extraction of Position + agency/company from common
InMail shapes (e.g. Madhvendra / KPG99) so those pitches become stub leads
without waiting on LLM fallback.
"""

from __future__ import annotations

import re
from datetime import datetime
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
# InMail notifications link through /comm/ with tracking params.
_COMM_THREAD_URL_RE = re.compile(
    r"https://www\.linkedin\.com/comm/messaging/thread/([^\s/?]+)", re.I
)
_SENDER_NAME_RE = re.compile(
    r"\n\s*([A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){0,3})\n\s*Reply\n",
)
# InMail layout: "Name, PhD" / headline / profile link.
_INMAIL_NAME_RE = re.compile(
    r"\n\s*([A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){0,3})(?:,\s*[A-Za-z.]{2,8})*[ \t]*\n"
    r"(?:[^\n]*\n)?\s*https://www\.linkedin\.com/(?:comm/)?in/",
)
_POSITION_RE = re.compile(
    r"(?im)^(?:Position|Role|Title)\s*[:\-]\s*(.+?)\s*$"
)
_COMPANY_SUFFIX = r"(?:Inc\.?|LLC|Corp\.?|Corporation|Incorporated|Ltd\.?|INC)"
_COMPANY_LINE_RE = re.compile(
    rf"(?m)^([A-Z0-9][\w.&,'/\- ]{{1,60}}{_COMPANY_SUFFIX})\s*$"
)
_RECRUITER_AT_RE = re.compile(
    r"(?im)^(?:Sr\.?\s+|Senior\s+)?(?:Technical\s+)?Recruiter\s+at\s+(.+?)\s*$"
)
_VIA_AGENCY_RE = re.compile(
    r"(?im)^(?:Best regards|Thanks|Thank you|Regards),?\s*\n"
    r"[A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+){0,3}\s*\n"
    r".*\n"
    rf"([A-Z0-9][\w.&'/\- ]{{1,60}}{_COMPANY_SUFFIX})\s*$",
)
# Subject-line title fallback: recruiters often put the title in the subject
# ("Sr. Analytics Engineer - Remote - FTE"). Only the part before the first
# " - " / " | " separator, and only when it names a role noun, so subjects
# like "Exploring new opportunities together" never become titles.
_SUBJECT_SPLIT_RE = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")
_SUBJECT_ROLE_NOUN_RE = re.compile(
    r"\b(?:Engineer|Developer|Architect|Scientist|Analyst|Programmer|Administrator|DBA|SRE)s?\b"
)
_SUBJECT_TRAILING_NOISE_RE = re.compile(
    r"(?i)\s+(?:job\s+)?(?:opportunity|opening|position|role)\s*$"
)
_LINKEDIN_FOOTER_MARKERS = ("This email was intended", "Learn why we included", "Unsubscribe:")

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

# Recruiter wants a résumé and/or a call before they will share a real JD.
_SCREEN_RESUME_ASK_RE = re.compile(
    r"(?i)\b("
    r"send (me )?(your |a |over a copy of your )?(resume|résumé|cv)|"
    r"copy of your (resume|résumé|cv)|"
    r"please (share|send|attach).{0,40}(resume|résumé|cv)|"
    r"looking forward to (receiving |seeing )?(your )?(resume|résumé|cv)"
    r")\b"
)
_SCREEN_CALL_BEFORE_JD_RE = re.compile(
    r"(?i)("
    r"availability to discuss|"
    r"some availability|"
    r"schedule a (quick )?call|"
    r"open to a (confidential )?chat|"
    r"when we speak|"
    r"once we (talk|speak|connect)|"
    r"after (we|our) (call|chat|conversation)|"
    r"share (who the client is|the (client|jd|job description)).{0,40}when|"
    r"i can share (who the client is|the client|more) when we"
    r")"
)


@dataclass
class PitchGaps:
    needs_jd: bool = False
    needs_end_client: bool = True
    needs_engagement: bool = True
    needs_remote: bool = True
    needs_rate_band: bool = True
    # Recruiter wants a résumé and/or call before they will share a real JD.
    screen_first: bool = False


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
    text = text.replace("\r\n", "\n")
    m = _THREAD_URL_RE.search(text)
    if m:
        return m.group(1).rstrip(").,]'\"")
    m = _COMM_THREAD_URL_RE.search(text)
    return f"https://www.linkedin.com/messaging/thread/{m.group(1)}/" if m else ""


def extract_linkedin_sender_name(text: str) -> str:
    if not text:
        return ""
    sig = parse_signature(text)
    if sig.name:
        return sig.name
    text = "\n" + text.replace("\r\n", "\n")
    m = _SENDER_NAME_RE.search(text) or _INMAIL_NAME_RE.search(text)
    return m.group(1).strip() if m else ""


def _strip_linkedin_footer(text: str) -> str:
    for marker in _LINKEDIN_FOOTER_MARKERS:
        idx = text.find(marker)
        if idx > 80:
            return text[:idx].strip()
    return text.strip()


def _title_from_subject(subject: str) -> str:
    cleaned = re.sub(r"(?i)^\s*(?:re|fwd?)\s*:\s*", "", subject or "").strip()
    head = _SUBJECT_SPLIT_RE.split(cleaned, maxsplit=1)[0].strip(" -–—|,")
    head = _SUBJECT_TRAILING_NOISE_RE.sub("", head).strip()
    if not _SUBJECT_ROLE_NOUN_RE.search(head) or len(head) > 80:
        return ""
    return head


def extract_role_heuristic(text: str, *, subject: str = "") -> ExtractedRole | None:
    """Free Position:/agency extraction for structured InMail pitches.

    Title comes from a `Position:`/`Role:`/`Title:` line, else from the
    subject line (see `_title_from_subject`). Returns None unless BOTH a
    title and a company/agency can be recovered — same bar as llm_new_lead
    so we don't create junk stubs.
    """
    if not text:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    title = ""
    m = _POSITION_RE.search(normalized)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip(" -–—|")
    if not title:
        title = _title_from_subject(subject)
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
    snippet = _strip_linkedin_footer(snippet)

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
    gaps.screen_first = bool(
        gaps.needs_jd
        and (
            _SCREEN_RESUME_ASK_RE.search(blob) or _SCREEN_CALL_BEFORE_JD_RE.search(blob)
        )
    )
    return gaps


_US_STATES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
    "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
    "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
    "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|"
    "Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming"
)
_PLACE_RE = re.compile(
    rf"\b(?:in|at|based in|located in|on-?site in|onsite in|hybrid in)\s+"
    rf"((?:[A-Z][a-z]+(?: [A-Z][a-z]+)*, [A-Z]{{2}}\b)|(?:{_US_STATES})\b)"
)

SIGNATURE = "Best, Shawn\nshawn.becker@spexture.com"


def extract_pitch_location(text: str) -> str:
    """A US place the pitch puts the client or role in ("New Jersey", "Bloomfield, CT")."""
    m = _PLACE_RE.search(text or "")
    return m.group(1) if m else ""


def _received_phrase(received_at: str) -> str:
    """ "on October 12" for the recruiter's message date (local time), or ""."""
    if not received_at:
        return ""
    try:
        when = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is not None:
        when = when.astimezone()
    return f" on {when.strftime('%B')} {when.day}"


def draft_qualifying_reply(
    text: str, *, subject: str = "", known_recruiter_name: str = "", received_at: str = ""
) -> QualifyingDraft:
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
    role = extract_role_heuristic(text, subject=subject)

    greeting = f"Hi {first}," if first else "Hi,"
    interest = "Thanks for reaching out" + _received_phrase(received_at)
    if role and role.title:
        interest += f" about the {role.title} role"
    interest += "."

    if gaps.screen_first:
        body = _draft_screen_first_body(
            greeting=greeting,
            interest=interest,
            gaps=gaps,
        )
    else:
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
            place = extract_pitch_location(text)
            asks.append(
                "Is the role fully remote (US), or is there an onsite/hybrid requirement"
                + (f" to be on-site with the client in {place}?" if place else "?")
            )
        if gaps.needs_rate_band:
            asks.append("What's the rate or salary band for the role?")

        if not asks:
            asks.append(
                "Happy to take a look — could you confirm end client, W2 vs 1099-direct (not C2C), remote status, and the rate/salary band?"
            )

        if len(asks) == 1:
            ask_block = asks[0]
        else:
            ask_block = "A few quick clarifiers before I send you my résumé:\n" + "\n".join(
                f"- {a}" for a in asks
            )
        body = f"{greeting}\n\n{interest} {ask_block}\n\n{SIGNATURE}"
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


def _draft_screen_first_body(*, greeting: str, interest: str, gaps: PitchGaps) -> str:
    """Résumé + call now; JD (and tailored package) only after they talk."""
    notes: list[str] = []
    if gaps.needs_engagement:
        notes.append(
            "I'm targeting W2 or 1099 direct-to-individual — not set up for C2C."
        )
    if gaps.needs_remote:
        notes.append(
            "I'm remote-first from Lehi, UT; hybrid in or near Salt Lake City is fine, onsite-only elsewhere isn't."
        )
    notes_block = ""
    if notes:
        notes_block = (
            "A couple of structural notes so we don't waste the call:\n"
            + "\n".join(f"- {n}" for n in notes)
            + "\n\n"
        )
    return (
        f"{greeting}\n\n"
        f"{interest} I'll send my latest résumé with this note — happy to jump on a "
        f"call once you've had a look. Send a couple of times that work for you.\n\n"
        f"{notes_block}"
        f"If it's a fit after we talk, a pasted job description is the most useful "
        f"next step on my side.\n\n"
        f"{SIGNATURE}"
    )


def promote_heuristic_unmatched(conn: sqlite3.Connection) -> list[str]:
    """Backfill: unmatched InMails with clear Position+company → stub lead + resolve.

    Called from pending-actions render so parked Madhvendra-class pitches
    don't wait for the next scan cycle. Returns promoted job_keys.
    """
    promoted: list[str] = []
    for row in store.list_unmatched_messages(conn):
        role = extract_role_heuristic(row["body_text"] or "", subject=row["subject"] or "")
        if role is None:
            continue
        promoted.append(promote_unmatched_row(conn, row, role))
    return promoted


def promote_unmatched_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    role: ExtractedRole,
    *,
    job_key: str | None = None,
) -> str:
    """Attach one parked unmatched message to a lead and resolve it.

    Uses `job_key` when given, else an existing lead matching the role's
    company + title, else a new stub lead scored by the free rule-based
    pass. Returns the job_key the message was resolved to.

    A heuristic role's snippet is already the JD slice of the body; an LLM
    role's excerpt is too short to score, so the whole InMail is used. The
    lead is dated by when the message was parked, not now, so the 21-day
    stale rule sees the real age of the pitch.
    """
    body = row["body_text"] or ""
    if job_key is None:
        existing = store.find_matching_job(conn, role.company, role.title)
        job_key = existing.normalized_key if existing else None
    if job_key is None:
        heuristic_slice = role.snippet if role.source == "linkedin_heuristic" else ""
        jd_text = (heuristic_slice or _strip_linkedin_footer(body)).strip()
        score = score_jd(jd_text) if jd_text else None
        seen = row["detected_at"] or store.utc_now_iso()
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
                f"Promoted from unmatched LinkedIn pitch ({role.source})"
            ],
            first_seen=seen,
            last_seen=seen,
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
    return job_key
