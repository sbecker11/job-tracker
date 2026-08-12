#!/usr/bin/env python3
"""Fast lead-comms cycle: scan mailboxes, refresh pending-actions, alert on new inbound.

Designed to run every few minutes via LaunchAgent (see recruiting-automation
``install_comms_fast.sh``). Deliberately skips package generation and heavy
LLM extraction fallbacks — the hourly ``run_cycle.sh`` still owns those.

Alerts (macOS notification + optional browser open) fire when this tick newly
archives an **inbound** lead conversation or parks an unmatched inbound
message that needs human follow-up.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "var" / "leads.db"
DEFAULT_STATE = REPO_ROOT / "var" / "comms_fast_state.json"
DEFAULT_LOCK = REPO_ROOT / "var" / "comms_fast.lock"
UI_URL = "http://127.0.0.1:3174/"
WORKSPACE = REPO_ROOT.parent
COMMS_REPO = WORKSPACE / "comms-migration"


@dataclass(frozen=True)
class Snapshot:
    max_conversation_id: int
    conversation_ids: frozenset[int]
    unmatched_ids: frozenset[str]


def _venv_python(repo: Path) -> Path:
    return repo / ".venv" / "bin" / "python"


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def take_snapshot(db: Path) -> Snapshot:
    conn = _connect(db)
    try:
        max_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM job_conversations").fetchone()[0])
        conv_ids = frozenset(
            int(r[0]) for r in conn.execute("SELECT id FROM job_conversations WHERE id > ?", (max(0, max_id - 5000),))
        )
        # Full set of recent unmatched ids is enough for a 3-minute tick.
        unmatched = frozenset(
            str(r[0])
            for r in conn.execute(
                "SELECT message_id FROM unmatched_messages ORDER BY rowid DESC LIMIT 200"
            )
        )
        return Snapshot(max_id, conv_ids, unmatched)
    finally:
        conn.close()


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> int:
    print(f"+ {' '.join(cmd)}", flush=True)
    try:
        completed = subprocess.run(cmd, cwd=str(cwd), timeout=timeout, check=False)
        return int(completed.returncode)
    except subprocess.TimeoutExpired:
        print(f"! timed out after {timeout}s: {' '.join(cmd)}", file=sys.stderr, flush=True)
        return 124


def run_steps(*, db: Path, skip_classify: bool) -> list[str]:
    """Run cheap mailbox sweeps. Returns human-readable step error notes."""
    errors: list[str] = []
    jt_py = str(_venv_python(REPO_ROOT))
    comms_py = str(_venv_python(COMMS_REPO))

    def note(rc: int, label: str) -> None:
        if rc != 0:
            errors.append(f"{label} exited {rc}")

    if not skip_classify and COMMS_REPO.is_dir() and Path(comms_py).is_file():
        # Rules + LLM for brand-new senders on personal hub (Abhinav-class).
        # Tight newer-than / limit so a 3-minute tick cannot chew the backlog.
        note(
            _run(
                [
                    comms_py,
                    "scripts/run_classifier.py",
                    "--account",
                    "personal_hub",
                    "--newer-than",
                    "1",
                    "--limit",
                    "25",
                    "--no-rule-telemetry",
                ],
                cwd=COMMS_REPO,
                timeout=120,
            ),
            "classify personal_hub",
        )

    for account_args, label in (
        ([], "scan recruiting"),
        (["--account", "personal_hub"], "scan personal_hub"),
    ):
        note(
            _run(
                [
                    jt_py,
                    "scripts/scan_communications.py",
                    "--db",
                    str(db),
                    "--include-sent",
                    "--newer-than",
                    "1",
                    "--limit",
                    "40",
                    *account_args,
                ],
                cwd=REPO_ROOT,
                timeout=120,
            ),
            label,
        )

    for account_args, label in (
        ([], "triage recruiting"),
        (["--account", "personal_hub"], "triage personal_hub"),
    ):
        note(
            _run(
                [
                    jt_py,
                    "scripts/triage_recruiter_inbox.py",
                    "--db",
                    str(db),
                    "--newer-than",
                    "1",
                    "--inbox-batch-message-cap",
                    "12",
                    "--inbox-batch-wall-budget-secs",
                    "45",
                    "--no-generate",
                    "--offline",
                    *account_args,
                ],
                cwd=REPO_ROOT,
                timeout=90,
            ),
            label,
        )

    note(
        _run(
            [
                jt_py,
                "scripts/triage_imap_inbox.py",
                "--db",
                str(db),
                "--imap-prefix",
                "SPEXTURE",
                "--inbox-batch-message-cap",
                "12",
                "--inbox-batch-wall-budget-secs",
                "45",
                "--no-generate",
                "--offline",
            ],
            cwd=REPO_ROOT,
            timeout=90,
        ),
        "triage spexture imap",
    )

    note(
        _run(
            [
                jt_py,
                "scripts/render_pending_actions.py",
                "--db",
                str(db),
                "--no-rescore",
            ],
            cwd=REPO_ROOT,
            timeout=60,
        ),
        "render pending-actions",
    )
    return errors


def collect_alerts(db: Path, before: Snapshot) -> list[dict]:
    conn = _connect(db)
    alerts: list[dict] = []
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.job_key, c.direction, c.summary, c.occurred_at, c.message_id,
                   l.company, l.title
            FROM job_conversations c
            LEFT JOIN job_leads l ON l.normalized_key = c.job_key
            WHERE c.id > ?
              AND lower(c.direction) = 'inbound'
            ORDER BY c.id ASC
            """,
            (before.max_conversation_id,),
        ).fetchall()
        for r in rows:
            company = r["company"] or r["job_key"]
            title = r["title"] or ""
            summary = (r["summary"] or "").strip() or "(no summary)"
            alerts.append(
                {
                    "kind": "inbound",
                    "title": f"Lead reply: {company}",
                    "body": f"{title} — {summary}"[:180],
                    "job_key": r["job_key"],
                }
            )

        unmatched = conn.execute(
            """
            SELECT message_id, from_address, subject
            FROM unmatched_messages
            ORDER BY rowid DESC
            LIMIT 200
            """
        ).fetchall()
        for r in unmatched:
            mid = str(r["message_id"])
            if mid in before.unmatched_ids:
                continue
            subj = (r["subject"] or "").strip() or "(no subject)"
            frm = (r["from_address"] or "").strip() or "unknown"
            alerts.append(
                {
                    "kind": "unmatched",
                    "title": "Unmatched recruiting mail",
                    "body": f"{frm} — {subj}"[:180],
                    "job_key": None,
                }
            )
    finally:
        conn.close()
    return alerts


