"""Tests for scripts/find_duplicate_companies.py."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from find_duplicate_companies import (  # noqa: E402
    company_similarity,
    find_duplicate_company_pairs,
    normalize_company,
)
from job_tracker.pipeline.models import JobLead  # noqa: E402
from job_tracker.pipeline.store import connect, upsert_lead  # noqa: E402


class TestNormalizeCompany:
    def test_strips_inc_suffix(self):
        assert normalize_company("Scribd, Inc.") == "scribd"
        assert normalize_company("Scribd") == "scribd"

    def test_strips_llc_and_corp_suffixes(self):
        assert normalize_company("Acme LLC") == "acme"
        assert normalize_company("Acme Corp") == "acme"
        assert normalize_company("Acme Corporation") == "acme"

    def test_only_strips_trailing_suffix_words_not_mid_string(self):
        # "Corp" only strips when it's actually a trailing word/token, not
        # when it's merely a substring of a real word.
        assert normalize_company("Corpstart") == "corpstart"

    def test_does_not_over_strip_a_real_company_name_word(self):
        # "Company" the suffix strips; but a name that's just one word
        # matching a suffix (unlikely in practice) still folds sensibly.
        assert normalize_company("Cox Communications") == "cox communications"

    def test_empty_company(self):
        assert normalize_company("") == ""
        assert normalize_company(None) == ""


class TestCompanySimilarity:
    def test_identical_after_suffix_strip_scores_1(self):
        assert company_similarity("Scribd", "Scribd, Inc.") == 1.0

    def test_minor_abbreviation_scores_high(self):
        assert company_similarity("Cox Communications", "Cox Comm") >= 0.60

    def test_genuinely_different_companies_score_low(self):
        assert company_similarity("Cox Communications", "Cox Automotive") < 0.90

    def test_empty_string_scores_zero(self):
        assert company_similarity("", "Acme") == 0.0


class TestFindDuplicateCompanyPairs:
    def _seed(self, conn, company: str, title: str, **overrides):
        fields = dict(
            company=company, title=title, source_message_id=f"m-{company}-{title}", source_label="single-jd"
        )
        fields.update(overrides)
        lead = JobLead(**fields)
        upsert_lead(conn, lead)
        return lead

    def test_finds_suffix_duplicate(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Scribd", "Software Engineer")
        self._seed(conn, "Scribd, Inc.", "Data Engineer")
        pairs = find_duplicate_company_pairs(conn, threshold=0.90)
        conn.close()

        assert len(pairs) == 1
        assert {pairs[0].a_company, pairs[0].b_company} == {"Scribd", "Scribd, Inc."}
        assert pairs[0].score == 1.0
        assert len(pairs[0].a_leads) == 1
        assert len(pairs[0].b_leads) == 1

    def test_genuinely_different_companies_not_flagged(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Cox Communications", "Sr Lead Software Engineer")
        self._seed(conn, "Cox Automotive", "Software Engineer")
        pairs = find_duplicate_company_pairs(conn, threshold=0.90)
        conn.close()

        assert pairs == []

    def test_single_company_produces_no_pairs(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Acme", "Software Engineer")
        pairs = find_duplicate_company_pairs(conn, threshold=0.90)
        conn.close()

        assert pairs == []

    def test_results_sorted_best_match_first(self, tmp_path: Path):
        conn = connect(tmp_path / "leads.db")
        self._seed(conn, "Scribd", "Software Engineer")
        self._seed(conn, "Scribd, Inc.", "Data Engineer")  # exact fold match, score 1.0
        self._seed(conn, "Cox Communications", "Engineer")
        self._seed(conn, "Cox Comm", "Engineer II")  # lower-scoring match
        pairs = find_duplicate_company_pairs(conn, threshold=0.5)
        conn.close()

        assert pairs == sorted(pairs, key=lambda p: -p.score)
        assert pairs[0].score == 1.0
