#!/usr/bin/env python3
"""Point stored LinkedIn copies at one lead.

A paste and the Gmail notification of the same pitch can already both be
in the database. This pairs them: the pasted lead is kept, the other lead
is skipped as its duplicate, and both thread histories are kept under
that lead's company and title communications folder. Parked mail of the
same pitch is resolved onto that lead as well.

    python3 scripts/link_same_pitches.py           # apply
    python3 scripts/link_same_pitches.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
if (
    __name__ == "__main__"
    and _VENV_PYTHON.is_file()
    and Path(sys.executable).resolve() != _VENV_PYTHON.resolve()
):
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])

_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.pitch_match import link_historic_same_pitches  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    try:
        actions = link_historic_same_pitches(conn, apply=not args.dry_run)
    finally:
        conn.close()
    if not actions:
        print("No stored copies of the same pitch.")
        return 0
    for item in actions:
        print(item)
    print(f"{len(actions)} link(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
