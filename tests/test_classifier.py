"""Offline tests for the heuristic email classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_tracker.email.classifier import classify
from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

FIXTURE_EXPECTATIONS = {
    "stripe_single_jd.json": Label.SINGLE_JD,
    "linkedin_digest.json": Label.LINK_ONLY_DIGEST,
    "indeed_digest.json": Label.LINK_ONLY_DIGEST,
    "rejection.json": Label.REJECTION,
    "recruiter_outreach.json": Label.RECRUITER_OUTREACH,
    "multi_jd_in_body.json": Label.MULTI_JD_IN_BODY,
    "newsletter_noise.json": Label.NOISE,
    "ats_search_agent_digest.json": Label.MULTI_JD_IN_BODY,
    "job_board_flattened_digest.json": Label.MULTI_JD_IN_BODY,
}


def load_fixture(name: str) -> EmailMessage:
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return EmailMessage(**data)


@pytest.mark.parametrize("fixture_name,expected", FIXTURE_EXPECTATIONS.items())
def test_fixture_labels(fixture_name: str, expected: Label) -> None:
    message = load_fixture(fixture_name)
    result = classify(message)
    assert result.label == expected, (
        f"{fixture_name}: expected {expected.value}, got {result.label.value}; "
        f"reasons={result.reasons}"
    )


def test_rejection_beats_job_keywords() -> None:
    """Rejection phrasing wins even when the role title appears in the subject."""
    result = classify(load_fixture("rejection.json"))
    assert result.label == Label.REJECTION
    assert result.confidence >= 0.8


def test_single_jd_includes_reasons() -> None:
    result = classify(load_fixture("stripe_single_jd.json"))
    assert result.label == Label.SINGLE_JD
    assert any("ATS" in r for r in result.reasons)


def test_extracted_roles_empty_stub() -> None:
    result = classify(load_fixture("stripe_single_jd.json"))
    assert result.extracted_roles == []


def test_single_job_linkedin_alert_is_not_swept_into_link_only_digest() -> None:
    """Regression for 2026-07-06: a real job-tracker triage run showed ~90%
    of LinkedIn job-alert mail short-circuiting to LINK_ONLY_DIGEST purely
    on sender domain, even when the subject cleanly names one company/title
    pipeline/extract.py's subject-parser can resolve on its own. This kind
    of mail — one unambiguous role, no multi-job phrasing/link-pile — must
    reach SINGLE_JD so it actually gets scored instead of parked in
    NEEDS_REVIEW forever."""
    message = EmailMessage(
        id="fixture-linkedin-single-job-alert",
        from_address="jobalerts-noreply@linkedin.com",
        subject="Senior Software Engineer at Ancestry",
        snippet="Based on your profile",
        body_plain="Senior Software Engineer at Ancestry. Apply on LinkedIn.",
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.SINGLE_JD, f"got {result.label.value}; reasons={result.reasons}"


def test_single_job_alert_with_non_tech_title_still_reaches_single_jd() -> None:
    """The narrow _JOB_TITLE regex (tech-keyword prefix like
    software/cloud/data + engineer/developer/...) misses plenty of real
    single-role alert subjects, e.g. "Web Developer II ... at Company" —
    "web" isn't in that prefix list. The broader subject-only
    "<role> at <Company>" signal must still be enough to both escape
    LINK_ONLY_DIGEST *and* land on SINGLE_JD (not fall through to the NOISE
    catch-all for lack of a title_count hit)."""
    message = EmailMessage(
        id="fixture-linkedin-non-tech-title-alert",
        from_address="jobalerts-noreply@linkedin.com",
        subject="Web Developer II - FE/UI Inst Advancement at Woodbury School of Business",
        snippet="Based on your profile",
        body_plain="Web Developer II - FE/UI Inst Advancement at Woodbury School of Business. Apply on LinkedIn.",
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.SINGLE_JD, f"got {result.label.value}; reasons={result.reasons}"


def test_unresolvable_single_role_phrasing_stays_link_only_digest() -> None:
    """A digest-sender subject that names a company but no clean, specific
    title (e.g. "... hiring for a Cloud role") shouldn't be forced into
    SINGLE_JD just because it isn't a multi-job digest either — with
    nothing extractable, LINK_ONLY_DIGEST (-> NEEDS_REVIEW, human looks at
    it) is still the safe outcome, not a silent NOISE drop."""
    message = EmailMessage(
        id="fixture-linkedin-vague-role-alert",
        from_address="jobs-noreply@linkedin.com",
        subject="Kaleidoscope Innovation is hiring for a Cloud role",
        snippet="Based on your profile",
        body_plain="Kaleidoscope Innovation is hiring for a Cloud role. Apply on LinkedIn.",
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.LINK_ONLY_DIGEST, f"got {result.label.value}; reasons={result.reasons}"


def test_genuine_linkedin_multi_job_digest_still_link_only_digest() -> None:
    """The fix above must not swallow real multi-job digests from the same
    sender domain — digest-phrased subjects and link-heavy bodies still win
    outright, independent of the single-job-signal carve-out."""
    message = EmailMessage(
        id="fixture-linkedin-multi-job-digest",
        from_address="jobalerts-noreply@linkedin.com",
        subject="New jobs similar to Senior Software Engineer at Elicit",
        snippet="More roles like this one",
        body_plain=(
            "Here are jobs matching your preferences:\n\n"
            "https://www.linkedin.com/jobs/view/111\n"
            "https://www.linkedin.com/jobs/view/222\n"
            "https://www.linkedin.com/jobs/view/333\n"
        ),
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.LINK_ONLY_DIGEST, f"got {result.label.value}; reasons={result.reasons}"


def test_single_job_alert_with_link_heavy_body_still_reaches_single_jd() -> None:
    """Regression for 2026-07-06 (2nd pass): a real LinkedIn "Job Alert"
    template ships ~10 tracking/footer/unsubscribe links even when the body
    contains exactly ONE job card — that alone used to trip `link_heavy`
    and override a clean, unambiguous single-role subject, parking ~87% of
    a real triage backlog in LINK_ONLY_DIGEST -> NEEDS_REVIEW with zero
    extraction ever attempted. `link_heavy` must defer to
    `single_job_signal` exactly like the sender-domain check already does;
    only `digest_subject` (a real, independent multi-job phrasing signal)
    still wins outright regardless."""
    message = EmailMessage(
        id="fixture-linkedin-single-job-alert-link-heavy",
        from_address="jobalerts-noreply@linkedin.com",
        subject="Senior Software Engineer at Podium",
        snippet="New jobs match your preferences.",
        body_plain=(
            "Senior Software Engineer at Podium\n"
            "Posted on 7/4/2026\n"
            "Your job alert for Senior Software Engineer in Lehi\n"
            "New jobs match your preferences.\n"
            "Senior Software Engineer\n"
            "Podium\n"
            "Lehi, UT\n"
            "Top applicant\n"
            "View job: https://www.linkedin.com/comm/jobs/view/1/?a=1\n"
            + "\n".join(f"https://www.linkedin.com/comm/footer/{i}" for i in range(9))
        ),
        body_html="",
        date="2026-07-04T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.SINGLE_JD, f"got {result.label.value}; reasons={result.reasons}"


@pytest.mark.parametrize(
    "case_id,from_address,subject,snippet",
    [
        (
            "epicor-after-careful-review",
            "wdsetup@myworkday.com",
            "Thank you for your interest in Epicor",
            "After careful review, we have decided to move forward with other "
            "candidates for this open position.",
        ),
        (
            "nice-move-forward-with-other",
            "no-reply@nice.com",
            "Regarding your application to NICE",
            "we have decided to move forward with other candidates for this role.",
        ),
        (
            "workday-data-engineer-iii-move-forward",
            "purple@myworkday.com",
            "Update on your Application for Data Engineer III (Hybrid)",
            "At this time, we have decided to move forward with other "
            "candidates for the role",
        ),
    ],
)
def test_real_rejection_samples_previously_missed(
    case_id: str, from_address: str, subject: str, snippet: str
) -> None:
    """Regression for 2026-07-14: these 3 real rejection emails (gathered
    from Mail.app archives) all used "move forward with other candidate(s)"
    — a phrase absent from _REJECTION_PATTERNS before this date — and none
    of them matched any other existing pattern in either the subject or
    body, so all 3 classified as something other than REJECTION."""
    message = EmailMessage(
        id=f"fixture-{case_id}",
        from_address=from_address,
        subject=subject,
        snippet=snippet,
        body_plain=snippet,
        body_html="",
        date="2026-05-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.REJECTION, f"got {result.label.value}; reasons={result.reasons}"


@pytest.mark.parametrize(
    "case_id,from_address,subject,snippet",
    [
        (
            "angel-studios-wont-be-able-to-invite",
            "no-reply@hire.lever.co",
            "Angel Studios Application — Data Scientist",
            "unfortunately, at this time we won't be able to invite you to the next stage of "
            "the hiring process",
        ),
        (
            "zapier-different-direction",
            "hello@withtwill.com",
            "Update on your application for Engineering Manager, Growth role",
            "Unfortunately, the hiring team decided to go in a different direction.",
        ),
        (
            "lightspeed-dms-not-proceed-with-candidacy",
            "no-reply@lightspeeddms.com",
            "Your application to Lightspeed DMS",
            "Unfortunately, we have decided not to proceed with your candidacy for the Senior "
            "Java Software Engineer opening at Lightspeed DMS.",
        ),
    ],
)
def test_real_rejection_samples_from_earlier_2026_archive(
    case_id: str, from_address: str, subject: str, snippet: str
) -> None:
    """Regression for 2026-07-14 (2nd batch): 3 more real rejection emails
    (Angel Studios, Zapier, Lightspeed DMS — dated 10/2025-3/2026, the oldest
    in the Mail.app archive review) found while checking for gaps beyond the
    8 samples above. All 3 already match the existing "unfortunately,"/"unfortunately
    we" pattern with no changes needed — locked in here as a regression test
    rather than a classifier change, since a future edit to that pattern
    could otherwise silently regress these without anyone noticing."""
    message = EmailMessage(
        id=f"fixture-{case_id}",
        from_address=from_address,
        subject=subject,
        snippet=snippet,
        body_plain=snippet,
        body_html="",
        date="2026-03-19T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.REJECTION, f"got {result.label.value}; reasons={result.reasons}"


def test_schema_org_marketing_json_ld_is_noise() -> None:
    """Regression: some senders' marketing mail leaks raw schema.org JSON-LD
    (promo cards, discount offers) into body_plain. That's a strong noise
    signal even if it incidentally mentions job-title-like words, since real
    listings never ship as JSON-LD markup."""
    message = EmailMessage(
        id="fixture-schema-org-noise",
        from_address="jobs@inform.theladders.com",
        subject="RE: Your Onsite Action",
        snippet="Is your dream job a destination?",
        body_plain=(
            'Is your dream job a destination? {"@context": "http://schema.org/", '
            '"@type": "EmailMessage", "action": {"@type": "DiscountOffer"}} '
            "Software Engineer roles await."
        ),
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    result = classify(message)
    assert result.label == Label.NOISE
