#!/usr/bin/env python3
"""Check every Pending Actions card for known-bad states (see card_audit.CHECKS).

Reads the generated `var/pending-actions.json` plus the leads DB (read-only)
and prints one line per problem. Also runs automatically at the end of
`render_pending_actions.py`. Exit code 1 when problems are found.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_tracker.pipeline.card_audit import audit_cards, issues_json, summarize  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, default=ROOT / "var" / "pending-actions.json")
    ap.add_argument("--db", type=Path, default=ROOT / "var" / "leads.db")
    ap.add_argument("--output", type=Path, help="Also write the problems as JSON here")
    args = ap.parse_args(argv)

    payload = json.loads(args.json.read_text(encoding="utf-8"))
    conn = None
    if args.db.exists():
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    try:
        issues = audit_cards(payload, conn)
    finally:
        if conn is not None:
            conn.close()
    print(summarize(issues))
    if args.output:
        args.output.write_text(json.dumps(issues_json(issues), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
