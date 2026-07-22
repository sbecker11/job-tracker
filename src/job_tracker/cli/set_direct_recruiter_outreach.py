"""CLI: one-shot setter for a single lead's `direct_recruiter_outreach`.

Exists for the dashboard's inline tri-state selector (`render_pending_
actions.py`'s `directRecruiterCellHtml()`) — a browser can't write to
sqlite directly, so the page fires a `setdro://` custom URL scheme
(`tools/set-direct-recruiter-outreach/`), whose tiny helper app just shells
out to this CLI. For interactive, walk-the-whole-queue review instead, use
`review-direct-recruiter-outreach`.

Deliberately narrow: one lead, one value, no prompts — a non-zero exit
with a message on stderr is all the caller (the Swift helper) needs to
show a native alert on failure.

Usage:
    set-direct-recruiter-outreach --key <normalized_key> --value yes
    set-direct-recruiter-outreach --key <normalized_key> --value no
    set-direct-recruiter-outreach --key <normalized_key> --value undecided
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, set_direct_recruiter_outreach

_VALUES = {"yes": True, "no": False, "undecided": None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", required=True, metavar="NORMALIZED_KEY")
    ap.add_argument("--value", required=True, choices=sorted(_VALUES))
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        row = conn.execute("SELECT 1 FROM job_leads WHERE normalized_key = ?", (args.key,)).fetchone()
        if row is None:
            print(f"Lead not found: {args.key!r}", file=sys.stderr)
            return 1
        set_direct_recruiter_outreach(conn, args.key, _VALUES[args.value])
        print(f"Set direct_recruiter_outreach={args.value!r} for {args.key!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
