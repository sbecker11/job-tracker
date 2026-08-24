"""Phase 5 — side-by-side offer comparison for jobs in offered status.

  python scripts/offer_comparison.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        rows = conn.execute(
            """
            SELECT j.company, j.title, j.llm_match_pct, j.status,
                   o.base_salary, o.bonus, o.equity, o.benefits_notes,
                   o.deadline, o.received_at, o.decision
            FROM job_offers o
            JOIN job_leads j ON j.normalized_key = o.job_key
            ORDER BY o.received_at DESC
            """
        ).fetchall()
        offers = [dict(r) for r in rows]
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"offers": offers}, indent=2, default=str))
        return 0

    print("=== Offer comparison (Phase 5) ===")
    if not offers:
        print("  (no job_offers rows — add offers via CRM tooling or attach_document)")
        return 0
    for o in offers:
        match = o.get("llm_match_pct")
        match_s = f"{float(match):.0f}%" if match is not None else "—"
        print(
            f"  • {o['company']} — {o['title']}  [match {match_s} · {o.get('decision')}]"
            f"\n      base={o.get('base_salary')} bonus={o.get('bonus')} equity={o.get('equity')}"
            f"\n      deadline={o.get('deadline')} received={o.get('received_at')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
