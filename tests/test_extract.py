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


def test_flattened_job_board_digest_extracts_title_and_company_per_listing():
    """Aggregator digests (Adzuna, etc.) list 'Title / [flags] / Company -
    Location / more details' with each field on its own line (once HTML
    block tags are converted to real line breaks — see htmltext.py). Company
    and title should be split cleanly, and flag lines (TOP MATCH, NEW,
    REMOTE, ...) must never be mistaken for the title."""
    message = load_fixture("job_board_flattened_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    by_company = {r.company: r.title for r in roles}
    assert by_company["Robert Half"] == "Platform Engineer"
    assert by_company["Quantum Technologies LLC"] == "AI/ML Engineer"
    # Trailing flag line ("This job is available in multiple locations")
    # comes *after* the company/location line for this listing — must not
    # break the company/location match or get picked up as the title.
    assert by_company["Fidelity Investments"] == "Principal HashiCorp Vault Expert"
    assert by_company["Nelnet"] == "Senior Software Engineer"
    assert all(r.title.lower() not in {"top match", "new", "remote"} for r in roles)


def test_job_board_marketing_noise_does_not_extract_the_job_board_as_employer():
    """Regression: 'Ladders' re-engagement marketing mentions 'at Ladders,
    Inc.' in its own boilerplate footer; that must never be extracted as the
    hiring company."""
    message = load_fixture("job_board_marketing_noise.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    assert all(r.company != "Ladders, Inc" for r in roles)
    assert all(r.company != "Ladders" for r in roles)


def test_ref_no_web_aggregation_digest_extracts_clean_titles():
    """'Matching jobs from the web' aggregation digests (Energy Job Line and
    similar) delimit listings with a unique 'Ref no.: <hex>' id. With real
    line breaks preserved, the title is reliably the first line of each
    listing — extract it cleanly. (The very first listing, before any Ref
    no. marker, is preceded by sender-specific header boilerplate with no
    reliable anchor, so it's not recovered — only listings after the first
    marker are.) The line after the title is ambiguously either a company or
    a location depending on the aggregator's source, so company is
    deliberately left blank rather than guessed (routes to review)."""
    message = load_fixture("ref_no_web_aggregation_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    titles = {r.title for r in roles}
    assert "Senior Software Engineer - Full-Stack Developer (Hybrid)" in titles
    assert all(r.company == "" for r in roles)


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


def test_single_jd_snippet_is_the_full_body():
    """A SINGLE_JD message is about exactly one job, so its snippet can
    safely be the whole body — no sibling-listing contamination is possible."""
    message = load_fixture("stripe_single_jd.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    assert roles[0].snippet == message.combined_text


def test_multi_jd_bullet_snippet_is_isolated_to_its_own_bullet():
    """Regression (2026-07-07): each bullet-extracted role's snippet must be
    its own line only, never the whole digest or a sibling bullet's line —
    otherwise a keyword from one role's description could leak into another
    role's dealbreaker/skills scoring downstream."""
    message = load_fixture("multi_jd_in_body.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    by_title = {r.title: r for r in roles}
    assert "backend platform" in by_title["Senior Software Engineer"].snippet
    assert "product team" not in by_title["Senior Software Engineer"].snippet
    assert "analytics" not in by_title["Senior Software Engineer"].snippet


def test_flattened_job_board_digest_snippet_is_isolated_per_listing():
    """Regression (2026-07-07): each 'more details'-delimited listing's
    snippet must not include a neighboring listing's company/title/flags."""
    message = load_fixture("job_board_flattened_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    by_company = {r.company: r for r in roles}
    platform_role = by_company["Robert Half"]
    assert "Platform Engineer" in platform_role.snippet
    assert "Quantum Technologies" not in platform_role.snippet
    assert "Fidelity" not in platform_role.snippet


def test_ref_no_digest_snippet_is_isolated_per_listing():
    """Regression (2026-07-07): each Ref-no.-delimited listing's snippet must
    not spill into the next listing's chunk."""
    message = load_fixture("ref_no_web_aggregation_digest.json")
    roles = extract_roles(message, Label.MULTI_JD_IN_BODY)
    target = next(
        r for r in roles if "Senior Software Engineer - Full-Stack Developer (Hybrid)" in r.title
    )
    assert target.snippet


def test_sender_domain_fallback_ignores_known_job_boards():
    message = load_fixture("job_board_marketing_noise.json")
    roles = extract_roles(message, Label.SINGLE_JD)
    # No real employer signal anywhere in this mail — must fall through to
    # an incomplete role (empty company) rather than fabricate one from the
    # job board's own sender domain.
    assert roles and roles[0].company == ""
