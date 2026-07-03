"""Fan-out: turn a SINGLE_JD / MULTI_JD_IN_BODY message into (company, title) roles.

This is deliberately a heuristic, regex-based first pass (per the runbook's
"rule engine first, LLM only if ambiguous" design). It is good enough to
extract company + title from ATS notification mail and structured multi-role
digests; anything it can't confidently parse comes back with low confidence
so the pipeline can flag it for manual review instead of silently dropping it.
"""

from __future__ import annotations

import re

from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage, ExtractedRole

_ATS_URL = re.compile(
    r"https?://(?:"
    r"(?:boards|job-boards)\.greenhouse\.io/(?P<gh_token>[\w-]+)"
    r"|jobs\.lever\.co/(?P<lever_token>[\w-]+)"
    r"|jobs\.ashbyhq\.com/(?P<ashby_token>[\w-]+)"
    r"|(?:www\.)?smartrecruiters\.com/(?P<sr_token>[\w-]+)"
    r")[^\s<>\"']*",
    re.I,
)

_ATS_VENDOR_DOMAINS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
}

# Job boards / aggregators / ATS-notification platforms are never the actual
# employer — never attribute a role to them via the sender-domain fallback.
# (Real examples that produced false "companies" before this fix: Adzuna,
# Ladders, CareerBuilder, talent.com.)
_JOB_BOARD_DOMAINS = {
    "adzuna.com",
    "adzunajobs.com",
    "theladders.com",
    "careerbuilder.com",
    "talent.com",
    "jobs2web.com",
    "indeed.com",
    "linkedin.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "monster.com",
    "dice.com",
    "simplyhired.com",
    "lensa.com",
    "jobright.ai",
    "hired.com",
    "welcometothejungle.com",
}

# Same idea as _JOB_BOARD_DOMAINS, but matched against a *resolved company
# name string* rather than a sender domain — e.g. text like "our secure
# server at Ladders, Inc." lets the job board's own name leak in as a
# "company" via the generic text patterns, even though the mail is pure
# marketing/re-engagement copy with no real listing.
_JOB_BOARD_NAMES = re.compile(
    r"^(?:ladders|adzuna|indeed|linkedin|careerbuilder|talent\.com|monster|"
    r"dice|simplyhired|lensa|ziprecruiter|glassdoor|ladders,?\s*inc\.?)\b",
    re.I,
)


def _is_job_board_name(candidate: str) -> bool:
    return bool(_JOB_BOARD_NAMES.match(candidate.strip()))

_JOB_TITLE = re.compile(
    r"\b(?:(?:senior|staff|principal|lead|sr\.?|jr\.?)\s+)?"
    r"(?:software|full[\s-]?stack|backend|front[\s-]?end|data|platform|"
    r"devops|infrastructure|machine learning|ml|ai|cloud|mobile|ios|android|"
    r"security|site reliability|sre|qa|test)\s+"
    r"(?:engineer|developer|architect|manager)\b",
    re.I,
)

_ROLE_KEYWORD = re.compile(
    r"\b(?:engineer|developer|architect|manager|analyst|designer|scientist|lead)\b",
    re.I,
)

_BULLET_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")

_COMPANY_IS_HIRING = re.compile(r"\b([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\s+is hiring\b")
_COMPANY_TRAILING_DASH = re.compile(r"[—-]\s*([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\s*$")
_COMPANY_AT = re.compile(r"\bat\s+([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\b")
_COMPANY_OPEN_ROLES_AT = re.compile(
    r"\bopen (?:roles?|positions?)\s+at\s+([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\b",
    re.I,
)
_COMPANY_HIRING_FOR = re.compile(
    r"\bhiring for\s+([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\b", re.I
)
_COMPANY_FROM = re.compile(
    r"\bjobs?\s+(?:posted|alerts?)\s+from\s+([A-Z][\w&.,'-]*(?:\s+[A-Z][\w&.,'-]*){0,3})\b", re.I
)

