#!/usr/bin/env python3
"""Soft-delete (or hard-purge) a job lead — see job_tracker.cli.delete_lead."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.delete_lead import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
