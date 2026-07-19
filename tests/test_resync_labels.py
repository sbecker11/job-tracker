"""Tests for cli/resync_labels.py (2026-07-19): re-syncing a triaged
message's JobTracker/PURSUE|SKIP|NEEDS_REVIEW Gmail label to its linked
lead(s)' CURRENT verdict, since triage_recruiter_inbox.py only ever applies
that label once, at initial triage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from job_tracker.cli import resync_labels as resync_cli
from job_tracker.cli.resync_labels import main as resync_main
from job_tracker.email import gmail_writer
from job_tracker.pipeline.models import JobLead, normalize_key
from job_tracker.pipeline.store import connect, record_message_processed, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="Tractable", title="Senior SWE", source_message_id="m0", source_label="recruiter_job"))
    upsert_lead(conn, JobLead(company="Path Robotics", title="ML Engineer", source_message_id="m1", source_label="recruiter_job"))
    conn.close()
    return db_path


def _key(company: str, title: str) -> str:
    return normalize_key(company, title)


class _FakeGmailService:
    """Just enough of the Gmail API surface for resync_labels.py: label
    list/create, message list-by-label-query, and modify (recorded for
    assertions, never actually mutating anything)."""

    def __init__(self, label_membership: dict[str, list[str]] | None = None):
        # {label_name: [message_id, ...]} — which messages currently carry
        # which outcome label, standing in for live Gmail search results.
        self.label_membership = label_membership or {}
        self.modify_calls: list[dict] = []
        self._labels = {name: f"Label_{i}" for i, name in enumerate(gmail_writer.ALL_OUTCOME_LABELS, start=1)}

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return self

    def list(self, **kwargs):
        if "q" not in kwargs:
            return SimpleNamespace(
                execute=lambda: {"labels": [{"id": lid, "name": name} for name, lid in self._labels.items()]}
            )
        query = kwargs["q"]
        for name in gmail_writer.ALL_OUTCOME_LABELS:
            if f"label:{name}" in query:
                ids = self.label_membership.get(name, [])
                return SimpleNamespace(execute=lambda ids=ids: {"messages": [{"id": i} for i in ids]})
        return SimpleNamespace(execute=lambda: {"messages": []})

    def create(self, **kwargs):
        name = kwargs["body"]["name"]
        label_id = self._labels.setdefault(name, f"Label_{len(self._labels) + 1}")
        return SimpleNamespace(execute=lambda: {"id": label_id, "name": name})

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        return SimpleNamespace(execute=lambda: {})


@pytest.fixture()
def mock_gmail(monkeypatch):
    def _install(service):
        monkeypatch.setattr(resync_cli, "default_credentials_path", lambda *a, **k: Path("/tmp/c.json"))
        monkeypatch.setattr(resync_cli, "default_token_path", lambda *a, **k: Path("/tmp/t.json"))
        monkeypatch.setattr(resync_cli, "get_gmail_service", lambda *a, **k: service)
        monkeypatch.setattr(resync_cli, "get_gmail_service_writable", lambda *a, **k: service)
        return service

    return _install


def test_relabels_when_llm_review_overturns_the_rule_based_verdict(mock_gmail, seeded_db: Path):
    """The exact live scenario found 2026-07-19: initial triage's rule-based
    pass said PURSUE, a later full LLM review said pass — the message must
    move from JobTracker/PURSUE to JobTracker/SKIP."""
    key = _key("Tractable", "Senior SWE")
    conn = connect(seeded_db)
    record_message_processed(conn, "msg-1", outcome="PURSUE", lead_keys=[key], label_applied=gmail_writer.PURSUE_LABEL)
    conn.execute("UPDATE job_leads SET llm_verdict = 'pass' WHERE normalized_key = ?", (key,))
    conn.commit()
    conn.close()

    service = mock_gmail(_FakeGmailService({gmail_writer.PURSUE_LABEL: ["msg-1"]}))
    rc = resync_main(["--db", str(seeded_db)])
    assert rc == 0

    calls = [c for c in service.modify_calls if c["id"] == "msg-1"]
    assert len(calls) == 1
    assert calls[0]["body"]["addLabelIds"] == [service._labels[gmail_writer.SKIP_LABEL]]
    assert service._labels[gmail_writer.PURSUE_LABEL] in calls[0]["body"]["removeLabelIds"]


def test_no_change_when_label_already_matches_current_verdict(mock_gmail, seeded_db: Path):
    key = _key("Tractable", "Senior SWE")
    conn = connect(seeded_db)
    record_message_processed(conn, "msg-2", outcome="PURSUE", lead_keys=[key], label_applied=gmail_writer.PURSUE_LABEL)
    conn.execute("UPDATE job_leads SET llm_verdict = 'pursue' WHERE normalized_key = ?", (key,))
    conn.commit()
    conn.close()

    service = mock_gmail(_FakeGmailService({gmail_writer.PURSUE_LABEL: ["msg-2"]}))
    rc = resync_main(["--db", str(seeded_db)])
    assert rc == 0
    assert service.modify_calls == []


def test_multi_role_digest_uses_pursue_review_pass_priority(mock_gmail, seeded_db: Path):
    """A message with two linked leads (a digest) — one now 'pass', the
    other 'pursue' — must stay/move to PURSUE (pursue beats pass), same
    priority rule as initial triage's _decide_outcome."""
    key1 = _key("Tractable", "Senior SWE")
    key2 = _key("Path Robotics", "ML Engineer")
    conn = connect(seeded_db)
    record_message_processed(
        conn, "msg-3", outcome="NEEDS_REVIEW", lead_keys=[key1, key2], label_applied=gmail_writer.NEEDS_REVIEW_LABEL
    )
    conn.execute("UPDATE job_leads SET llm_verdict = 'pass' WHERE normalized_key = ?", (key1,))
    conn.execute("UPDATE job_leads SET llm_verdict = 'pursue' WHERE normalized_key = ?", (key2,))
    conn.commit()
    conn.close()

    service = mock_gmail(_FakeGmailService({gmail_writer.NEEDS_REVIEW_LABEL: ["msg-3"]}))
    rc = resync_main(["--db", str(seeded_db)])
    assert rc == 0

    calls = [c for c in service.modify_calls if c["id"] == "msg-3"]
    assert len(calls) == 1
    assert calls[0]["body"]["addLabelIds"] == [service._labels[gmail_writer.PURSUE_LABEL]]