# Corporate ATS "search agent" digests (e.g. jobs2web-style): a single sender
# company lists several of its own real openings after a "Job Matches:" marker.
# The search-agent NAME itself (e.g. "Agent: Sr Software Engineer") is a saved
# search, not a real posting — never extract a role from it.
_JOB_MATCHES_SECTION = re.compile(
    r"Job Matches:\s*(.*?)(?=Remember to forward|Getting these notifications|"
    r"Add another agent|Connect with us|Manage your Job Preferences|$)",
    re.I | re.S,
)
_TITLE_LOCATION_PAIR = re.compile(
    r"(?P<title>[A-Z][\w&,.'/ ]{2,60}?)\s+-\s+"
    r"(?P<location>(?:[A-Z][a-zA-Z.]+,\s*)?[A-Z]{2}(?:,\s*(?:US|USA))?(?:,\s*\d{5})?)"
    r"(?=\s+[A-Z]|\s*$)"
)

# Job-board digests that list several postings, each terminated by a "more
# details" call-to-action (seen from Adzuna and similar aggregators). Once
# HTML block tags are converted to real line breaks (see htmltext.py), each
# listing reliably takes the shape:
#     <Title>
#     [flag lines: TOP MATCH / NEW / REMOTE]
#     <Company> - <Location>
#     [trailing flag lines, e.g. "This job is available in multiple locations"]
#     more details »
_MORE_DETAILS_SPLIT = re.compile(r"more details(?:\s*(?:&raquo;|»|>>))?", re.I)
_COMPANY_LOCATION_LINE = re.compile(r"^(?P<company>.+?)\s+-\s+(?P<location>[A-Z].+)$")
_DIGEST_FLAG_LINES = {"top match", "new", "remote", "this job is available in multiple locations"}

# "Matching jobs from the web" style aggregation (e.g. Energy Job Line,
# LinkedIn-style curation): each listing ends with a unique "Ref no.: <hex>"
# id — a reliable per-listing anchor. With real line breaks preserved, the
# listing's title is reliably the first line right after the previous
# listing's Ref no.; company vs. location on the following line is still
# ambiguous (could be either — the aggregation mixes formats per source), so
# that part is left for manual review rather than guessed.
_REF_NO_SPLIT = re.compile(r"Ref no\.?:\s*[0-9A-Fa-f]{16,40}")


def _token_to_company(token: str) -> str:
    words = re.split(r"[-_]+", token)
    return " ".join(w.capitalize() for w in words if w)


def _find_ats_matches(text: str) -> list[tuple[str, str, str]]:
    """Return (provider, token, matched_url) triples in order of appearance."""
    out: list[tuple[str, str, str]] = []
    for m in _ATS_URL.finditer(text):
        for provider, group in (
            ("greenhouse", "gh_token"),
            ("lever", "lever_token"),
            ("ashby", "ashby_token"),
            ("smartrecruiters", "sr_token"),
        ):
            token = m.groupdict().get(group)
            if token:
                out.append((provider, token, m.group(0)))
                break
    return out


def _company_from_text(text: str) -> str | None:
    # Search line-by-line (not the whole blob) so a capitalized word on the
    # *next* line (e.g. a "Hi Shawn," greeting) can never bleed into the
    # captured company name via the regex's cross-word \s+ continuation.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for pattern in (
            _COMPANY_OPEN_ROLES_AT,
            _COMPANY_HIRING_FOR,
            _COMPANY_FROM,
            _COMPANY_IS_HIRING,
            _COMPANY_TRAILING_DASH,
            _COMPANY_AT,
        ):
            m = pattern.search(line)
            if m:
                candidate = m.group(1).strip()
                # A mid-name "." (e.g. "Corp.") is legitimate, but ". <Capital>"
                # is a sentence boundary the capture group's multi-word
                # continuation can otherwise swallow (e.g. "at DTN. We are
                # hiring" -> "DTN. We"). Truncate there.
                candidate = re.split(r"\.\s+(?=[A-Z])", candidate)[0].rstrip(".,")
                if candidate and len(candidate) <= 50 and not _is_job_board_name(candidate):
                    return candidate
    return None


