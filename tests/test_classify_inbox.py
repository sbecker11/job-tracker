"""Tests for classify_inbox CLI (fixture path; Gmail mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_tracker.cli import classify_inbox
from job_tracker.cli.classify_inbox import main as classify_inbox_main
from job_tracker.email.models import EmailMessage

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_classify_fixture_text(capsys):
    fixture = FIXTURES / "stripe_single_jd.json"
    rc = classify_inbox_main(["--fixture", str(fixture)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "label:" in out
    assert "confidence:" in out


def test_classify_fixture_json(capsys):
    fixture = FIXTURES / "newsletter_noise.json"
    rc = classify_inbox_main(["--fixture", str(fixture), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "classification" in payload
    assert payload["classification"]["label"]


def test_classify_all_fixtures(capsys):
    rc = classify_inbox_main(["--all-fixtures", "--fixtures-dir", str(FIXTURES)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "---" in out  # multi separator


def test_classify_all_fixtures_empty_dir(tmp_path: Path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = classify_inbox_main(["--all-fixtures", "--fixtures-dir", str(empty)])
    assert rc == 1
    assert "No fixtures" in capsys.readouterr().err


def test_classify_message_id_mocked(monkeypatch, capsys):
    msg = EmailMessage(
        id="m1",
        from_address="r@example.com",
        subject="Role",
        body_plain="We are hiring a Software Engineer at Acme. Apply now.",
    )
    monkeypatch.setattr(classify_inbox, "fetch_message_by_id", lambda *a, **k: msg)
    rc = classify_inbox_main(["--message-id", "m1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"]["id"] == "m1"


def test_classify_dry_run_no_messages(monkeypatch, capsys):
    monkeypatch.setattr(classify_inbox, "fetch_unread", lambda **k: [])
    rc = classify_inbox_main(["--dry-run"])
    assert rc == 0
    assert "No messages matched" in capsys.readouterr().err


def test_classify_dry_run_with_messages(monkeypatch, capsys):
    msgs = [
        EmailMessage(id="a", from_address="a@x.com", subject="A", body_plain="noise newsletter"),
        EmailMessage(
            id="b",
            from_address="b@x.com",
            subject="B",
            body_plain="Software Engineer at Stripe https://boards.greenhouse.io/stripe/jobs/1",
        ),
    ]
    monkeypatch.setattr(classify_inbox, "fetch_unread", lambda **k: msgs)
    rc = classify_inbox_main(["--dry-run", "--newer-than", "7", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--- a ---" in out or "id:" in out
