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
    # Broadened 2026-07-14 from a "consideration"-only match after a real
    # Epicor rejection used "After careful review" instead — same phrase
    # family, different noun.
    ("subject/body: after careful consideration/review/evaluation",
     re.compile(r"after careful (?:consideration|review|evaluation)", re.I)),
    ("subject/body: regret to inform", re.compile(r"regret to inform", re.I)),
    ("subject/body: thank you for applying", re.compile(r"thank you for (?:your )?(?:application|applying)", re.I)),
    # Added 2026-07-14 from 8 real rejection samples gathered from Mail.app
    # archives: "move forward with other candidate(s)" (a very different verb
    # from the existing "pursue other candidates") was the single most common
    # phrase, and its absence was the sole miss for 3 of the 8 — NICE, Epicor,
    # and a Workday-routed "Data Engineer III" rejection all used this exact
    # construction with no other pattern above matching either the subject or
    # body. See job-tracker git history for the full sample set this was
    # tuned against.
    ("subject/body: move forward with other candidate",
     re.compile(r"move forward with (?:an)?other candidate", re.I)),
    # "you have not been selected" (BNSF) — distinct, unambiguous decline
    # phrasing with no legitimate positive-outcome use.
    ("subject/body: not been selected", re.compile(r"not been selected", re.I)),
    # "decided not to move forward with your application" (Adobe) —
    # generalizes the negated form so it doesn't depend on an "unfortunately"
    # prefix also being present, unlike in the one sample seen so far.
    ("subject/body: not to move forward with you",
     re.compile(r"not (?:to )?move forward with (?:your|you)\b", re.I)),
    # A follow-up check against 3 more real samples (Angel Studios, Zapier,
    # Lightspeed DMS — the oldest in the Mail.app archive review, back to
    # 10/2025) found 0 gaps: all 3 already matched "unfortunately," above.
    # See tests/test_classifier.py's test_real_rejection_samples_from_
    # earlier_2026_archive — no new pattern was needed here.
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

# Broader than _JOB_TITLE above (which requires a specific tech-keyword
# prefix like "software"/"cloud"/"data") — used only to spot a single,
# unambiguous "<Title> at <Company>" pattern in the *subject line* of a
# digest-domain sender's mail (see single_job_signal in classify() below), a
# shape job-alert platforms use for one-off single-role alerts just as often
# as for genuine multi-job digests. Real digests almost always carry their
# own tell (a `_DIGEST_SUBJECT` phrase like "15 new jobs", or `_MULTI_JD_HINTS`
# phrasing), which are checked independently and win regardless of this.
_SUBJECT_SINGLE_ROLE_AT_COMPANY = re.compile(
    r"\b(?:engineer|developer|architect|manager|analyst|designer|scientist|"
    r"specialist|consultant|director|coordinator)\b[^|,;]{0,40}?\s+at\s+[A-Z]",
    re.I,
)

_MULTI_JD_HINTS = re.compile(
    r"\b(?:open roles?|multiple positions?|several (?:open )?roles?|"
    r"jobs? (?:at|from) \w+|we(?:'re| are) hiring for|job matches:|"
    r"jobs? (?:posted|alerts?) from)\b",
    re.I,
)

