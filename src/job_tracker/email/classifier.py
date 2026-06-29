"""Heuristic email classifier for the recruiting inbox."""

from __future__ import annotations

import re
from re import Pattern

from job_tracker.email.labels import Label
from job_tracker.email.models import ClassificationResult, EmailMessage

# ATS apply URLs — same providers as jd_resolver.
_ATS_URL = re.compile(
    r"https?://(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|"
    r"jobs\.lever\.co|jobs\.ashbyhq\.com|(?:www\.)?smartrecruiters\.com)[^\s<>\"']+",
    re.I,
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)

_JOB_TITLE = re.compile(
    r"\b(?:(?:senior|staff|principal|lead|sr\.?|jr\.?)\s+)?"
    r"(?:software|full[\s-]?stack|backend|front[\s-]?end|data|platform|"
    r"devops|infrastructure|machine learning|ml|ai|cloud|mobile|ios|android|"
    r"security|site reliability|sre|qa|test)\s+"
    r"(?:engineer|developer|architect|manager)\b",
    re.I,
)

_JOB_KEYWORDS = re.compile(
    r"\b(?:engineer|developer|software|role|position|hiring|job|opportunity|"
    r"apply|career|opening|requisition|candidate|interview)\b",
    re.I,
)

_REJECTION_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("subject/body: not moving forward", re.compile(r"not moving forward", re.I)),
    ("subject/body: pursue other candidates", re.compile(r"pursue other candidates", re.I)),
    ("subject/body: position filled", re.compile(r"position (?:has been )?filled", re.I)),
    ("subject/body: unfortunately", re.compile(r"unfortunately(?:,|\s+we)", re.I)),
    ("subject/body: will not proceed", re.compile(r"will not (?:be )?proceed", re.I)),
    ("subject/body: after careful consideration", re.compile(r"after careful consideration", re.I)),
    ("subject/body: regret to inform", re.compile(r"regret to inform", re.I)),
    ("subject/body: thank you for applying", re.compile(r"thank you for (?:your )?(?:application|applying)", re.I)),
]

_DIGEST_SENDERS = re.compile(
    r"(?:linkedin\.com|indeed\.com|glassdoor\.com|ziprecruiter\.com|"
    r"hired\.com|monster\.com|dice\.com|jobright\.ai|welcometothejungle\.com)",
    re.I,
)
_DIGEST_SUBJECT = re.compile(
    r"\b(?:(?:new|recommended|top|\d+)\s+jobs?|jobs?\s+for you|job alert|"
    r"matching jobs|your job matches)\b",
    re.I,
)

_RECRUITER_FROM = re.compile(
    r"(?:recruit|talent|staffing|hiring|careers?@|hr@|people@|"
    r"@[\w.-]*(?:staffing|recruiting|talent))",
    re.I,
)
_RECRUITER_BODY = re.compile(
    r"(?:came across your profile|love to connect|quick (?:chat|call)|"
    r"schedule (?:a )?(?:call|chat|time)|speak with you about|"
    r"open to a conversation|reaching out about|talent acquisition|"
    r"discuss an opportunity)",
    re.I,
)

_NOISE_SENDERS = re.compile(
    r"(?:newsletter|marketing|promo|noreply@(?:mail\.)?(?:hubspot|mailchimp|sendgrid)|"
    r"notifications@github|@substack\.com)",
    re.I,
)
_UNSUBSCRIBE = re.compile(r"\bunsubscribe\b", re.I)

_MULTI_JD_HINTS = re.compile(
    r"\b(?:open roles?|multiple positions?|several (?:open )?roles?|"
    r"jobs? (?:at|from) \w+|we(?:'re| are) hiring for)\b",
    re.I,
)


def _find_patterns(text: str, patterns: list[tuple[str, Pattern[str]]]) -> list[str]:
    return [name for name, pat in patterns if pat.search(text)]


def _count_urls(text: str) -> int:
    return len(_URL.findall(text))


def _count_ats_urls(text: str) -> int:
    return len(_ATS_URL.findall(text))


def _count_job_titles(text: str) -> int:
    return len(_JOB_TITLE.findall(text))


def _unique_job_titles(text: str) -> set[str]:
    return {m.group(0).lower() for m in _JOB_TITLE.finditer(text)}


def _has_job_keywords(text: str) -> bool:
    return _JOB_KEYWORDS.search(text) is not None


