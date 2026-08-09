"""CLI: mark a lead's résumé package as sent (moves Contact priority → Wait).

The React pending-actions UI cannot write sqlite itself, so it fires
`mps://mark?key=<normalized_key>` (`tools/mark-package-sent/`), whose helper
app shells out to this CLI.

Usage:
    mark-package-sent --key <normalized_key> [--channel email|linkedin]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, mark_package_sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--key", required=True, metavar="NORMALIZED_KEY")
    ap.add_argument(
        "--channel",
        default="email",
        choices=("email", "linkedin", "other", "call"),
        help="Outbound channel logged on the conversation row (default: email)",
    )
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        result = mark_package_sent(conn, args.key, channel=args.channel)
        print(f"Marked {result}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
