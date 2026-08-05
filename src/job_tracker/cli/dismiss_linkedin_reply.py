"""CLI: dismiss a LinkedIn reply card from pending-actions.

The static dashboard cannot write sqlite itself, so the page fires
`dlr://dismiss?...` (`tools/dismiss-linkedin-reply/`), whose helper app
shells out to this CLI.

Usage:
    dismiss-linkedin-reply --kind lead --key <normalized_key> [--message-id <id>]
    dismiss-linkedin-reply --kind unmatched --message-id <id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, dismiss_linkedin_reply


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--kind", required=True, choices=("lead", "unmatched"))
    ap.add_argument("--key", default="", metavar="NORMALIZED_KEY", help="Required for kind=lead")
    ap.add_argument("--message-id", default="", dest="message_id", help="Required for kind=unmatched; optional for lead")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        result = dismiss_linkedin_reply(
            conn,
            kind=args.kind,
            message_id=args.message_id,
            normalized_key=args.key,
        )
        print(f"Dismissed {result}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
