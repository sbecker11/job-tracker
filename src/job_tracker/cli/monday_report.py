"""Monday v1 — zero dead-time decision report.

Aggregates the queues that block "receive lead → decide/act" so Shawn can
clear clarify / send / wait / decide without living in Gmail.

  python scripts/monday_report.py
  monday-report                 # console script after pip install -e .

Objective lens: surface highest interview-likelihood work first
(direct-recruiter outreach, reply-due, high match, packages ready).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    list_leads,
    list_leads_awaiting_full_llm_review,
    list_unmatched_messages,
)
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework

_DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "Resumes" / "2026"


def _age_days(iso: str | None, now: datetime) -> int:
    if not iso:
        return 0
    try:
        seen = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=now.tzinfo or timezone.utc)
    return max(0, (now - seen).days)


def _has_resume_and_cover(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    names = [p.name.lower() for p in folder.glob("*.docx")]
    return any("resume" in n for n in names) and any("cover" in n for n in names)


def _package_folder(output_root: Path, company: str, title: str) -> Path:
    # Matches llm_apply folder convention: Company/Title/
    def _safe(s: str) -> str:
        return "".join(c if c.isalnum() or c in " ._-" else "_" for c in (s or "")).strip() or "_"

    return output_root / _safe(company) / _safe(title)


def _schedule_health(state_dir: Path, *, now: datetime) -> dict:
    halt_path = state_dir / "HALT"
    last_ok_path = state_dir / "last_ok_cycle"
    halted = halt_path.is_file()
    halt_reason = halt_path.read_text(encoding="utf-8").strip() if halted else ""
    hours_since_ok: float | None = None
    if last_ok_path.is_file():
        try:
            last_ok_epoch = int(last_ok_path.read_text(encoding="utf-8").strip())
            last_ok_dt = datetime.fromtimestamp(last_ok_epoch, tz=timezone.utc).astimezone()
            hours_since_ok = max(0.0, (now.astimezone() - last_ok_dt).total_seconds() / 3600.0)
        except ValueError:
            hours_since_ok = None
    if halted:
        level, summary = "danger", f"HALTED — {halt_reason or '(no reason)'}"
    elif hours_since_ok is None:
        level, summary = "info", "No last_ok_cycle yet"
    elif hours_since_ok >= 3:
        level, summary = "warning", f"No OK cycle in {hours_since_ok:.0f}h (pipeline may be behind)"
    else:
        level, summary = "ok", f"Last OK cycle {hours_since_ok:.1f}h ago"
    return {
        "level": level,
        "summary": summary,
        "halted": halted,
        "haltReason": halt_reason,
        "hoursSinceOk": None if hours_since_ok is None else round(hours_since_ok, 1),
        "stateDir": str(state_dir),
    }


def _interview_likelihood_score(row: dict) -> float:
    """Higher = more likely to yield an interview if acted on now.

    Heuristic only (not ML): direct recruiter + high LLM/keyword match + freshness.
    """
    score = 0.0
    dro = row.get("direct_recruiter_outreach")
    if dro == 1 or dro is True:
        score += 40.0
    elif dro is None:
        score += 10.0  # undecided — still worth a look
    match = float(row.get("llm_match_pct") or row.get("match_pct") or 0.0)
    score += min(match, 100.0) * 0.45
    age = int(row.get("age_days") or 0)
    score += max(0.0, 15.0 - age)  # fresher is better
    if row.get("awaiting_response_since"):
        score += 5.0  # ball in their court but keep warm
    return round(score, 1)


def build_monday_snapshot(
    *,
    db_path: Path,
    output_root: Path,
    state_dir: Path,
    now: datetime | None = None,
    top_n: int = 8,
) -> dict:
    now = now or datetime.now(timezone.utc).astimezone()
    fw = load_framework(DEFAULT_FRAMEWORK_PATH)
    gate = float((fw.get("thresholds") or {}).get("llm_review_min_pct") or 70.0)

    conn = connect(db_path)
    try:
        unmatched = [dict(r) for r in list_unmatched_messages(conn)]
        awaiting_llm = [dict(r) for r in list_leads_awaiting_full_llm_review(conn, gate)]

        waiting = []
        packages_ready = []
        needs_decision = []
        candidates = []

        for r in list_leads(conn):
            d = dict(r)
            status = (d.get("status") or "").strip()
            if status in ("deleted", "unavailable", "hired", "skipped", "rejected"):
                continue
            d["age_days"] = _age_days(d.get("first_seen"), now)
            d["likelihood"] = _interview_likelihood_score(d)

            if d.get("awaiting_response_since"):
                waiting.append(d)

            if status == "package_generated":
                folder = _package_folder(output_root, d.get("company") or "", d.get("title") or "")
                if _has_resume_and_cover(folder):
                    d["package_folder"] = str(folder)
                    packages_ready.append(d)

            llm_v = (d.get("llm_verdict") or "").strip().lower()
            if status in ("new", "pursued") and llm_v in ("review", "pursue"):
                folder = _package_folder(output_root, d.get("company") or "", d.get("title") or "")
                if llm_v == "review" or not _has_resume_and_cover(folder):
                    needs_decision.append(d)

            if status in ("new", "pursued", "package_generated", "applied", "following_up", "interviewing"):
                candidates.append(d)

        def _enrich(rows: list) -> list[dict]:
            out = []
            for r in rows:
                d = dict(r) if not isinstance(r, dict) else dict(r)
                d["age_days"] = _age_days(d.get("first_seen"), now)
                d["likelihood"] = _interview_likelihood_score(d)
                out.append(d)
            return _sort_likelihood(out)

        def _sort_likelihood(rows: list[dict]) -> list[dict]:
            return sorted(
                rows,
                key=lambda x: (-float(x.get("likelihood") or 0), -int(x.get("age_days") or 0)),
            )

        waiting = _sort_likelihood(waiting)
        packages_ready = _sort_likelihood(packages_ready)
        needs_decision = _sort_likelihood(needs_decision)
        awaiting_llm = _enrich(awaiting_llm)
        top = _sort_likelihood(candidates)[:top_n]

        return {
            "generatedAt": now.isoformat(),
            "objective": (
                "Minimize dead-time between lead arrival and decision; "
                "prioritize leads most likely to yield interviews / hires."
            ),
            "schedule": _schedule_health(state_dir, now=now),
            "counts": {
                "unmatchedCommunications": len(unmatched),
                "awaitingLlmReview": len(awaiting_llm),
                "packagesReady": len(packages_ready),
                "waitingOnThem": len(waiting),
                "needsDecision": len(needs_decision),
            },
            "unmatched": unmatched[:top_n],
            "awaitingLlmReview": awaiting_llm[:top_n],
            "packagesReady": packages_ready[:top_n],
            "waitingOnThem": waiting[:top_n],
            "needsDecision": needs_decision[:top_n],
            "topInterviewLikelihood": top,
            "llmReviewGatePct": gate,
            "uiUrl": "http://127.0.0.1:3174/",
            "nextCommands": _next_commands(
                unmatched=len(unmatched),
                awaiting=len(awaiting_llm),
                packages=len(packages_ready),
                halted=_schedule_health(state_dir, now=now)["halted"],
            ),
        }
    finally:
        conn.close()


def _next_commands(*, unmatched: int, awaiting: int, packages: int, halted: bool) -> list[str]:
    cmds: list[str] = []
    if halted:
        cmds.append("cd ../recruiting-automation && ./install.sh   # clear HALT / restart schedule")
    if unmatched:
        cmds.append("python scripts/resolve_communication.py --list   # clarify unmatched InMails")
    if awaiting:
        cmds.append("python scripts/process_awaiting_llm_review.py --limit 5   # finish scoring high-gate leads")
    if packages:
        cmds.append("open http://127.0.0.1:3174/   # Send résumé / Decide-apply stages")
    cmds.append("python scripts/resync_labels.py --dry-run   # label↔DB trust check")
    cmds.append("python scripts/render_pending_actions.py --no-rescore   # refresh UI JSON")
    return cmds


def _fmt_lead(d: dict) -> str:
    company = d.get("company") or "?"
    title = d.get("title") or "?"
    match = d.get("llm_match_pct") if d.get("llm_match_pct") is not None else d.get("match_pct")
    match_s = f"{float(match):.0f}%" if match is not None else "—"
    age = d.get("age_days", _age_days(d.get("first_seen"), datetime.now(timezone.utc).astimezone()))
    lik = d.get("likelihood")
    dro = d.get("direct_recruiter_outreach")
    dro_s = "★direct" if dro in (1, True) else ("·no" if dro in (0, False) else "?dro")
    lik_s = f" L={lik}" if lik is not None else ""
    return f"  • {company} — {title}  [{match_s} · {age}d · {dro_s}{lik_s}]"


def _fmt_unmatched(d: dict) -> str:
    subj = (d.get("subject") or "")[:70]
    fr = (d.get("from_address") or "")[:40]
    return f"  • {fr} | {subj}"


def render_text(snap: dict) -> str:
    lines: list[str] = []
    c = snap["counts"]
    sch = snap["schedule"]
    lines.append("=== MONDAY v1 — decide now ===")
    lines.append(snap["objective"])
    lines.append("")
    lines.append(f"Schedule: [{sch['level']}] {sch['summary']}")
    lines.append("")
    lines.append("Queue counts (act top-down for least dead-time):")
    lines.append(f"  1. Unmatched / clarify     {c['unmatchedCommunications']}")
    lines.append(f"  2. Awaiting LLM review     {c['awaitingLlmReview']}  (gate ≥ {snap['llmReviewGatePct']:.0f}%)")
    lines.append(f"  3. Packages ready to send  {c['packagesReady']}")
    lines.append(f"  4. Waiting on them         {c['waitingOnThem']}")
    lines.append(f"  5. Needs your decision     {c['needsDecision']}")
    lines.append("")

    def _section(title: str, rows: list, fmt) -> None:
        lines.append(f"--- {title} ({len(rows)} shown) ---")
        if not rows:
            lines.append("  (none)")
        else:
            for r in rows:
                lines.append(fmt(r))
        lines.append("")

    _section("Unmatched communications", snap["unmatched"], _fmt_unmatched)
    _section("Awaiting LLM review", snap["awaitingLlmReview"], _fmt_lead)
    _section("Packages ready", snap["packagesReady"], _fmt_lead)
    _section("Waiting on them", snap["waitingOnThem"], _fmt_lead)
    _section("Needs decision", snap["needsDecision"], _fmt_lead)
    _section("Top interview-likelihood", snap["topInterviewLikelihood"], _fmt_lead)

    lines.append("--- Next commands ---")
    for cmd in snap["nextCommands"]:
        lines.append(f"  $ {cmd}")
    lines.append("")
    lines.append(f"Pending-actions UI: {snap['uiUrl']}")
    lines.append(f"Generated: {snap['generatedAt']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="recruiting-automation state/ dir (HALT, last_ok_cycle)",
    )
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args(argv)

    workspace = Path(
        __import__("os").environ.get(
            "RECRUITING_AUTOMATION_WORKSPACE_ROOT",
            str(Path.home() / "workspace-recruiting-automation"),
        )
    )
    state_dir = args.state_dir or (workspace / "recruiting-automation" / "state")

    snap = build_monday_snapshot(
        db_path=args.db,
        output_root=args.output_root,
        state_dir=state_dir,
        top_n=args.top,
    )
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render_text(snap))
    # Non-zero if schedule halted or clarify backlog is large (useful for automation)
    if snap["schedule"]["halted"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
