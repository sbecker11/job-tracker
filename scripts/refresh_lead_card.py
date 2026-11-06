#!/usr/bin/env python3
"""Update one lead's card on Pending Actions after a status change or logged reply.

    python scripts/refresh_lead_card.py --key "<normalized_key>" [--message-id <id>]

Started in the background by set-lead-status, decline-lead and
record-unmatched-reply. Falls back to a full render when the lead now
belongs in a stage this can't build on its own.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline import db_lock  # noqa: E402
from job_tracker.pipeline.card_patch import DEFAULT_JSON, DEFAULT_UI_JSON, NeedsFullRender, refresh  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402


def _full_render(db: Path) -> int:
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "render_pending_actions.py"), "--db", str(db), "--no-rescore"],
        cwd=_REPO_ROOT,
        check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--key", required=True)
    ap.add_argument("--message-id", default="")
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--ui-json", type=Path, default=DEFAULT_UI_JSON)
    ap.add_argument("--lock", type=Path, default=db_lock.DEFAULT_LOCK_PATH)
    ap.add_argument("--wait-lock-seconds", type=float, default=600.0)
    args = ap.parse_args(argv)

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lock_fh = db_lock.acquire(args.lock, wait_seconds=args.wait_lock_seconds)
    if lock_fh is None:
        print(f"[{stamp}] {args.key}: lock busy, left to the next cycle", flush=True)
        return db_lock.LOCK_BUSY_EXIT_CODE
    try:
        if not args.json.is_file():
            print(f"[{stamp}] {args.key}: no cached page, full render", flush=True)
            return _full_render(args.db)
        conn = connect(args.db)
        try:
            result = refresh(conn, args.key, message_id=args.message_id, json_path=args.json, ui_path=args.ui_json)
        except NeedsFullRender as exc:
            print(f"[{stamp}] {args.key}: {exc}; full render", flush=True)
            return _full_render(args.db)
        finally:
            conn.close()
        print(f"[{stamp}] {args.key}: {result}", flush=True)
        return 0
    finally:
        db_lock.release(lock_fh)


if __name__ == "__main__":
    raise SystemExit(main())
