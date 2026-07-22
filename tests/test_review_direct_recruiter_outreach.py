"""Tests for cli/review_direct_recruiter_outreach.py."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.review_direct_recruiter_outreach import main, suggest
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


def _seed(conn, **overrides) -> JobLead:
    fields = dict(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd", jd_text="")
    fields.update(overrides)
    lead = JobLead(**fields)
    upsert_lead(conn, lead)
    return lead


class TestSuggest:
    def test_linkedin_message_source_label_is_always_suggested_true(self):
        assert suggest(source_label="linkedin_message", jd_text="") is True

    def test_thin_personal_pitch_text_is_suggested_true(self):
        assert suggest(
            source_label="single-jd",
            jd_text="I came across your profile and would love to connect for a quick chat about an opportunity.",
        ) is True

    def test_real_jd_text_is_suggested_false(self):
        assert suggest(
            source_label="single-jd",
            jd_text="Acme is hiring a Software Engineer. Responsibilities: build APIs. Apply now.",
        ) is False


class TestMain:
    def test_reports_nothing_to_review_when_all_decided(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        conn.close()
        rc = main(["--db", str(db_path)], input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")))
        assert rc == 0
        assert "Nothing to review" in capsys.readouterr().out

    def test_accepting_default_via_enter_sets_suggested_value(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        lead = _seed(conn, source_label="linkedin_message", jd_text="")
        conn.close()

        rc = main(["--db", str(db_path)], input_func=lambda _: "")
        assert rc == 0

        conn = connect(db_path)
        row = conn.execute(
            "SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
        ).fetchone()
        assert row["direct_recruiter_outreach"] == 1
        conn.close()

    def test_explicit_no_overrides_a_true_suggestion(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        lead = _seed(conn, source_label="linkedin_message", jd_text="")
        conn.close()

        rc = main(["--db", str(db_path)], input_func=lambda _: "n")
        assert rc == 0

        conn = connect(db_path)
        row = conn.execute(
            "SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
        ).fetchone()
        assert row["direct_recruiter_outreach"] == 0
        conn.close()

    def test_skip_leaves_lead_undecided(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        lead = _seed(conn, source_label="single-jd", jd_text="A real JD with responsibilities and requirements.")
        conn.close()

        rc = main(["--db", str(db_path)], input_func=lambda _: "s")
        assert rc == 0

        conn = connect(db_path)
        row = conn.execute(
            "SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
        ).fetchone()
        assert row["direct_recruiter_outreach"] is None
        conn.close()

    def test_quit_stops_and_leaves_remaining_undecided(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        first = _seed(conn, company="Acme", title="Engineer", source_label="single-jd", jd_text="")
        second = _seed(conn, company="Beta Co", title="Engineer", source_label="single-jd", jd_text="")
        conn.close()

        answers = iter(["q"])
        rc = main(["--db", str(db_path)], input_func=lambda _: next(answers))
        assert rc == 0

        conn = connect(db_path)
        row1 = conn.execute(
            "SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (first.normalized_key,)
        ).fetchone()
        row2 = conn.execute(
            "SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (second.normalized_key,)
        ).fetchone()
        assert row1["direct_recruiter_outreach"] is None
        assert row2["direct_recruiter_outreach"] is None
        conn.close()

    def test_limit_caps_how_many_leads_are_reviewed(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        _seed(conn, company="Acme", title="Engineer", source_label="single-jd", jd_text="")
        _seed(conn, company="Beta Co", title="Engineer", source_label="single-jd", jd_text="")
        conn.close()

        calls = []

        def _input(prompt):
            calls.append(prompt)
            return "n"

        rc = main(["--db", str(db_path), "--limit", "1"], input_func=_input)
        assert rc == 0
        assert len(calls) == 1

    def test_already_decided_leads_are_excluded_from_the_queue(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        decided = _seed(conn, company="Acme", title="Engineer", source_label="single-jd", jd_text="")
        conn.execute(
            "UPDATE job_leads SET direct_recruiter_outreach = 1 WHERE normalized_key = ?", (decided.normalized_key,)
        )
        conn.commit()
        conn.close()

        rc = main(["--db", str(db_path)], input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")))
        assert rc == 0
