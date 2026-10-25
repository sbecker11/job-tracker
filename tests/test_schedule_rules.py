"""REQUIREMENTS SYS-2, SYS-3 (fast tick + who takes the lock), SYS-6.

The hourly side (SYS-1, SYS-3's run_cycle.sh steps, SYS-4, SYS-5) is covered
by recruiting-automation/tests/test_schedule_rules.bats.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from job_tracker.cli import scan_communications, triage_recruiter_inbox
from job_tracker.pipeline import db_lock

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_fast_cycle():
    spec = importlib.util.spec_from_file_location("comms_fast_cycle", REPO_ROOT / "scripts" / "comms_fast_cycle.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comms_fast_cycle"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fast_cycle():
    return _load_fast_cycle()


def _captured_steps(fast_cycle, monkeypatch, tmp_path: Path) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, timeout):
        calls.append([str(c) for c in cmd])
        return 0

    monkeypatch.setattr(fast_cycle, "_run", fake_run)
    monkeypatch.setattr(fast_cycle, "COMMS_REPO", tmp_path)
    monkeypatch.setattr(fast_cycle, "_venv_python", lambda repo: tmp_path / "python")
    (tmp_path / "python").write_text("")
    assert fast_cycle.run_steps(db=tmp_path / "leads.db", skip_classify=False) == []
    return calls


def _script(cmd: list[str]) -> str:
    return next(Path(c).name for c in cmd if c.endswith(".py"))


def test_sys2_fast_tick_intake_steps_are_offline(fast_cycle, monkeypatch, tmp_path: Path):
    calls = _captured_steps(fast_cycle, monkeypatch, tmp_path)
    assert [_script(c) for c in calls] == [
        "run_classifier.py",
        "scan_communications.py",
        "triage_recruiter_inbox.py",
        "triage_imap_inbox.py",
        "triage_imap_inbox.py",
        "render_pending_actions.py",
    ]
    for cmd in calls:
        assert "--llm-fallback" not in cmd, cmd
        if _script(cmd).startswith("triage_"):
            assert "--offline" in cmd and "--no-generate" in cmd, cmd
    assert "--no-rescore" in calls[-1]


def test_sys3_fast_tick_skips_when_lock_busy(fast_cycle, monkeypatch, tmp_path: Path):
    db = tmp_path / "leads.db"
    db.write_text("")
    monkeypatch.setattr(fast_cycle.db_lock, "acquire", lambda path, wait_seconds=0.0: None)
    monkeypatch.setattr(fast_cycle, "run_steps", lambda **kw: pytest.fail("ran steps without the lock"))
    rc = fast_cycle.main(["--db", str(db), "--lock", str(tmp_path / "x.lock"), "--no-open"])
    assert rc == db_lock.LOCK_BUSY_EXIT_CODE


def test_sys3_only_top_level_entry_points_take_the_lock():
    """Scripts run under a lock holder must not take the lock again (it isn't
    re-entrant), so only the entry points that own a whole run may import it."""
    allowed = {
        "scripts/comms_fast_cycle.py",
        # Long LLM runs: take the DB lock per write (--lock), never under with_db_lock.
        "scripts/extract_required_skills.py",
        # Uses db_lock only for its own cache lock, not var/comms_fast.lock.
        "scripts/analyze_skills.py",
        "scripts/triage_imap_now.py",
        "scripts/with_db_lock.py",
        "src/job_tracker/pipeline/db_lock.py",
    }
    pattern = re.compile(r"\bdb_lock\b")
    takers = {
        str(p.relative_to(REPO_ROOT))
        for root in ("src", "scripts")
        for p in (REPO_ROOT / root).rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
    }
    assert takers == allowed


def test_sys3_default_lock_path():
    assert db_lock.DEFAULT_LOCK_PATH == REPO_ROOT / "var" / "comms_fast.lock"


def test_sys6_recruiting_gmail_query_is_recruiter_job_minus_own_labels():
    q = triage_recruiter_inbox.DEFAULT_QUERY
    assert q.startswith("label:Category/recruiter_job ")
    for label in ("PURSUE", "SKIP", "NEEDS_REVIEW", "Linked", "NeedsFollowup"):
        assert f"-label:JobTracker/{label}" in q


def test_sys6_linkedin_replies_are_read_by_sender():
    q = scan_communications.DEFAULT_INBOUND_QUERY
    assert "from:hit-reply@linkedin.com" in q
    assert "from:inmail-hit-reply@linkedin.com" in q


def test_sys6_comms_migration_never_touches_leads_db():
    comms = REPO_ROOT.parent / "comms-migration"
    if not comms.is_dir():
        pytest.skip("comms-migration not checked out next to job-tracker")
    hits = [
        str(p.relative_to(comms))
        for p in comms.rglob("*.py")
        if ".venv" not in p.parts
        and re.search(
            r"leads\.db|\bjob_leads\b|^\s*(?:from|import)\s+job_tracker\b",
            p.read_text(encoding="utf-8", errors="ignore"),
            re.MULTILINE,
        )
    ]
    assert hits == []
