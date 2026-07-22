#!/usr/bin/env python3
"""CLI to triage a plain-IMAP mailbox (e.g. shawn.becker@spexture.com): LLM-score, auto-generate on 'pursue', file into IMAP folders."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.triage_imap_inbox import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
