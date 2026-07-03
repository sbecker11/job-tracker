#!/usr/bin/env python3
"""CLI to review or export stored job leads (var/leads.db) without re-running the pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.list_leads import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