# schema.org JSON-LD marketing boilerplate (promo cards, discount offers) that
# leaks into body_plain for some senders — a strong noise signal on its own,
# since real job listings don't ship as raw JSON-LD markup.
_SCHEMA_ORG_MARKETING = re.compile(
    r'"@type"\s*:\s*"(?:EmailMessage|Organization|DiscountOffer|PromotionCard)"', re.I
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


# The two LinkedIn addresses that carry an actual InMail/reply's real text
# (see scan_communications.py's module docstring) — as opposed to
# `jobalerts-noreply@`/`messaging-digest-noreply@`, which only ever carry
# bulk "N new jobs" digests. `_DIGEST_SENDERS` matches `linkedin\.com`
# generically (right, for `classify()`'s own purposes — LINK_ONLY_DIGEST vs.
# SINGLE_JD/MULTI_JD_IN_BODY doesn't need this distinction, since a personal
# InMail never reaches `classify()` at all in practice; scan_communications.py
# intercepts hit-reply@/inmail-hit-reply@ mail upstream of it). This
# function's job is different — telling a personal pitch apart from a bulk
# digest — so it needs the narrower carve-out below to avoid misreading a
# real personal InMail as a digest purely because of the shared domain.
_LINKEDIN_PERSONAL_REPLY_SENDERS = re.compile(r"(?:^|[@.])(?:inmail-)?hit-reply@linkedin\.com", re.I)


def is_personal_recruiter_message(text: str, from_address: str = "") -> bool:
    """True when `text` reads like a human recruiter's personalized pitch or
    reply (came across your profile / love to connect / quick call / talent
    acquisition, etc.) rather than a bulk job-alert digest that merely lists
    a role — the same signal `classify()`'s step 4 (Label.RECRUITER_OUTREACH)
    already uses, exposed standalone (2026-07-21) so callers that don't have
    a full `EmailMessage` to classify — `pipeline/store.py`'s backfill script
    scanning stored `jd_text`, `scan_communications.py`'s follow-up-excerpt
    path — can reuse the exact same rule instead of re-deriving it.

    Deliberately conservative: a digest-domain sender (LinkedIn Job Alerts,
    etc. — but NOT the two hit-reply@ personal-reply addresses, see
    `_LINKEDIN_PERSONAL_REPLY_SENDERS` above) or multi-role phrasing ("open
    roles", "N new jobs") always loses, even if the body happens to also
    contain outreach-flavored phrasing — real bulk digests occasionally do
    (e.g. a "reach out if interested" footer), and this must not flag those
    as personal outreach.
    """
    is_personal_reply_sender = bool(_LINKEDIN_PERSONAL_REPLY_SENDERS.search(from_address))
    if not is_personal_reply_sender and (_DIGEST_SENDERS.search(from_address) or _DIGEST_SUBJECT.search(text)):
        return False
    if _MULTI_JD_HINTS.search(text):
        return False
    recruiter_from = is_personal_reply_sender or bool(_RECRUITER_FROM.search(from_address))
    recruiter_body = bool(_RECRUITER_BODY.search(text))
    if not (recruiter_from or recruiter_body):
        return False
    ats_count = _count_ats_urls(text)
    title_count = _count_job_titles(text)
    url_count = _count_urls(text)
    thin_jd = ats_count == 0 and title_count <= 1 and url_count <= 2
    return thin_jd


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
    # Broader than `_JOB_TITLE`/title_count (which needs a specific
    # tech-keyword prefix like "software"/"cloud"/"data") — catches the
    # "<Title> at <Company>" shape job-alert subjects use for single-role
    # mail regardless of field (e.g. "Web Developer II ... at Woodbury
    # School of Business"). Feeds both the digest-sender carve-out below
    # and step 6 (Single JD), so a message that escapes LINK_ONLY_DIGEST
    # via this signal can actually reach SINGLE_JD instead of falling
    # through to the NOISE catch-all for lack of a title_count hit.
    subject_single_role = bool(_SUBJECT_SINGLE_ROLE_AT_COMPANY.search(subject))

    # 1.5. schema.org JSON-LD marketing boilerplate — real job listings never
    # ship as raw JSON-LD; this is a strong noise signal even if the mail
    # incidentally mentions job-title-like words elsewhere.
    if _SCHEMA_ORG_MARKETING.search(text) and ats_count == 0:
        reasons.append("body: schema.org marketing JSON-LD, no ATS links")
        return ClassificationResult(Label.NOISE, 0.8, reasons)

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
    # A digest-domain sender (LinkedIn Job Alerts, etc.) doesn't always mean a
    # multi-job digest — plenty of that mail is a single "<Title> at <Company>"
    # alert with exactly one identifiable role and no multi-job phrasing.
    # Added 2026-07-06 after a real job-tracker triage run showed ~90% of
    # Category/recruiter_job mail short-circuiting to LINK_ONLY_DIGEST purely
    # on sender domain — before extraction was ever attempted — even when the
    # subject cleanly named one company/title that pipeline/extract.py's
    # subject-parser could resolve fine on its own.
    #
    # BROADENED again 2026-07-06 (2nd pass): `link_heavy` alone was *still*
    # overriding `single_job_signal`, since a real single-job LinkedIn Job
    # Alert email ships ~10 tracking/footer/unsubscribe links even when the
    # body contains exactly one job card (e.g. "Senior Software Engineer at
    # Podium" — one listing, 10 URLs, 253 words — tripped `link_heavy` and
    # got misfiled as a digest despite a clean, unambiguous single-role
    # subject). `digest_subject` genuinely does still win outright — its
    # phrasing ("15 new jobs", "jobs for you") is a real multi-job signal
    # independent of any subject-single-role false-positive risk — but
    # `link_heavy` is just a raw link-count proxy with no such guarantee, so
    # it must respect `single_job_signal` too. Multi-job digests remain
    # correctly caught: a real digest either has >1 unique job title,
    # multi-role phrasing, or (for aggregators like Energy Job Line/
    # TheLadders) no single "<title> at <company>" subject shape at all —
    # so `single_job_signal` stays False for them regardless of this change.
    single_job_signal = (len(unique_titles) == 1 or subject_single_role) and not _MULTI_JD_HINTS.search(text)
    if (
        (digest_subject and url_count >= 2)
        or (link_heavy and url_count >= 3 and ats_count == 0 and not single_job_signal)
        or (digest_sender and not single_job_signal)
    ):
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
    if ats_count == 1 or ((title_count >= 1 or subject_single_role) and has_jobs):
        if ats_count == 1:
            reasons.append("body: single ATS link")
        if title_count >= 1:
            reasons.append("body: job title present")
        elif subject_single_role:
            reasons.append("subject: single role at company")
        if has_jobs:
            reasons.append("body: job keywords present")
        conf = 0.9 if ats_count == 1 else 0.8
        return ClassificationResult(Label.SINGLE_JD, conf, reasons)

    # 7. Fallback
    reasons.append("no stronger rule matched")
    return ClassificationResult(Label.NOISE, 0.5, reasons)
