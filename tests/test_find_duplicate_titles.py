"""Tests for scripts/find_duplicate_titles.py."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from find_duplicate_titles import (  # noqa: E402
    find_duplicate_title_pairs,
    normalize_title,
    title_similarity,
)
from job_tracker.pipeline.models import JobLead  # noqa: E402
from job_tracker.pipeline.store import connect, upsert_lead  # noqa: E402


class TestNormalizeTitle:
    def test_expands_snr_to_senior(self):
        assert normalize_title("Snr Software Engineer") == "senior software engineer"

    def test_expands_sr_to_senior(self):
        assert normalize_title("Sr Software Engineer") == "senior software engineer"

    def test_expands_roman_numeral_to_digit(self):
        assert normalize_title("Senior Developer I") == "senior developer 1"
        assert normalize_title("Senior Developer 1") == "senior developer 1"

    def test_strips_punctuation(self):
        assert normalize_title("Full-Stack Engineer, AI") == "full stack engineer ai"
        assert normalize_title("Full Stack Engineer, AI") == "full stack engineer ai"

    def test_empty_title(self):
        assert normalize_title("") == ""
        assert normalize_title(None) == ""


class TestTitleSimilarity:
    def test_identical_after_expansion_scores_1(self):
        assert title_similarity("Senior Developer I", "Senior Developer 1") == 1.0

    def test_snr_vs_senior_scores_high(self):
        assert title_similarity("Snr Software Engineer", "Senior Software Engineer") >= 0.85

    def test_genuinely_different_roles_score_low(self):
        assert title_similarity("Senior Backend Engineer", "Senior Frontend Engineer") < 0.85

    def test_empty_string_scores_zero(self):
        assert title_similarity("", "Senior Engineer") == 0.0


class TestFindDuplicateTitlePairs:
    def _seed(self, conn, company: str, title: str, **overrides):
        fields = dict(company=company, title=title, source_message_id=f"m-{title}", source_label="single-jd")
        fields.update(overrides)
        lead = JobLead(**fields)
        upsert_lead(conn, lead)
        return lead

    def test_finds_abbreviation_duplicate_within_same_company(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Senior Developer I")
        self._seed(conn, "Acme", "Senior Developer 1")
        pairs = find_duplicate_title_pairs(conn, threshold=0.85)
        conn.close()

        assert len(pairs) == 1
        assert pairs[0].company == "Acme"
        assert pairs[0].score == 1.0

    def test_does_not_cross_match_different_companies(self, tmp_path: Path):
        """Two companies both posting "Senior Software Engineer" is not a
        duplicate — grouping is strictly per-company."""
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Senior Software Engineer")
        self._seed(conn, "Beta Co", "Senior Software Engineer")
        pairs = find_duplicate_title_pairs(conn, threshold=0.85)
        conn.close()

        assert pairs == []

    def test_genuinely_different_titles_not_flagged(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Senior Backend Engineer")
        self._seed(conn, "Acme", "Staff Data Scientist")
        pairs = find_duplicate_title_pairs(conn, threshold=0.85)
        conn.close()

        assert pairs == []

    def test_single_lead_company_produces_no_pairs(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Senior Software Engineer")
        pairs = find_duplicate_title_pairs(conn, threshold=0.85)
        conn.close()

        assert pairs == []

    def test_results_sorted_best_match_first(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Senior Developer I")
        self._seed(conn, "Acme", "Senior Developer 1")  # exact match, score 1.0
        self._seed(conn, "Acme", "Snr Developer")  # lower match vs the others
        pairs = find_duplicate_title_pairs(conn, threshold=0.5)
        conn.close()

        assert pairs == sorted(pairs, key=lambda p: -p.score)
        assert pairs[0].score == 1.0
