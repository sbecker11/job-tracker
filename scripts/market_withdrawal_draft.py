#!/usr/bin/env python3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
from job_tracker.cli.market_withdrawal_draft import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
