#!/usr/bin/env python3
"""CLI for resolving job descriptions from public ATS board APIs."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python scripts/resolve_jd.py` without install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.ats.jd_resolver import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
