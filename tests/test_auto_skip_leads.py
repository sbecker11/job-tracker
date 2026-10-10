"""auto_skip_leads — stale (>14 days) and Eastern-time-only auto-skip rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.cli.auto_skip_leads import main as auto_skip_main
from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead

_REMOTE_JD = "Senior Software Engineer. Fully remote. Python, AWS, PostgreSQL, React."


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).replace(microsecond=0).isoformat()


def _lead(conn, company: str, *, status: str = "new", age_days: int = 1, jd_text: str = _REMOTE_JD) -> str:
    lead = JobLead(company=company, title="Senior Software Engineer", source_message_id=f"m:{company}", source_label="t")
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET status = ?, first_seen = ?, last_seen = ?, jd_text = ? WHERE normalized_key = ?",
        (status, _days_ago(age_days), _days_ago(age_days), jd_text, lead.normalized_key),
    )
    conn.commit()
    return lead.normalized_key


def _status(conn, key: str) -> str:
    return conn.execute("SELECT status FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()["status"]


def test_stale_rule_uses_last_communication_over_21_days(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    quiet_new = _lead(conn, "QuietNew", age_days=30)
    quiet_pursued = _lead(conn, "QuietPursued", status="pursued", age_days=30)
    quiet_packaged = _lead(conn, "QuietPackaged", status="package_generated", age_days=30)
    recent_email = _lead(conn, "RecentEmail", age_days=15)
    old_but_talking = _lead(conn, "OldButTalking", age_days=60)
    add_job_conversation(conn, JobConversation(job_key=old_but_talking, occurred_at=_days_ago(10), summary="recruiter reply"))
    old_outbound_only = _lead(conn, "OldOutbound", age_days=60)
    add_job_conversation(
        conn,
        JobConversation(job_key=old_outbound_only, direction="outbound", occurred_at=_days_ago(25), summary="my intro"),
    )
    old_awaiting = _lead(conn, "OldAwaiting", age_days=60)
    conn.execute("UPDATE job_leads SET awaiting_response_since = ? WHERE normalized_key = ?", (_days_ago(30), old_awaiting))
    conn.commit()
    conn.close()

    assert auto_skip_main(["--db", str(db)]) == 0

    conn = connect(db)
    assert _status(conn, quiet_new) == "skipped"
    assert _status(conn, quiet_pursued) == "skipped"
    assert _status(conn, quiet_packaged) == "package_generated"
    assert _status(conn, recent_email) == "new"
    assert _status(conn, old_but_talking) == "new"
    assert _status(conn, old_outbound_only) == "skipped"
    assert _status(conn, old_awaiting) == "skipped"
    notes = conn.execute("SELECT notes FROM job_leads WHERE normalized_key = ?", (quiet_new,)).fetchone()["notes"]
    assert "no communication in more than 21 days" in notes
    conn.close()


def test_eastern_time_rule_skips_requirement_not_preference(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    located = _lead(conn, "Located", jd_text=_REMOTE_JD + "\nCandidates located in the EST or CST time zones only.")
    hours = _lead(conn, "Hours", status="pursued", jd_text=_REMOTE_JD + "\nSchedule: Monday to Friday, 9 AM to 5 PM EST.")
    preferred = _lead(conn, "Preferred", jd_text=_REMOTE_JD + "\nRemote within the US; East Coast preferred.")
    hq = _lead(conn, "HQ", jd_text=_REMOTE_JD + "\nHeadquartered in Boston, MA. Remote US. Interested? Best of luck.")
    conn.close()

    assert auto_skip_main(["--db", str(db)]) == 0

    conn = connect(db)
    assert _status(conn, located) == "skipped"
    assert _status(conn, hours) == "skipped"
    assert _status(conn, preferred) == "new"
    assert _status(conn, hq) == "new"
    conn.close()


def test_dry_run_changes_nothing(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    key = _lead(conn, "OldNew", age_days=30)
    conn.close()

    assert auto_skip_main(["--db", str(db), "--dry-run"]) == 0
    assert "would skip" in capsys.readouterr().out

    conn = connect(db)
    assert _status(conn, key) == "new"
    conn.close()
