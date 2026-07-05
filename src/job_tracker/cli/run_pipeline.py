"""CLI: run the full classify -> extract -> resolve -> score -> store pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.email.gmail_reader import KNOWN_ACCOUNTS, fetch_message_by_id, fetch_unread
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_MODEL
from job_tracker.pipeline.run import DEFAULT_DB_PATH, run_pipeline

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_FIXTURES = _REPO_ROOT / "tests" / "fixtures"


def _load_fixture(path: Path) -> EmailMessage:
    data = json.loads(path.read_text(encoding="utf-8"))
    return EmailMessage(**data)


def _print_report(summary) -> None:
    print(f"Processed {summary.total_messages} messages.")
    if summary.skipped:
        skipped_str = ", ".join(f"{k}={v}" for k, v in summary.skipped.items())
        print(f"Skipped (no lead): {skipped_str}")
    print(f"New leads stored: {summary.new_leads}  (total scored this run: {len(summary.leads)})")
    if summary.llm_fallback_used:
        print(
            f"LLM fallback invoked for {summary.llm_fallback_used} message(s) "
            f"(rescued {summary.llm_fallback_rescued} that regex extraction missed)"
        )

    if summary.pursue:
        print(f"\n=== PURSUE ({len(summary.pursue)}) ===")
        for lead in summary.pursue:
            print(f"  [{lead['match_pct']:>5.1f}%] {lead['title']} @ {lead['company']}  {lead['apply_url']}")
            for line in lead["rationale"]:
                print(f"           - {line}")

    if summary.review:
        print(f"\n=== REVIEW ({len(summary.review)}) ===")
        for lead in summary.review:
            print(f"  [{lead['match_pct']:>5.1f}%] {lead['title']} @ {lead['company']}  {lead['apply_url']}")

    if summary.outreach_needs_reply:
        print(f"\n=== RECRUITER OUTREACH — needs a reply ({len(summary.outreach_needs_reply)}) ===")
        for item in summary.outreach_needs_reply:
            print(f"  {item['subject']}  <{item['from']}>")

    if summary.needs_review:
        # A single flattened digest email (e.g. an Energy Job Line or Adzuna
        # aggregation) can surface a dozen+ unstructured listing snippets —
        # group by source message so the report stays one line per email,
        # with a few sample snippets, instead of drowning in repeats of the
        # same subject line.
        by_message: dict[str, list[dict]] = {}
        for item in summary.needs_review:
            by_message.setdefault(item["message_id"], []).append(item)

        total_snippets = len(summary.needs_review)
        print(
            f"\n=== EXTRACTION NEEDS REVIEW ({len(by_message)} messages, "
            f"{total_snippets} items) ==="
        )
        for message_id, items in by_message.items():
            subject = items[0]["subject"]
            reasons = {it["reason"] for it in items}
            print(f"  {subject}  ({', '.join(sorted(reasons))}, {len(items)} item(s))  [{message_id}]")
            samples = [it["partial"]["title"] for it in items if it.get("partial", {}).get("title")]
            for sample in samples[:3]:
                print(f"      - {sample[:100]}")
            if len(samples) > 3:
                print(f"      ... and {len(samples) - 3} more (see --json)")

    if summary.passed:
        print(f"\n=== PASS ({len(summary.passed)}) — not shown in detail, see --json for full list ===")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the full recruiting-inbox pipeline: classify, extract, "
        "resolve JD, score against CLAUDE.md framework, dedup into the leads DB."
    )
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--dry-run", action="store_true", help="Fetch unread Gmail (read-only)")
    source.add_argument("--message-id", metavar="ID", help="Fetch and process one Gmail message by ID")
    source.add_argument("--fixture", type=Path, help="Process a single fixture JSON file")
    source.add_argument("--all-fixtures", action="store_true", help="Process every fixture in --fixtures-dir")

    ap.add_argument("--fixtures-dir", type=Path, default=_DEFAULT_FIXTURES)
    ap.add_argument("--limit", type=int, help="Max Gmail messages to fetch")
    ap.add_argument("--query", default="is:unread in:inbox", help="Gmail search query")
    ap.add_argument("--newer-than", type=int, metavar="DAYS")
    ap.add_argument("--credentials", type=Path)
    ap.add_argument("--token", type=Path)
    ap.add_argument(
        "--account",
        choices=KNOWN_ACCOUNTS,
        help="Read from a named account other than the default recruiting funnel "
        "(credentials/token resolved from ~/.config/job-tracker/<account>/ unless "
        "--credentials/--token override). E.g. --account personal_hub "
        "--query 'label:Category/recruiter_job is:unread' to pick up recruiter mail "
        "comms-migration's classifier flagged on scbboston@gmail.com.",
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="Skip live ATS board lookups; score against email body/snippet text only",
    )
    ap.add_argument(
        "--llm-fallback",
        action="store_true",
        help="For messages the regex extractor can't confidently parse, ask the "
        "Anthropic API to extract roles instead (requires ANTHROPIC_API_KEY in "
        ".env; results are cached per message so repeat runs don't re-bill it)",
    )
    ap.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help=f"Anthropic model id/alias to use for --llm-fallback (default: {DEFAULT_LLM_MODEL})",
    )
    ap.add_argument("--json", action="store_true", help="Emit the full summary as JSON instead of a report")
    args = ap.parse_args(argv)

    if args.all_fixtures:
        paths = sorted(args.fixtures_dir.glob("*.json"))
        if not paths:
            print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
            return 1
        messages = [_load_fixture(p) for p in paths]
    elif args.fixture:
        messages = [_load_fixture(args.fixture)]
    elif args.message_id:
        messages = [
            fetch_message_by_id(
                args.message_id,
                credentials_path=args.credentials,
                token_path=args.token,
                account=args.account,
            )
        ]
    else:
        print("DRY RUN: read-only Gmail fetch; no messages modified.", file=sys.stderr)
        messages = fetch_unread(
            limit=args.limit,
            query=args.query,
            newer_than_days=args.newer_than,
            credentials_path=args.credentials,
            token_path=args.token,
            account=args.account,
        )
        if not messages:
            print("No messages matched the query.", file=sys.stderr)
            return 0

    summary = run_pipeline(
        messages,
        db_path=args.db,
        resolve_full_jd=not args.offline,
        use_llm_fallback=args.llm_fallback,
        llm_model=args.llm_model,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "total_messages": summary.total_messages,
                    "skipped": summary.skipped,
                    "new_leads": summary.new_leads,
                    "leads": summary.leads,
                    "outreach_needs_reply": summary.outreach_needs_reply,
                    "needs_review": summary.needs_review,
                    "llm_fallback_used": summary.llm_fallback_used,
                    "llm_fallback_rescued": summary.llm_fallback_rescued,
                },
                indent=2,
            )
        )
    else:
        _print_report(summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
