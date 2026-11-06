#!/usr/bin/env python3
"""Put one lead at the top of Pending Actions without rebuilding the other cards.

    python scripts/refresh_one_lead.py --company "E-solutions INC" --title "AI Engineer"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.lead_card_cache import refresh_named_lead  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_JSON = _REPO_ROOT / "var" / "pending-actions.json"
DEFAULT_UI_JSON = _REPO_ROOT / "pending-actions-ui" / "public" / "pending-actions.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--ui-json", type=Path, default=DEFAULT_UI_JSON)
    args = ap.parse_args(argv)
    if not args.json.is_file():
        print(f"No cached page at {args.json}. Run a full render once first.", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        card = refresh_named_lead(
            conn,
            company=args.company,
            title=args.title,
            payload_path=args.json,
            ui_path=args.ui_json,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(
        f"Card 1: {card.get('title')} @ {card.get('company')} "
        f"({card.get('recruiterName')}) — other cards left cached."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
