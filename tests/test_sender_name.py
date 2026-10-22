"""Unmatched-pitch drafts greet the sender by name (REQUIREMENTS CON-1)."""

from __future__ import annotations

from job_tracker.pipeline.store import sender_name


def test_name_from_forwarded_header():
    body = "Begin forwarded message: From: Amit Kumar &lt;Amitkumar@valzosoft.com&gt; Subject: Facets"
    assert sender_name("amitkumar@valzosoft.com", body) == "Amit Kumar"
    assert sender_name("timgreenwald@harnham.com", "> From: Tim Greenwald <timgreenwald@harnham.com>") == "Tim Greenwald"


def test_falls_back_to_address_and_never_guesses():
    assert sender_name("deepak.kumar@diverselynx.com", "no header here") == "Deepak Kumar"
    assert sender_name("vinayv@fisecglobal.net", "") == ""
    assert sender_name("no-reply@alerts.talent.com", "From: Talent <no-reply@alerts.talent.com>") == ""
    assert sender_name("no.reply@email.roberthalf.com", "") == ""
    assert sender_name("aws-marketing-email-replies@amazon.com", "") == ""
    assert sender_name("data-engineer-usajobs@googlegroups.com", "") == ""