def _extract_job_matches_roles(text: str) -> list[ExtractedRole]:
    """Parse a corporate ATS 'search agent' digest's real Job Matches: list.

    The search-agent name itself (e.g. "Agent: Sr Software Engineer") is a
    saved search, not a posting, and is deliberately never used as a title.
    """
    section = _JOB_MATCHES_SECTION.search(text)
    if not section:
        return []
    company = _company_from_text(text)
    roles = []
    for m in _TITLE_LOCATION_PAIR.finditer(section.group(1)):
        title = m.group("title").strip()
        if not title:
            continue
        roles.append(
            ExtractedRole(
                company=company or "",
                title=title,
                source="job_matches_digest",
                confidence=0.6 if company else 0.35,
            )
        )
    return roles


def _extract_ref_no_digest_roles(text: str) -> list[ExtractedRole]:
    """Parse 'matching jobs from the web' style digests (Energy Job Line and
    similar curated-aggregation senders). Each entry between consecutive
    'Ref no.: <hex>' markers reliably starts with the next listing's real
    title on its own line. The line after that is either a company or a
    location depending on the source the aggregator pulled from — that
    ambiguity is left blank for manual review rather than guessed.
    """
    chunks = _REF_NO_SPLIT.split(text)
    if len(chunks) < 3:
        return []
    roles = []
    for chunk in chunks[1:-1]:  # first chunk is header noise, last is footer
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        title = lines[0]
        if len(title) < 8:
            continue
        roles.append(
            ExtractedRole(
                company="",
                title=title,
                source="ref_no_digest",
                confidence=0.35,
            )
        )
    return roles


def _extract_more_details_digest_roles(text: str) -> list[ExtractedRole]:
    """Parse 'Title / [flags] / Company - Location / more details' digest
    listings (Adzuna and similar), now that each field reliably sits on its
    own line (see htmltext.py's structure-preserving HTML conversion).
    """
    chunks = _MORE_DETAILS_SPLIT.split(text)
    roles = []
    for chunk in chunks[:-1]:  # last chunk is always trailing footer/noise
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        # The company/location line isn't always the chunk's last line — a
        # trailing flag like "This job is available in multiple locations"
        # can follow it — so search backward for the last line that actually
        # matches "<Company> - <Location>". Gmail's own `snippet` preview
        # (prepended in combined_text) flattens the first listing onto one
        # un-broken line with no real newlines, which can spuriously match
        # this pattern with a 100+ character "company" — a length cap
        # rejects that without needing to special-case the snippet field.
        company = None
        company_idx = None
        for i in range(len(lines) - 1, -1, -1):
            m = _COMPANY_LOCATION_LINE.match(lines[i])
            if m and len(m.group("company").strip()) <= 60:
                company = m.group("company").strip()
                company_idx = i
                break
        if company is None or _is_job_board_name(company):
            continue

        title = None
        for line in reversed(lines[:company_idx]):
            if line.lower() in _DIGEST_FLAG_LINES:
                continue
            title = line
            break
        if not title:
            continue

        roles.append(
            ExtractedRole(
                company=company,
                title=title,
                source="digest_listing",
                confidence=0.55,
            )
        )
    return roles


def _company_from_sender(from_address: str) -> str | None:
    m = re.search(r"@([\w.-]+)", from_address or "")
    if not m:
        return None
    domain = m.group(1).lower()
    for vendor in _ATS_VENDOR_DOMAINS | _JOB_BOARD_DOMAINS:
        if domain == vendor or domain.endswith("." + vendor):
            return None  # ATS/job-board notification domain, not the employer
    parts = domain.split(".")
    if len(parts) < 2:
        return None
    label = parts[-2]
    if label in {"gmail", "yahoo", "outlook", "hotmail", "icloud"}:
        return None
    return label.replace("-", " ").title()