def test_dry_run_prints_but_never_calls_modify(mock_gmail, seeded_db: Path, capsys):
    key = _key("Tractable", "Senior SWE")
    conn = connect(seeded_db)
    record_message_processed(conn, "msg-4", outcome="PURSUE", lead_keys=[key], label_applied=gmail_writer.PURSUE_LABEL)
    conn.execute("UPDATE job_leads SET llm_verdict = 'pass' WHERE normalized_key = ?", (key,))
    conn.commit()
    conn.close()

    service = mock_gmail(_FakeGmailService({gmail_writer.PURSUE_LABEL: ["msg-4"]}))
    rc = resync_main(["--db", str(seeded_db), "--dry-run"])
    assert rc == 0
    assert service.modify_calls == []
    assert "would relabel" in capsys.readouterr().out


def test_message_with_deleted_lead_is_skipped_without_crashing(mock_gmail, seeded_db: Path):
    conn = connect(seeded_db)
    record_message_processed(
        conn, "msg-5", outcome="PURSUE", lead_keys=["ghost-company::ghost-title"], label_applied=gmail_writer.PURSUE_LABEL
    )
    conn.close()

    service = mock_gmail(_FakeGmailService({gmail_writer.PURSUE_LABEL: ["msg-5"]}))
    rc = resync_main(["--db", str(seeded_db)])
    assert rc == 0
    assert service.modify_calls == []


def test_no_triaged_messages_returns_cleanly(mock_gmail, seeded_db: Path):
    service = mock_gmail(_FakeGmailService({}))
    rc = resync_main(["--db", str(seeded_db)])
    assert rc == 0
    assert service.modify_calls == []
