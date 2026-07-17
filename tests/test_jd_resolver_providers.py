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
                    "location": "Remote",
                    "jobUrl": "https://ashby/1",
                    "descriptionHtml": "<p>Ashby JD</p>",
                }
            ]
        },
    )
    ash = jd_resolver.list_ashby("acme")
    assert ash[0].title == "ML"

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
        _raw_description_html="<p>Already here</p>",
    )
    assert "Already here" in fetch_full_description(p)

    monkeypatch.setattr(
        jd_resolver,
        "_get_json",
        lambda url: {"content": "<p>GH full</p>"} if "greenhouse" in url else None,
    )
    p2 = Posting(provider="greenhouse", board_token="t", job_id="9", title="SWE")
    assert "GH full" in fetch_full_description(p2)

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
    p3 = Posting(provider="smartrecruiters", board_token="t", job_id="9", title="SWE")
    text = fetch_full_description(p3)
    assert "Do things" in text and "Python" in text

    p4 = Posting(provider="unknown", board_token="t", job_id="9", title="SWE")
    assert fetch_full_description(p4) == ""


def test_board_tokens_for_pinned():
    tokens = jd_resolver._board_tokens_for("Ancestry", "greenhouse")
    assert tokens  # at least candidate tokens
    # pinned path if present in KNOWN_BOARDS
    for company, pins in jd_resolver.KNOWN_BOARDS.items():
        if "greenhouse" in pins:
            toks = jd_resolver._board_tokens_for(company, "greenhouse")
            assert pins["greenhouse"] == toks[0]
            break
