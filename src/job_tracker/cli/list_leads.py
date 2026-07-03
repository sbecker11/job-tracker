"""CLI: review or export stored job leads without re-running the pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_leads

_COLUMNS = [
    "company",
    "title",
    "match_pct",
    "verdict",
    "status",
    "apply_url",
    "jd_resolved",
    "times_seen",
    "first_seen",
    "last_seen",
]


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["matched_skills"] = json.loads(d.get("matched_skills") or "[]")
    d["rationale"] = json.loads(d.get("rationale") or "[]")
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review or export stored job leads.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--verdict", choices=["pursue", "review", "pass"], help="Filter by verdict")
    ap.add_argument("--status", help="Filter by status (new, pursuing, passed, applied)")
    ap.add_argument("--csv", type=Path, help="Write results to this CSV path instead of printing")
    ap.add_argument("--json", action="store_true", help="Print full JSON (including rationale) instead of a table")
    ap.add_argument("--set-status", help="Set status for all matching rows (e.g. --verdict pursue --set-status pursuing)")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db} — run scripts/run_pipeline.py first.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    rows = [_row_to_dict(r) for r in list_leads(conn, verdict=args.verdict)]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]

    if args.set_status:
        for r in rows:
            conn.execute(
                "UPDATE job_leads SET status = ? WHERE normalized_key = ?",
                (args.set_status, r["normalized_key"]),
            )
        conn.commit()
        print(f"Updated {len(rows)} row(s) to status={args.set_status}")
        conn.close()
        return 0

    conn.close()

    if not rows:
        print("No matching leads.")
        return 0

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k) for k in _COLUMNS})
        print(f"Wrote {len(rows)} leads to {args.csv}")
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    header = f"{'MATCH%':>7}  {'VERDICT':<8} {'STATUS':<9} {'COMPANY':<24} {'TITLE':<40}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['match_pct']:>6.1f}%  {r['verdict']:<8} {r['status']:<9} "
            f"{r['company'][:24]:<24} {r['title'][:40]:<40}"
        )
    print(f"\n{len(rows)} lead(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
