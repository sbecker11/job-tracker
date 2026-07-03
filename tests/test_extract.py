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
