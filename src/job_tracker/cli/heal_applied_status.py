"""CLI: heal Ready-to-apply CRM lag (applied_at without status, same-URL twins).

Companion to the live guards in `store.advance_status` (forward-only) and
`scripts/render_pending_actions.py` (Ready exclusion). Safe to re-run —
idempotent once lags are cleared.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, heal_applied_crm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Heal applied_at/status lag and same-apply-URL title twins "
        "so already-submitted postings leave Ready to apply."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change; never write to the DB",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        actions = heal_applied_crm(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"count": len(actions), "actions": actions}, indent=2))
    else:
        mode = "would heal" if args.dry_run else "healed"
        print(f"{mode}: {len(actions)} lead(s)")
        for a in actions:
            print(f"  - {a['company']} / {a['title']}: {a['reason']}")
    return 0
