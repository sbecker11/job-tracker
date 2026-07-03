"""Tests for role fan-out extraction (company/title from SINGLE_JD / MULTI_JD)."""

from __future__ import annotations

import json
from pathlib import Path

from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.extract import extract_roles

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> EmailMessage:
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return EmailMessage(**data)


def test_single_jd_extracts_company_and_title():
    message = load_fixture("stripe_single_jd.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    assert len(roles) == 1
    role = roles[0]
    assert role.company == "Stripe"
    assert "Software Engineer" in role.title
    assert role.apply_url.startswith("https://boards.greenhouse.io/stripe/")
    assert role.confidence > 0


def test_single_jd_company_does_not_bleed_across_lines():
    """Regression: company extraction must not span into a following greeting line."""
    message = load_fixture("stripe_single_jd.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    assert "\n" not in roles[0].company
    assert "Hi Shawn" not in roles[0].company


def test_multi_jd_extracts_one_role_per_bullet():
    message = load_fixture("multi_jd_in_body.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    titles = {r.title for r in roles}
    assert "Senior Software Engineer" in titles
    assert "Full Stack Engineer" in titles
    assert "Data Engineer" in titles
    assert all(r.company == "StartupCo" for r in roles)


def test_multi_jd_pairs_apply_urls_in_order():
    message = load_fixture("multi_jd_in_body.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    by_title = {r.title: r for r in roles}
    assert by_title["Senior Software Engineer"].apply_url.endswith("senior-backend")
    assert by_title["Full Stack Engineer"].apply_url.endswith("fullstack")
    # Third role has no matching URL (only 2 ATS links for 3 bullets) — should be empty, not misassigned.
    assert by_title["Data Engineer"].apply_url == ""


def test_no_roles_for_non_jd_labels():
    message = load_fixture("newsletter_noise.json")
    assert extract_roles(message, Label.NOISE) == []
    assert extract_roles(message, Label.LINK_ONLY_DIGEST) == []


def test_recruiter_outreach_has_no_roles():
    message = load_fixture("recruiter_outreach.json")
    assert extract_roles(message, Label.RECRUITER_OUTREACH) == []


def test_ats_search_agent_digest_extracts_real_listings_not_the_saved_search():
    """Regression: a corporate 'search agent' digest (e.g. jobs2web) lists the
    sender company's *real* openings after 'Job Matches:'. The saved-search
    name itself ("Agent: Sr Software Engineer") is not a real posting and
    must never be extracted as a role."""
    message = load_fixture("ats_search_agent_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    titles = {r.title for r in roles}
    assert "Motion Graphics & Illustration Specialist" in titles
    assert "IT Finance Administrator" in titles
    assert "Sr Software Engineer" not in titles
    assert all(r.company == "Acme Corp" for r in roles)


def test_flattened_job_board_digest_surfaces_for_review_without_fake_company():
    """Regression: aggregator digests (Adzuna, etc.) flatten 'Title Company -
    Location more details' into one run-on paragraph with no reliable
    title/company delimiter. Rather than guess and risk a wrong split
    (e.g. company='Platform Engineer TOP MATCH NEW Robert Half'), extraction
    must leave company blank so the pipeline routes it to manual review."""
    message = load_fixture("job_board_flattened_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    assert roles, "should surface something for a human to review"
    assert all(r.company == "" for r in roles)
    assert any("Robert Half" in r.title for r in roles)


def test_job_board_marketing_noise_does_not_extract_the_job_board_as_employer():
    """Regression: 'Ladders' re-engagement marketing mentions 'at Ladders,
    Inc.' in its own boilerplate footer; that must never be extracted as the
    hiring company."""
    message = load_fixture("job_board_marketing_noise.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    assert all(r.company != "Ladders, Inc" for r in roles)
    assert all(r.company != "Ladders" for r in roles)


def test_ref_no_web_aggregation_digest_surfaces_snippets_for_review():
    """Regression: 'matching jobs from the web' aggregation digests (Energy
    Job Line and similar) delimit listings with a unique 'Ref no.: <hex>' id,
    but don't cleanly separate title from company/location. Extraction must
    surface a readable snippet for manual review rather than guess a split."""
    message = load_fixture("ref_no_web_aggregation_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    assert roles, "should surface something for a human to review"
    assert all(r.company == "" for r in roles)
    assert any("Full-Stack Developer" in r.title for r in roles)


def test_company_extraction_stops_at_sentence_boundary():
    """Regression: 'at DTN. We are hiring...' must not capture 'DTN. We' —
    the multi-word continuation in the company regexes can otherwise swallow
    the next sentence's capitalized first word."""
    message = EmailMessage(
        id="fixture-sentence-boundary",
        from_address="careers@dtn.example",
        subject="Opportunity at DTN",
        snippet="",
        body_plain="We have an opening at DTN. We are hiring a Software Engineer to join our team.",
        body_html="",
        date="2026-07-01T09:00:00Z",
    )
    roles = extract_roles(message, Label.SINGLE_JD)
    assert roles
    assert roles[0].company == "DTN"


def test_sender_domain_fallback_ignores_known_job_boards():
    message = load_fixture("job_board_marketing_noise.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    # No real employer signal anywhere in this mail — must fall through to
    # an incomplete role (empty company) rather than fabricate one from the
    # job board's own sender domain.
    assert roles and roles[0].company == ""
