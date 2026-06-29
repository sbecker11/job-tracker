"""CLI for classifying recruiting-inbox messages (fixture mode until Gmail reader lands)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from job_tracker.email.classifier import classify
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Classify recruiting-inbox email (fixture mode; Gmail fetch in next commit)."
    )
    ap.add_argument(
        "--fixture",
        type=Path,
        help="Path to a fixture JSON file (EmailMessage fields)",
    )
    ap.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_DEFAULT_FIXTURES,
        help=f"Directory of fixture JSON files (default: {_DEFAULT_FIXTURES})",
    )
    ap.add_argument(
        "--all-fixtures",
        action="store_true",
        help="Classify every *.json file in --fixtures-dir",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON output")
    args = ap.parse_args(argv)

    if args.all_fixtures:
        paths = sorted(args.fixtures_dir.glob("*.json"))
    elif args.fixture:
        paths = [args.fixture]
    else:
        ap.error("Provide --fixture PATH or --all-fixtures")

    if not paths:
        print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
        return 1

    exit_code = 0
    for path in paths:
        message = _load_fixture(path)
        result = classify(message)
        if args.json:
            payload = {
                "fixture": str(path),
                "message": asdict(message),
                "classification": {
                    "label": result.label.value,
                    "confidence": result.confidence,
                    "reasons": result.reasons,
                    "extracted_roles": [asdict(r) for r in result.extracted_roles],
                },
            }
            print(json.dumps(payload, indent=2))
        else:
            if len(paths) > 1:
                print(f"--- {path.name} ---")
            print(_format_result(message, result))
            if len(paths) > 1:
                print()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
