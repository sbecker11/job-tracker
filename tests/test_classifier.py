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
