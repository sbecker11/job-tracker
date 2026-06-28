"""Tests for ATS JD resolver."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_selftest_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "resolve_jd.py"), "--selftest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASS" in result.stdout
