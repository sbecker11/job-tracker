"""Time one short and one longer text-only Grok call. Not part of the schedule.

Run from job-tracker with the venv active, after CURSOR_API_KEY is in the
workspace .env:

    python scripts/spike_cursor_grok.py

Prints the Grok ids Cursor returns, whether grok-4.7-high-fast is one of
them, and the elapsed time of two prompts. Does not print the key.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_tracker.pipeline.cursor_llm import (  # noqa: E402
    GROK_HIGH_FAST,
    CursorLLMError,
    CursorMessages,
    list_grok_model_ids,
)


def main() -> int:
    try:
        ids = list_grok_model_ids()
    except CursorLLMError as exc:
        print(exc, file=sys.stderr)
        return 2
    print("grok models:")
    for item in ids:
        print(f"  {item}")
    bare = [item for item in ids if " [" not in item]
    if GROK_HIGH_FAST in bare:
        print(f"confirmed: {GROK_HIGH_FAST}")
    else:
        print(f"NOT in the catalog: {GROK_HIGH_FAST}", file=sys.stderr)
        return 3

    client = CursorMessages()
    samples = [
        ("classify", "Reply with only the word pong.", 16),
        (
            "evaluate",
            "Reply with a JSON object {\"verdict\": \"pass\", \"match_pct\": 0} and nothing else. "
            + ("role " * 800),
            64,
        ),
    ]
    for name, user, max_tokens in samples:
        start = time.monotonic()
        response = client.messages.create(
            model=GROK_HIGH_FAST,
            max_tokens=max_tokens,
            system="Follow the user request exactly.",
            messages=[{"role": "user", "content": user}],
        )
        elapsed = time.monotonic() - start
        text = response.content[0].text.replace("\n", " ")[:120]
        usage = response.usage
        print(
            f"{name}: {elapsed:.1f}s  {usage.input_tokens} in / {usage.output_tokens} out  {text!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
