"""Phase 4 — weekly spend / pursue rollup from stored LLM cost fields, plus
real spend per month from the workspace call log (`llm_call_log`), which
covers every API call from job-tracker and comms-migration.

  python scripts/spend_report.py
  python scripts/spend_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.pipeline.llm_call_log import log_path
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect

BILLING_URL = "https://platform.claude.com/settings/billing"

def build_spend_summary(conn) -> dict:
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(llm_eval_cost_usd), 0) AS total_eval_usd,
          SUM(CASE WHEN llm_verdict = 'pursue' OR (llm_verdict IS NULL AND verdict = 'pursue') THEN 1 ELSE 0 END) AS pursue_leads,
          SUM(CASE WHEN status = 'package_generated' THEN 1 ELSE 0 END) AS packages_generated,
          SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_leads
        FROM job_leads
        WHERE status != 'deleted'
        """
    ).fetchone()
    cache_rows = conn.execute("SELECT COUNT(*) AS n FROM llm_extraction_cache").fetchone()["n"]
    return {
        "totalEvalCostUsd": round(float(row["total_eval_usd"] or 0), 4),
        "pursueLeads": int(row["pursue_leads"] or 0),
        "packagesGenerated": int(row["packages_generated"] or 0),
        "rejectedLeads": int(row["rejected_leads"] or 0),
        "llmExtractionCacheRows": int(cache_rows or 0),
        "costPerPursueUsd": round(
            float(row["total_eval_usd"] or 0) / max(int(row["pursue_leads"] or 0), 1), 4
        ),
    }


def monthly_call_spend(path: Path | None = None) -> dict[str, dict[str, dict[str, float]]]:
    """{"2026-10": {"comms-migration": {"calls": 12, "costUsd": 0.03}, ...}, ...}"""
    out: dict[str, dict[str, dict[str, float]]] = {}
    p = path or log_path()
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        month = str(entry.get("ts", ""))[:7]
        if not month:
            continue
        bucket = out.setdefault(month, {}).setdefault(str(entry.get("source", "?")), {"calls": 0, "costUsd": 0.0})
        bucket["calls"] += 1
        bucket["costUsd"] = round(bucket["costUsd"] + float(entry.get("cost_usd") or 0), 6)
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        summary = build_spend_summary(conn)
    finally:
        conn.close()
    summary["monthlyCallSpend"] = monthly_call_spend()

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("=== Spend summary (Phase 4) ===")
    print(f"  Total LLM eval cost (stored):  ${summary['totalEvalCostUsd']:.4f}")
    print(f"  Pursue leads:                  {summary['pursueLeads']}")
    print(f"  Cost / pursue (approx):        ${summary['costPerPursueUsd']:.4f}")
    print(f"  Packages generated:            {summary['packagesGenerated']}")
    print(f"  Rejected leads:                {summary['rejectedLeads']}")
    print(f"  LLM extraction cache rows:     {summary['llmExtractionCacheRows']} (short-circuit hits)")
    print(f"\n=== API spend by month (all calls, {log_path()}) ===")
    if not summary["monthlyCallSpend"]:
        print("  (no calls logged yet)")
    for month, sources in summary["monthlyCallSpend"].items():
        total = sum(b["costUsd"] for b in sources.values())
        detail = ", ".join(f"{src} {int(b['calls'])} calls ${b['costUsd']:.2f}" for src, b in sorted(sources.items()))
        print(f"  {month}: ${total:.2f}  ({detail})")
    print(f"  Billed totals: {BILLING_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