def _sender_domain(from_address: str) -> str:
    match = re.search(r"@[\w.-]+", from_address)
    return match.group(0).lower() if match else from_address.lower()


def classify(message: EmailMessage) -> ClassificationResult:
    """
    Assign one label using ordered heuristics (first strong match wins).

    extracted_roles is intentionally empty in this commit; fan-out comes in 2c.
    """
    text = message.combined_text
    subject = message.subject
    from_addr = message.from_address
    reasons: list[str] = []

    # 1. Rejection
    rejection_hits = _find_patterns(text, _REJECTION_PATTERNS)
    if rejection_hits:
        reasons.extend(rejection_hits)
        return ClassificationResult(Label.REJECTION, 0.9, reasons)

    url_count = _count_urls(text)
    ats_count = _count_ats_urls(text)
    title_count = _count_job_titles(text)
    unique_titles = _unique_job_titles(text)
    has_jobs = _has_job_keywords(text)

    # 2. Noise — no job signal, or obvious non-job mail
    if _NOISE_SENDERS.search(from_addr) and not has_jobs:
        reasons.append("sender: marketing/newsletter pattern")
        return ClassificationResult(Label.NOISE, 0.85, reasons)
    if _UNSUBSCRIBE.search(text) and not has_jobs and ats_count == 0:
        reasons.append("body: unsubscribe without job keywords")
        return ClassificationResult(Label.NOISE, 0.8, reasons)
    if not has_jobs and ats_count == 0 and title_count == 0:
        reasons.append("no job keywords, titles, or ATS links")
        return ClassificationResult(Label.NOISE, 0.75, reasons)

    # 3. Link-only digest
    digest_sender = bool(_DIGEST_SENDERS.search(from_addr) or _DIGEST_SENDERS.search(_sender_domain(from_addr)))
    digest_subject = bool(_DIGEST_SUBJECT.search(subject))
    link_heavy = url_count >= 3 and len(text.split()) < url_count * 40
    if digest_sender or (digest_subject and url_count >= 2) or (link_heavy and url_count >= 3 and ats_count == 0):
        if digest_sender:
            reasons.append("sender: job-alert domain")
        if digest_subject:
            reasons.append("subject: job-alert phrasing")
        if link_heavy:
            reasons.append(f"body: link-heavy ({url_count} URLs)")
        conf = 0.9 if digest_sender else 0.8
        return ClassificationResult(Label.LINK_ONLY_DIGEST, conf, reasons)

    # 4. Recruiter outreach — recruiter tone, no ATS/JD substance
    recruiter_from = bool(_RECRUITER_FROM.search(from_addr))
    recruiter_body = bool(_RECRUITER_BODY.search(text))
    thin_jd = ats_count == 0 and title_count <= 1 and url_count <= 2
    if (recruiter_from or recruiter_body) and thin_jd and not _MULTI_JD_HINTS.search(text):
        if recruiter_from:
            reasons.append("sender: recruiter/talent pattern")
        if recruiter_body:
            reasons.append("body: outreach phrasing")
        conf = 0.85 if recruiter_from and recruiter_body else 0.75
        return ClassificationResult(Label.RECRUITER_OUTREACH, conf, reasons)

    # 5. Multi-JD in body
    if ats_count >= 2 or len(unique_titles) >= 2 or _MULTI_JD_HINTS.search(text):
        if ats_count >= 2:
            reasons.append(f"body: {ats_count} ATS links")
        if len(unique_titles) >= 2:
            reasons.append(f"body: {len(unique_titles)} distinct job titles")
        if _MULTI_JD_HINTS.search(text):
            reasons.append("body: multi-role phrasing")
        return ClassificationResult(Label.MULTI_JD_IN_BODY, 0.85, reasons)

    # 6. Single JD
    if ats_count == 1 or (title_count >= 1 and has_jobs):
        if ats_count == 1:
            reasons.append("body: single ATS link")
        if title_count >= 1:
            reasons.append("body: job title present")
        if has_jobs:
            reasons.append("body: job keywords present")
        conf = 0.9 if ats_count == 1 else 0.8
        return ClassificationResult(Label.SINGLE_JD, conf, reasons)

    # 7. Fallback
    reasons.append("no stronger rule matched")
    return ClassificationResult(Label.NOISE, 0.5, reasons)
