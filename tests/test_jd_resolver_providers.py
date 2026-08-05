"""Mocked HTTP coverage for ATS provider listers and description fetch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from job_tracker.ats import jd_resolver
from job_tracker.ats.jd_resolver import Posting, fetch_full_description


def test_get_json_success_and_failures(monkeypatch):
    class Resp:
        def __init__(self, status=200, payload=None, bad_json=False):
            self.status_code = status
            self._payload = payload
            self._bad_json = bad_json

        def json(self):
            if self._bad_json:
                raise ValueError("bad")
            return self._payload

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if "retry" in url:
            if calls["n"] == 1:
                raise jd_resolver.requests.RequestException("boom")
            return Resp(payload={"ok": True})
        if "ratelimit" in url:
            return Resp(status=429, payload={"ok": True})
        if "http404" in url:
            return Resp(status=404)
        if "badjson" in url:
            return Resp(payload=None, bad_json=True)
        return Resp(payload={"ok": True})

    monkeypatch.setattr(jd_resolver.requests, "get", fake_get)
    assert jd_resolver._get_json("https://x/ok") == {"ok": True}
    assert jd_resolver._get_json("https://x/http404") is None
    assert jd_resolver._get_json("https://x/badjson") is None
    calls["n"] = 0
    assert jd_resolver._get_json("https://x/retry", retries=1) == {"ok": True}
    with patch.object(jd_resolver.time, "sleep", lambda s: None):
        calls["n"] = 0
        # 429 then we'd retry - but fake always returns 429; after retries returns None
        assert jd_resolver._get_json("https://x/ratelimit", retries=0) is None


def test_list_providers_parse(monkeypatch):
    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: {
            "jobs": [
                {
                    "id": 1,
                    "title": "SWE",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://gh/1",
                }
            ]
        }
        if "greenhouse" in url
        else None,
    )
    gh = jd_resolver.list_greenhouse("acme")
    assert gh and gh[0].title == "SWE"

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: [
            {
                "id": "abc",
                "text": "DE",
                "categories": {"location": "UT"},
                "hostedUrl": "https://lever/1",
                "description": "<p>Hi</p>",
                "lists": [{"text": "Reqs", "content": "<li>Python</li>"}],
                "additional": "<p>More</p>",
            }
        ],
    )
    lv = jd_resolver.list_lever("acme")
    assert lv[0].title == "DE"
    assert "Hi" in lv[0]._raw_description_html

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: {
            "jobs": [
                {
                    "id": "a1",
                    "title": "ML",
                    "location": "San Mateo, CA",
                    "workplaceType": "Hybrid",
                    "employmentType": "FullTime",
                    "isRemote": True,  # Ashby often lies; prefer workplaceType
                    "jobUrl": "https://ashby/1",
                    "descriptionHtml": "<p>Ashby JD</p>",
                    "compensation": {
                        "compensationTierSummary": "$143K – $179K • Offers Equity"
                    },
                }
            ]
        },
    )
    ash = jd_resolver.list_ashby("acme")
    assert ash[0].title == "ML"
    assert ash[0].location == "San Mateo, CA"
    assert ash[0].workplace_type == "Hybrid"
    assert ash[0].employment_type == "FullTime"
    assert "$143K" in ash[0].compensation_summary

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: {
            "content": [
                {
                    "id": "sr1",
                    "name": "SRE",
                    "location": {"city": "NY", "country": "US"},
                    "ref": "https://sr/1",
                }
            ]
        },
    )
    sr = jd_resolver.list_smartrecruiters("acme")
    assert sr and sr[0].title == "SRE"
    assert "NY" in sr[0].location


def test_fetch_full_description_paths(monkeypatch):
    p = Posting(
        provider="lever",
        board_token="t",
        job_id="1",
        title="SWE",
        location="Remote",
        _raw_description_html="<p>Already here</p>",
    )
    lever_text = fetch_full_description(p)
    assert "Already here" in lever_text
    assert lever_text.startswith("Location: Remote")

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: (
            {
                "content": "<p>GH full</p>",
                "location": {
                    "name": "New Jersey, USA; New York, USA; San Francisco, California, United States"
                },
            }
            if "greenhouse" in url
            else None
        ),
    )
    p2 = Posting(provider="greenhouse", board_token="t", job_id="9", title="SWE")
    gh_text = fetch_full_description(p2)
    assert "GH full" in gh_text
    assert gh_text.startswith("Location: New Jersey, USA")
    assert "San Francisco" in gh_text.split("\n\n", 1)[0]

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: {
            "jobAd": {
                "sections": {
                    "jobDescription": {"title": "Role", "text": "<p>Do things</p>"},
                    "qualifications": {"title": "Reqs", "text": "<p>Python</p>"},
                }
            }
        },
    )
    p3 = Posting(
        provider="smartrecruiters", board_token="t", job_id="9", title="SWE", location="NY"
    )
    text = fetch_full_description(p3)
    assert "Do things" in text and "Python" in text
    assert text.startswith("Location: NY")

    p4 = Posting(provider="unknown", board_token="t", job_id="9", title="SWE")
    assert fetch_full_description(p4) == ""


def test_with_location_header_idempotent():
    from job_tracker.ats.jd_resolver import _with_location_header

    assert _with_location_header("Body", "Remote") == "Location: Remote\n\nBody"
    assert _with_location_header("Location: Remote\n\nBody", "SF") == "Location: Remote\n\nBody"
    assert _with_location_header("", "SF") == "Location: SF"
    assert _with_location_header("Body", "") == "Body"


def test_with_location_header_ashby_sidebar():
    """Notable-class: Hybrid + city + comp must reach dealbreaker text."""
    from job_tracker.ats.jd_resolver import Posting, _with_location_header, fetch_full_description

    text = _with_location_header(
        "We also have remote employees.",
        "San Mateo, CA",
        workplace_type="Hybrid",
        employment_type="FullTime",
        compensation="$143K – $179K • Offers Equity",
    )
    assert text.startswith("Location: San Mateo, CA")
    assert "Location Type: Hybrid" in text
    assert "Employment Type: Full time" in text
    assert "Compensation: $143K" in text
    assert "remote employees" in text

    p = Posting(
        provider="ashby",
        board_token="notable",
        job_id="x",
        title="SWE",
        location="San Mateo, CA",
        workplace_type="Hybrid",
        employment_type="FullTime",
        compensation_summary="$143K – $179K",
        _raw_description_html="<p>Body mentions remote employees</p>",
    )
    fetched = fetch_full_description(p)
    assert "Location Type: Hybrid" in fetched
    assert fetched.startswith("Location: San Mateo, CA")


def test_board_tokens_for_pinned():
    tokens = jd_resolver._board_tokens_for("Ancestry", "greenhouse")
    assert tokens  # at least candidate tokens
    # pinned path if present in KNOWN_BOARDS
    for company, pins in jd_resolver.KNOWN_BOARDS.items():
        if "greenhouse" in pins:
            toks = jd_resolver._board_tokens_for(company, "greenhouse")
            assert pins["greenhouse"] == toks[0]
            break
