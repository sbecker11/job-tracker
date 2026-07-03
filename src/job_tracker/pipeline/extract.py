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
            _COMPANY_IS_HIRING,
            _COMPANY_TRAILING_DASH,
            _COMPANY_AT,
        ):
            m = pattern.search(line)
            if m:
                candidate = m.group(1).strip().rstrip(".,")
                if candidate and len(candidate) <= 50:
                    return candidate
    return None


def _company_from_sender(from_address: str) -> str | None:
    m = re.search(r"@([\w.-]+)", from_address or "")
    if not m:
        return None
    domain = m.group(1).lower()
    for vendor in _ATS_VENDOR_DOMAINS:
        if domain == vendor or domain.endswith("." + vendor):
            return None  # ATS notification domain, not the employer
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
