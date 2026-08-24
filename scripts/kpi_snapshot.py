#!/usr/bin/env python3
"""Emit decision-queue KPI JSON for status.sh / run_cycle.sh."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.cli.monday_report import build_kpi_counts  # noqa: E402
from job_tracker.cli.verify_framework_sync import verify_framework_sync  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH  # noqa: E402


def _label_drift_count() -> int | None:
    """Parse `resync_labels.py --dry-run` summary; None if unavailable."""
    script = _REPO_ROOT / "scripts" / "resync_labels.py"
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r":\s*(\d+)\s+would need relabeling", text)
    if m:
        return int(m.group(1))
    if proc.returncode != 0 and "No triaged messages" not in text:
        return None
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--check-label-drift", action="store_true")
    ap.add_argument("--check-framework", action="store_true")
    ap.add_argument("--claude-md", type=Path, default=None)
    args = ap.parse_args(argv)

    payload: dict = {"kpis": build_kpi_counts(db_path=args.db, state_dir=args.state_dir)}

    if args.check_label_drift:
        payload["labelDriftWouldRelabel"] = _label_drift_count()

    if args.check_framework:
        claude = args.claude_md or Path.home() / "CLAUDE.md"
        payload["frameworkSync"] = verify_framework_sync(claude_path=claude)

    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
