#!/usr/bin/env python3
"""CLI to evaluate a stored lead and generate a tailored résumé + cover letter on a pursue verdict."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.apply_package import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
