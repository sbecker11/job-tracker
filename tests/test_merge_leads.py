"""Tests for cli/merge_leads.py."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.merge_leads import jd_text_similarity, main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


def _seed(conn, **overrides) -> JobLead:
    fields = dict(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd", jd_text="")
    fields.update(overrides)
    lead = JobLead(**fields)
    upsert_lead(conn, lead)
    return lead


class TestJdTextSimilarity:
    def test_identical_text_scores_1(self):
        assert jd_text_similarity("Build APIs in Python.", "Build APIs in Python.") == 1.0

    def test_whitespace_only_differences_score_1(self):
        assert jd_text_similarity("Build APIs\nin Python.", "Build   APIs in Python.") == 1.0

    def test_genuinely_different_text_scores_low(self):
        assert jd_text_similarity("Build APIs in Python.", "Manage a retail warehouse team.") < 0.5

    def test_either_side_empty_scores_zero(self):
        assert jd_text_similarity("", "Build APIs in Python.") == 0.0
        assert jd_text_similarity("Build APIs in Python.", "") == 0.0
        assert jd_text_similarity("", "") == 0.0


class TestMainArgValidation:
    def test_no_mode_args_errors(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        connect(db_path).close()
        rc = main(["--db", str(db_path)], input_func=lambda _: (_ for _ in ()).throw(AssertionError()))
        assert rc == 1
        assert "Specify either" in capsys.readouterr().err

    def test_mixing_both_modes_errors(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        connect(db_path).close()
        rc = main(
            ["--db", str(db_path), "--keep", "a", "--absorb", "b", "--rename-from", "X", "--rename-to", "Y"],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError()),
        )
        assert rc == 1
        assert "not both" in capsys.readouterr().err

    def test_merge_mode_requires_both_keep_and_absorb(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        connect(db_path).close()
        rc = main(["--db", str(db_path), "--keep", "a"], input_func=lambda _: (_ for _ in ()).throw(AssertionError()))
        assert rc == 1
        assert "requires both --keep and --absorb" in capsys.readouterr().err

    def test_rename_mode_requires_both_from_and_to(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        connect(db_path).close()
        rc = main(
            ["--db", str(db_path), "--rename-from", "X"],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError()),
        )
        assert rc == 1
        assert "requires both --rename-from and --rename-to" in capsys.readouterr().err


class TestMergeMode:
    def test_missing_keep_key_errors_without_prompting(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        absorb = _seed(conn, company="Scribd, Inc.", title="Data Engineer")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", "nonexistent::key", "--absorb", absorb.normalized_key],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_same_key_for_both_errors(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        lead = _seed(conn)
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", lead.normalized_key, "--absorb", lead.normalized_key],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        assert rc == 1
        assert "must refer to two different leads" in capsys.readouterr().err

    def test_declining_confirmation_leaves_both_leads_intact(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        keep = _seed(conn, company="Scribd", title="Software Engineer")
        absorb = _seed(conn, company="Scribd", title="Software Engineer (re-post)")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", keep.normalized_key, "--absorb", absorb.normalized_key],
            input_func=lambda _: "n",
        )
        assert rc == 0

        conn = connect(db_path)
        assert conn.execute(
            "SELECT 1 FROM job_leads WHERE normalized_key = ?", (absorb.normalized_key,)
        ).fetchone() is not None
        conn.close()

    def test_yes_flag_merges_without_prompting(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        keep = _seed(conn, company="Scribd", title="Software Engineer")
        absorb = _seed(conn, company="Scribd", title="Software Engineer (re-post)")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", keep.normalized_key, "--absorb", absorb.normalized_key, "--yes"],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt with --yes")),
        )
        assert rc == 0
        assert "Merged." in capsys.readouterr().out

        conn = connect(db_path)
        assert conn.execute(
            "SELECT 1 FROM job_leads WHERE normalized_key = ?", (absorb.normalized_key,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM job_leads WHERE normalized_key = ?", (keep.normalized_key,)
        ).fetchone() is not None
        conn.close()

    def test_confirming_with_y_merges(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        keep = _seed(conn, company="Scribd", title="Software Engineer")
        absorb = _seed(conn, company="Scribd", title="Software Engineer (re-post)")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", keep.normalized_key, "--absorb", absorb.normalized_key],
            input_func=lambda _: "y",
        )
        assert rc == 0

        conn = connect(db_path)
        assert conn.execute(
            "SELECT 1 FROM job_leads WHERE normalized_key = ?", (absorb.normalized_key,)
        ).fetchone() is None
        conn.close()

    def test_low_similarity_warning_printed_when_both_have_jd_text(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        keep = _seed(conn, company="Scribd", title="Software Engineer", jd_text="Build APIs in Python.")
        absorb = _seed(
            conn, company="Scribd, Inc.", title="Data Engineer", jd_text="Manage a retail warehouse team."
        )
        conn.close()

        rc = main(
            ["--db", str(db_path), "--keep", keep.normalized_key, "--absorb", absorb.normalized_key],
            input_func=lambda _: "n",
        )
        assert rc == 0
        assert "low" in capsys.readouterr().out.lower()


class TestRenameMode:
    def test_renames_all_matching_leads(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        a = _seed(conn, company="Reddit, Inc.", title="ML Engineer")
        b = _seed(conn, company="Reddit, Inc.", title="Compute Platform Engineer")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--rename-from", "Reddit, Inc.", "--rename-to", "Reddit", "--yes"],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt with --yes")),
        )
        assert rc == 0
        assert "Renamed 2 lead(s)" in capsys.readouterr().out

        conn = connect(db_path)
        row_a = conn.execute(
            "SELECT company, normalized_key FROM job_leads WHERE normalized_key = ?", (a.normalized_key,)
        ).fetchone()
        row_b = conn.execute(
            "SELECT company, normalized_key FROM job_leads WHERE normalized_key = ?", (b.normalized_key,)
        ).fetchone()
        assert row_a["company"] == "Reddit"
        assert row_b["company"] == "Reddit"
        # Both leads keep their own distinct identity — this is a rename, not a merge.
        assert row_a["normalized_key"] == a.normalized_key
        assert row_b["normalized_key"] == b.normalized_key
        conn.close()

    def test_no_matching_company_errors(self, tmp_path: Path, capsys):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        _seed(conn, company="Acme")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--rename-from", "Nonexistent Co", "--rename-to", "Something"],
            input_func=lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        assert rc == 1
        assert "No leads found" in capsys.readouterr().err

    def test_declining_confirmation_renames_nothing(self, tmp_path: Path):
        db_path = tmp_path / "leads.db"
        conn = connect(db_path)
        lead = _seed(conn, company="Reddit, Inc.")
        conn.close()

        rc = main(
            ["--db", str(db_path), "--rename-from", "Reddit, Inc.", "--rename-to", "Reddit"],
            input_func=lambda _: "n",
        )
        assert rc == 0

        conn = connect(db_path)
        row = conn.execute(
            "SELECT company FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
        ).fetchone()
        assert row["company"] == "Reddit, Inc."
        conn.close()
