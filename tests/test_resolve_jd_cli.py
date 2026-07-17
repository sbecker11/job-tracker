"""Coverage for job_tracker.cli.resolve_jd re-export."""

from __future__ import annotations

from job_tracker.cli import resolve_jd
from job_tracker.ats import jd_resolver


def test_resolve_jd_reexports_main():
    assert resolve_jd.main is jd_resolver.main
    assert "main" in resolve_jd.__all__
