"""Offline coverage for ats.jd_resolver resolve/CLI (no live ATS HTTP)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from job_tracker.ats import jd_resolver
from job_tracker.ats.jd_resolver import Posting, main, resolve


def test_resolve_accepts_high_score_match():
    postings = [
        Posting(
            provider="greenhouse",
            board_token="acme",
            job_id="1",
            title="Senior Software Engineer",
            location="Remote",
            url="https://example.com/1",
            _raw_description_html="<p>Build APIs</p>",
        ),
        Posting(
            provider="greenhouse",
            board_token="acme",
            job_id="2",
            title="Marketing Manager",
            location="NY",
            url="https://example.com/2",
            _raw_description_html="<p>Sell stuff</p>",
        ),
    ]
    with patch.object(jd_resolver, "fetch_full_description", return_value="Build APIs"):
        result = resolve("Acme", "Senior Software Engineer", postings=list(postings), verbose=True)
    assert result["accepted"] is True
    assert result["match"]["title"] == "Senior Software Engineer"
    assert "Build APIs" in result["match"]["description"]


def test_resolve_rejects_low_score(capsys):
    postings = [
        Posting(
            provider="lever",
            board_token="acme",
            job_id="s",
            title="Sales Director",
            location="Remote",
            url="https://example.com/s",
            _raw_description_html="<p>x</p>",
        )
    ]
    result = resolve("Acme", "Senior Software Engineer", postings=list(postings), threshold=0.9)
    assert result["accepted"] is False
    assert result["match"] is None
    assert result["candidates"]


def test_gather_postings_uses_first_working_token(monkeypatch, capsys):
    calls = []

    def fake_lister(token):
        calls.append(token)
        if token == "acme":
            return [
                Posting(
                    provider="greenhouse",
                    board_token=token,
                    job_id="1",
                    title="SWE",
                    location="",
                    url="u",
                    _raw_description_html="",
                )
            ]
        return []

    monkeypatch.setitem(jd_resolver.PROVIDERS, "greenhouse", fake_lister)
    # Only exercise one provider
    monkeypatch.setattr(
        jd_resolver,
        "_board_tokens_for",
        lambda company, provider: ["wrong", "acme"],
    )
    collected = jd_resolver.gather_postings("Acme", providers=["greenhouse"], verbose=True)
    assert len(collected) == 1
    assert "wrong" in calls and "acme" in calls
    err = capsys.readouterr().err
    assert "board" in err


def test_main_selftest():
    assert main(["--selftest"]) == 0


def test_main_json_accepted(monkeypatch, capsys):
    monkeypatch.setattr(
        jd_resolver,
        "resolve",
        lambda *a, **k: {
            "company": "Acme",
            "requested_title": "SWE",
            "accepted": True,
            "match": {
                "title": "SWE",
                "match_score": 0.99,
                "provider": "greenhouse",
                "location": "Remote",
                "url": "https://x",
                "description": "JD",
            },
            "candidates": [],
        },
    )
    rc = main(["--company", "Acme", "--title", "SWE", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True


def test_main_human_no_match(monkeypatch, capsys):
    monkeypatch.setattr(
        jd_resolver,
        "resolve",
        lambda *a, **k: {
            "company": "Acme",
            "requested_title": "SWE",
            "accepted": False,
            "match": None,
            "candidates": [
                {"match_score": 0.2, "title": "Other", "provider": "lever", "url": "u"},
            ],
        },
    )
    rc = main(["--company", "Acme", "--title", "SWE"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "No confident match" in out
    assert "Closest titles" in out


def test_main_human_no_board(monkeypatch, capsys):
    monkeypatch.setattr(
        jd_resolver,
        "resolve",
        lambda *a, **k: {
            "company": "Acme",
            "requested_title": "SWE",
            "accepted": False,
            "match": None,
            "candidates": [],
        },
    )
    rc = main(["--company", "Acme", "--title", "SWE"])
    assert rc == 2
    assert "No board found" in capsys.readouterr().out


def test_main_human_accepted(monkeypatch, capsys):
    monkeypatch.setattr(
        jd_resolver,
        "resolve",
        lambda *a, **k: {
            "company": "Acme",
            "requested_title": "SWE",
            "accepted": True,
            "match": {
                "title": "SWE",
                "match_score": 0.99,
                "provider": "greenhouse",
                "location": "Remote",
                "url": "https://x",
                "description": "Full JD",
            },
            "candidates": [],
        },
    )
    rc = main(["--company", "Acme", "--title", "SWE"])
    assert rc == 0
    assert "Full JD" in capsys.readouterr().out


def test_main_requires_company_title():
    with pytest.raises(SystemExit):
        main([])
