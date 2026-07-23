"""Tests for scripts/unemployment_claim_report.py."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from unemployment_claim_report import (  # noqa: E402
    build_week_rows,
    default_week_start,
    floor_to_sunday,
    is_malformed_email,
    is_system_sender,
    load_registry,
    main,
    note_for,
    save_registry,
    week_bounds,
)

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead  # noqa: E402
from job_tracker.pipeline.store import (  # noqa: E402
    add_job_contact,
    add_job_conversation,
    advance_status,
    connect,
    upsert_lead,
)


def _seed_lead(conn, company: str, title: str, **overrides) -> str:
    fields = dict(company=company, title=title, source_message_id=f"m-{company}-{title}", source_label="single-jd")
    fields.update(overrides)
    lead = JobLead(**fields)
    upsert_lead(conn, lead)
    return lead.normalized_key


class TestFloorToSunday:
    def test_sunday_stays_the_same(self):
        # 2026-07-19 is a Sunday.
        assert floor_to_sunday(date(2026, 7, 19)) == date(2026, 7, 19)

    def test_monday_goes_back_one_day(self):
        assert floor_to_sunday(date(2026, 7, 20)) == date(2026, 7, 19)

    def test_saturday_goes_back_six_days(self):
        assert floor_to_sunday(date(2026, 7, 25)) == date(2026, 7, 19)


class TestDefaultWeekStart:
    def test_uses_most_recent_sunday(self):
        assert default_week_start(date(2026, 7, 23)) == date(2026, 7, 19)


class TestWeekBounds:
    def test_inclusive_sun_to_sat(self):
        start, end = week_bounds(date(2026, 7, 19))
        assert start == "2026-07-19"
        assert end == "2026-07-25"


class TestIsSystemSender:
    def test_linkedin_job_alert_bot_flagged(self):
        assert is_system_sender("jobalerts-noreply@linkedin.com") is True

    def test_ashby_ats_confirmation_flagged(self):
        assert is_system_sender("no-reply@ashbyhq.com") is True

    def test_theladders_digest_flagged(self):
        assert is_system_sender("jobs@my.theladders.com") is True

    def test_real_recruiter_email_not_flagged(self):
        assert is_system_sender("cole@crbworkforce.com") is False

    def test_empty_email_not_flagged(self):
        assert is_system_sender("") is False


class TestIsMalformedEmail:
    def test_linkedin_profile_url_is_malformed(self):
        assert is_malformed_email("linkedin.com/in/prisha-singh-102457257") is True

    def test_missing_at_sign_is_malformed(self):
        assert is_malformed_email("not-an-email") is True

    def test_real_email_is_not_malformed(self):
        assert is_malformed_email("cole@crbworkforce.com") is False

    def test_empty_string_is_not_malformed(self):
        assert is_malformed_email("") is False


class TestNoteFor:
    def test_malformed_takes_priority_over_system_sender_check(self):
        assert "malformed" in note_for("linkedin.com/in/someone")

    def test_system_sender_flagged(self):
        assert "system/no-reply" in note_for("jobalerts-noreply@linkedin.com")

    def test_real_contact_has_no_note(self):
        assert note_for("cole@crbworkforce.com") == ""


class TestBuildWeekRows:
    def test_includes_lead_with_applied_at_in_window(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        add_job_contact(conn, JobContact(job_key=key, email="recruiter@acme.example", role="recruiter"))

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert len(rows) == 1
        assert rows[0]["company"] == "Acme"
        assert rows[0]["date_of_communication"] == "2026-07-08"
        assert rows[0]["recruiter_email"] == "recruiter@acme.example"
        assert rows[0]["job_status"] == "applied"
        assert rows[0]["notes"] == ""

    def test_excludes_lead_outside_window(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-06-01T10:00:00Z")

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert rows == []

    def test_uses_earliest_qualifying_date_when_multiple_stages_in_window(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "DIRECTV", "Senior Data Engineer")
        advance_status(conn, key, "applied", when="2026-07-21T10:00:00Z")
        advance_status(conn, key, "interviewing", when="2026-07-22T10:00:00Z")

        rows = build_week_rows(conn, "2026-07-19", "2026-07-25")
        conn.close()

        assert len(rows) == 1
        assert rows[0]["date_of_communication"] == "2026-07-21"

    def test_outbound_conversation_counts_as_contact_attempt(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Clevanoo LLC", "Java Spring Boot Developer")
        add_job_conversation(
            conn,
            JobConversation(job_key=key, direction="outbound", channel="email", occurred_at="2026-07-16T09:00:00Z"),
        )

        rows = build_week_rows(conn, "2026-07-12", "2026-07-18")
        conn.close()

        assert len(rows) == 1
        assert rows[0]["date_of_communication"] == "2026-07-16"

    def test_inbound_email_alone_does_not_count(self, tmp_path: Path):
        """A purely passive inbound digest (no stage date, no outbound reply,
        no non-email channel) is not a "you did something" contact attempt."""
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "ZimZee Recruiting", "Data Engineer")
        add_job_conversation(
            conn,
            JobConversation(job_key=key, direction="inbound", channel="email", occurred_at="2026-07-09T09:00:00Z"),
        )

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert rows == []

    def test_non_email_channel_counts_regardless_of_direction(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Mayo Clinic", "Sr. Software Engineer")
        add_job_conversation(
            conn,
            JobConversation(job_key=key, direction="inbound", channel="call", occurred_at="2026-07-16T09:00:00Z"),
        )

        rows = build_week_rows(conn, "2026-07-12", "2026-07-18")
        conn.close()

        assert len(rows) == 1

    def test_excluded_job_keys_are_skipped(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11", excluded_job_keys={key})
        conn.close()

        assert rows == []

    def test_no_contact_on_file_yields_empty_email_and_no_note(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert rows[0]["recruiter_email"] == ""
        assert rows[0]["notes"] == ""

    def test_system_sender_email_is_flagged_in_notes(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key = _seed_lead(conn, "ZimZee Recruiting", "Data Engineer")
        advance_status(conn, key, "applied", when="2026-07-09T10:00:00Z")
        add_job_contact(conn, JobContact(job_key=key, email="jobalerts-noreply@linkedin.com", role="other"))

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert "system/no-reply" in rows[0]["notes"]

    def test_rows_sorted_by_date(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        key_a = _seed_lead(conn, "Company A", "Role A")
        advance_status(conn, key_a, "applied", when="2026-07-10T10:00:00Z")
        key_b = _seed_lead(conn, "Company B", "Role B")
        advance_status(conn, key_b, "applied", when="2026-07-06T10:00:00Z")

        rows = build_week_rows(conn, "2026-07-05", "2026-07-11")
        conn.close()

        assert [r["company"] for r in rows] == ["Company B", "Company A"]


class TestRegistryPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        state_path = tmp_path / "registry.json"
        save_registry(state_path, {"acme::software engineer": {"company": "Acme", "week_start": "2026-07-05"}})
        registry = load_registry(state_path)
        assert registry["acme::software engineer"]["company"] == "Acme"

    def test_load_missing_file_returns_empty_dict(self, tmp_path: Path):
        assert load_registry(tmp_path / "does-not-exist.json") == {}


class TestMainCli:
    def test_writes_csv_and_registers_job_keys(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--week-start",
                "2026-07-05",
                "--output-dir",
                str(output_dir),
            ]
        )

        assert exit_code == 0
        csv_path = output_dir / "Weekly_Claim_ContactAttempts_2026-07-05.csv"
        assert csv_path.exists()
        contents = csv_path.read_text()
        assert "Acme" in contents
        assert "Software Engineer" in contents

        registry = load_registry(output_dir / ".reported_job_keys.json")
        assert key in registry

    def test_same_lead_not_reported_twice_across_weekly_runs(self, tmp_path: Path):
        """A lead with qualifying activity in two different weeks (e.g. an
        application one week, a follow-up interview the next) must only
        land on the FIRST weekly claim it qualifies for, not both."""
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "DIRECTV", "Senior Data Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        advance_status(conn, key, "interviewing", when="2026-07-15T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        # Week 1 run
        main(["--db", str(db_path), "--week-start", "2026-07-05", "--output-dir", str(output_dir)])
        # Week 2 run (separate process invocation in practice, but same state file)
        main(["--db", str(db_path), "--week-start", "2026-07-12", "--output-dir", str(output_dir)])

        week1_csv = (output_dir / "Weekly_Claim_ContactAttempts_2026-07-05.csv").read_text()
        week2_csv = (output_dir / "Weekly_Claim_ContactAttempts_2026-07-12.csv").read_text()

        assert "DIRECTV" in week1_csv
        assert "DIRECTV" not in week2_csv

    def test_force_include_overrides_registry_exclusion(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "DIRECTV", "Senior Data Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        advance_status(conn, key, "interviewing", when="2026-07-15T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        main(["--db", str(db_path), "--week-start", "2026-07-05", "--output-dir", str(output_dir)])
        main(
            [
                "--db",
                str(db_path),
                "--week-start",
                "2026-07-12",
                "--output-dir",
                str(output_dir),
                "--force-include",
                key,
            ]
        )

        week2_csv = (output_dir / "Weekly_Claim_ContactAttempts_2026-07-12.csv").read_text()
        assert "DIRECTV" in week2_csv

    def test_dry_run_does_not_write_csv_or_registry(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        main(
            [
                "--db",
                str(db_path),
                "--week-start",
                "2026-07-05",
                "--output-dir",
                str(output_dir),
                "--dry-run",
            ]
        )

        assert not (output_dir / "Weekly_Claim_ContactAttempts_2026-07-05.csv").exists()
        assert not (output_dir / ".reported_job_keys.json").exists()

    def test_multiple_weeks_in_one_invocation(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key1 = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key1, "applied", when="2026-07-08T10:00:00Z")
        key2 = _seed_lead(conn, "Beta Corp", "Data Engineer")
        advance_status(conn, key2, "applied", when="2026-07-15T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        main(
            [
                "--db",
                str(db_path),
                "--week-start",
                "2026-07-05",
                "--weeks",
                "2",
                "--output-dir",
                str(output_dir),
            ]
        )

        assert "Acme" in (output_dir / "Weekly_Claim_ContactAttempts_2026-07-05.csv").read_text()
        assert "Beta Corp" in (output_dir / "Weekly_Claim_ContactAttempts_2026-07-12.csv").read_text()

    def test_min_contacts_warning_printed_when_below_threshold(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        main(
            [
                "--db",
                str(db_path),
                "--week-start",
                "2026-07-05",
                "--output-dir",
                str(output_dir),
                "--min-contacts",
                "4",
            ]
        )

        captured = capsys.readouterr()
        assert "WARNING" in captured.out

    def test_week_start_is_floored_to_sunday_even_if_other_weekday_given(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        key = _seed_lead(conn, "Acme", "Software Engineer")
        advance_status(conn, key, "applied", when="2026-07-08T10:00:00Z")
        conn.close()

        output_dir = tmp_path / "out"
        # 2026-07-09 is a Thursday inside the same Sun-Sat week as 2026-07-08.
        main(["--db", str(db_path), "--week-start", "2026-07-09", "--output-dir", str(output_dir)])

        assert (output_dir / "Weekly_Claim_ContactAttempts_2026-07-05.csv").exists()
