#!/usr/bin/env python3
"""CLI to re-sync JobTracker/PURSUE|SKIP|NEEDS_REVIEW Gmail labels to each message's linked lead(s)' current verdict."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.resync_labels import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
