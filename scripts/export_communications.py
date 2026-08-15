"""CLI to render one job's communications history to an ODT, on demand."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_tracker.cli.export_communications import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
