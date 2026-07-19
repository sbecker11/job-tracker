"""Unit tests for `pipeline.run.choose_apply_url` — see its docstring for the
2026-07-19 bug this exists to fix (a stale LinkedIn email-notification
tracking link silently winning over a durable, ATS-resolved canonical URL)."""

from __future__ import annotations

from job_tracker.pipeline.run import choose_apply_url


def test_prefers_resolved_url_over_linkedin_tracking_link():
    extracted = (
        "https://www.linkedin.com/comm/jobs/view/4397699889/"
        "?trackingId=WNPi43xBRtaSLXI%2BJiLwbQ%3D%3D&otpToken=abc123"
    )
    resolved = "https://job-boards.greenhouse.io/cloverhealth/jobs/8031845"
    assert choose_apply_url(extracted, resolved) == resolved


def test_prefers_resolved_url_over_bare_linkedin_domain():
    assert choose_apply_url("https://linkedin.com/jobs/view/123", "https://acme.com/careers/1") == (
        "https://acme.com/careers/1"
    )


def test_keeps_non_linkedin_extracted_url_over_resolved():
    extracted = "https://acme.com/careers/senior-swe"
    resolved = "https://job-boards.greenhouse.io/acme/jobs/999"
    assert choose_apply_url(extracted, resolved) == extracted


def test_falls_back_to_extracted_when_no_resolved_url():
    assert choose_apply_url("https://linkedin.com/jobs/view/123", "") == "https://linkedin.com/jobs/view/123"


def test_falls_back_to_resolved_when_no_extracted_url():
    assert choose_apply_url("", "https://job-boards.greenhouse.io/acme/jobs/999") == (
        "https://job-boards.greenhouse.io/acme/jobs/999"
    )


def test_empty_when_both_empty():
    assert choose_apply_url("", "") == ""


def test_malformed_extracted_url_falls_back_to_resolved_safely():
    # urlparse doesn't normally raise on garbage input, but the function
    # guards with try/except anyway — confirm a clearly non-URL string
    # doesn't crash and just behaves like a non-LinkedIn extracted URL.
    resolved = "https://job-boards.greenhouse.io/acme/jobs/999"
    assert choose_apply_url("not a url at all", resolved) == "not a url at all"
