#!/usr/bin/env python3
"""CLI to print (and optionally rewrite) the deterministic no-LLM review for one lead.

Usage:
    python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer"
    python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer" --json
    python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer" --write
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.no_llm_review import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