def _first_title(text: str) -> str | None:
    m = _JOB_TITLE.search(text)
    return m.group(0).strip() if m else None


def _clean_bullet_title(line: str) -> str | None:
    if not _ROLE_KEYWORD.search(line):
        return None
    # Drop a trailing " — description" / " - description" qualifier.
    head = re.split(r"\s+[—–]\s+|\s+-\s+", line, maxsplit=1)[0].strip()
    return head or line.strip()


def _extract_single_jd(message: EmailMessage) -> list[ExtractedRole]:
    text = message.combined_text
    ats_matches = _find_ats_matches(text)

    company = _company_from_text(f"{message.subject}\n{text}")
    apply_url = ""
    source = "subject" if company else ""

    if ats_matches:
        provider, token, url = ats_matches[0]
        apply_url = url
        if not company:
            company = _token_to_company(token)
            source = "ats_url"
    if not company:
        company = _company_from_sender(message.from_address)
        source = "sender_domain" if company else source

    title = _first_title(message.subject) or _first_title(text)

    if not company or not title:
        return [
            ExtractedRole(
                company=company or "",
                title=title or "",
                apply_url=apply_url,
                source=source or "unresolved",
                confidence=0.3,
            )
        ]

    confidence = 0.9 if (ats_matches and source == "ats_url") or source == "subject" else 0.6
    return [
        ExtractedRole(
            company=company,
            title=title,
            apply_url=apply_url,
            source=source or "sender_domain",
            confidence=confidence,
        )
    ]


def _extract_multi_jd(message: EmailMessage) -> list[ExtractedRole]:
    text = message.combined_text
    shared_company = _company_from_text(f"{message.subject}\n{text}") or _company_from_sender(
        message.from_address
    )
    ats_matches = _find_ats_matches(text)
    ats_urls_by_index = [url for _, _, url in ats_matches]

    roles: list[ExtractedRole] = []
    for line in text.splitlines():
        bullet = _BULLET_LINE.match(line)
        candidate_line = bullet.group(1) if bullet else None
        if not candidate_line:
            continue
        title = _clean_bullet_title(candidate_line)
        if not title:
            continue
        apply_url = ats_urls_by_index[len(roles)] if len(roles) < len(ats_urls_by_index) else ""
        company = shared_company or (
            _token_to_company(_find_ats_matches(apply_url)[0][1]) if apply_url else ""
        )
        roles.append(
            ExtractedRole(
                company=company or "",
                title=title,
                apply_url=apply_url,
                source="bullet_line",
                confidence=0.75 if company else 0.4,
            )
        )

    if not roles:
        roles = _extract_job_matches_roles(text)

    if not roles:
        roles = _extract_more_details_digest_roles(text)

    if not roles:
        roles = _extract_ref_no_digest_roles(text)

    if not roles:
        # No parseable bullet lines — fall back to one row per ATS link found.
        for provider, token, url in ats_matches:
            roles.append(
                ExtractedRole(
                    company=shared_company or _token_to_company(token),
                    title=_first_title(text) or "",
                    apply_url=url,
                    source="ats_url",
                    confidence=0.5,
                )
            )

    return roles


def extract_roles(message: EmailMessage, label: Label) -> list[ExtractedRole]:
    """Fan a classified message out into (company, title) candidates.

    Only meaningful for SINGLE_JD and MULTI_JD_IN_BODY; other labels have no
    role to extract and return an empty list.
    """
    if label == Label.SINGLE_JD:
        return _extract_single_jd(message)
    if label == Label.MULTI_JD_IN_BODY:
        return _extract_multi_jd(message)
    return []