def notify_macos(title: str, body: str) -> None:
    # AppleScript string escaping
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" with title "{esc(title)}" '
        f'subtitle "job-tracker" sound name "Glass"'
    )
    subprocess.run(["osascript", "-e", script], check=False)
    glass = Path("/System/Library/Sounds/Glass.aiff")
    if glass.is_file():
        subprocess.run(["afplay", str(glass)], check=False)


def open_ui() -> None:
    subprocess.run(["open", UI_URL], check=False)


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def acquire_lock(lock_path: Path):
    """Non-blocking exclusive lock (macOS has no flock(1); use fcntl)."""
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--skip-classify", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="Notify only; do not open the React UI")
    ap.add_argument("--dry-run-notify", action="store_true", help="Print alerts instead of notifying")
    ap.add_argument(
        "--wait-lock-seconds",
        type=float,
        default=0.0,
        help="If another tick holds the lock, wait up to N seconds before giving up "
        "(UI 'Reply sent' uses this; launchd keeps the default of 0 = skip immediately).",
    )
    args = ap.parse_args(argv)

    if not args.db.is_file():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    lock_fh = None
    deadline = time.monotonic() + max(0.0, float(args.wait_lock_seconds))
    while True:
        lock_fh = acquire_lock(args.lock)
        if lock_fh is not None:
            break
        if time.monotonic() >= deadline:
            print("comms_fast: previous tick still running — skip", flush=True)
            # Non-zero so the Reply-sent helper can surface a clear failure
            # instead of the UI spinning until generatedAt never changes.
            return 75
        time.sleep(0.5)

    try:
        before = take_snapshot(args.db)
        errors = run_steps(db=args.db, skip_classify=args.skip_classify)
        alerts = collect_alerts(args.db, before)

        if alerts:
            print(f"Alerts: {len(alerts)}", flush=True)
            for a in alerts:
                print(f"  [{a['kind']}] {a['title']}: {a['body']}", flush=True)
                if args.dry_run_notify:
                    continue
                notify_macos(a["title"], a["body"])
            if not args.dry_run_notify and not args.no_open:
                open_ui()
        else:
            print("No new inbound lead communications.", flush=True)

        state = load_state(args.state)
        state.update(
            {
                "last_max_conversation_id": take_snapshot(args.db).max_conversation_id,
                "last_alert_count": len(alerts),
                "last_errors": errors,
            }
        )
        save_state(args.state, state)

        if errors:
            print("Step errors: " + "; ".join(errors), file=sys.stderr, flush=True)
            # Still exit 0 so launchd interval does not thrash; errors are logged.
        return 0
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
