"""CLI for classifying recruiting-inbox messages."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from job_tracker.email.classifier import classify
from job_tracker.email.gmail_reader import fetch_message_by_id, fetch_unread
from job_tracker.email.models import EmailMessage

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_FIXTURES = _REPO_ROOT / "tests" / "fixtures"


def _load_fixture(path: Path) -> EmailMessage:
    data = json.loads(path.read_text(encoding="utf-8"))
    return EmailMessage(**data)


def _format_result(message: EmailMessage, result) -> str:
    lines = [
        f"id:         {message.id}",
        f"from:       {message.from_address}",
        f"subject:    {message.subject}",
        f"label:      {result.label.value}",
        f"confidence: {result.confidence:.2f}",
        f"reasons:    {', '.join(result.reasons) or '(none)'}",
    ]
    return "\n".join(lines)


def _emit_result(
    message: EmailMessage,
    result,
    *,
    json_output: bool,
    source: str,
    multi: bool,
) -> None:
    if json_output:
        payload = {
            "source": source,
            "message": asdict(message),
            "classification": {
                "label": result.label.value,
                "confidence": result.confidence,
                "reasons": result.reasons,
                "extracted_roles": [asdict(r) for r in result.extracted_roles],
            },
        }
        print(json.dumps(payload, indent=2))
        return

    if multi:
        print(f"--- {message.id} ---")
    print(_format_result(message, result))
    if multi:
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify recruiting-inbox email from Gmail or offline fixtures."
    )
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch unread Gmail (read-only) and classify; no messages modified",
    )
    source.add_argument(
        "--message-id",
        metavar="ID",
        help="Fetch and classify one Gmail message by ID",
    )
    source.add_argument(
        "--fixture",
        type=Path,
        help="Classify a fixture JSON file (EmailMessage fields)",
    )
    source.add_argument(
        "--all-fixtures",
        action="store_true",
        help="Classify every *.json file in --fixtures-dir",
    )

    ap.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_DEFAULT_FIXTURES,
        help=f"Directory of fixture JSON files (default: {_DEFAULT_FIXTURES})",
    )
    ap.add_argument("--limit", type=int, help="Max Gmail messages to fetch")
    ap.add_argument(
        "--query",
        default="is:unread in:inbox",
        help="Gmail search query (default: is:unread in:inbox)",
    )
    ap.add_argument(
        "--newer-than",
        type=int,
        metavar="DAYS",
        help="Only fetch mail newer than N days (appended to Gmail query)",
    )
    ap.add_argument(
        "--credentials",
        type=Path,
        help="OAuth client secrets JSON (default: credentials.json or JOB_TRACKER_GMAIL_CREDENTIALS)",
    )
    ap.add_argument(
        "--token",
        type=Path,
        help="OAuth token cache JSON (default: token.json or JOB_TRACKER_GMAIL_TOKEN)",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON output")
    args = ap.parse_args(argv)

    if args.all_fixtures:
        paths = sorted(args.fixtures_dir.glob("*.json"))
        if not paths:
            print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
            return 1
        for path in paths:
            message = _load_fixture(path)
            result = classify(message)
            _emit_result(
                message,
                result,
                json_output=args.json,
                source=str(path),
                multi=len(paths) > 1 and not args.json,
            )
        return 0

    if args.fixture:
        message = _load_fixture(args.fixture)
        result = classify(message)
        _emit_result(
            message,
            result,
            json_output=args.json,
            source=str(args.fixture),
            multi=False,
        )
        return 0

    credentials_path = args.credentials
    token_path = args.token

    if args.message_id:
        message = fetch_message_by_id(
            args.message_id,
            credentials_path=credentials_path,
            token_path=token_path,
        )
        result = classify(message)
        _emit_result(
            message,
            result,
            json_output=args.json,
            source=f"gmail:{args.message_id}",
            multi=False,
        )
        return 0

    if args.dry_run:
        print("DRY RUN: read-only Gmail fetch; no messages modified.", file=sys.stderr)
        messages = fetch_unread(
            limit=args.limit,
            query=args.query,
            newer_than_days=args.newer_than,
            credentials_path=credentials_path,
            token_path=token_path,
        )
        if not messages:
            print("No messages matched the query.", file=sys.stderr)
            return 0

        for message in messages:
            result = classify(message)
            _emit_result(
                message,
                result,
                json_output=args.json,
                source=f"gmail:{message.id}",
                multi=len(messages) > 1 and not args.json,
            )
        return 0

    ap.error("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
