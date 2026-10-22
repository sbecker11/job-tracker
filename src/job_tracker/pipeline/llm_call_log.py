"""Append one line per Anthropic API call to the workspace-wide call log.

`<workspace root>/var/llm-calls.jsonl` (gitignored), shared with
`comms-migration`'s classifier, so `spend_report` can total real spend per
month and compare it with Anthropic's bills. The per-lead
`llm_eval_cost_usd` column keeps only the latest evaluation and misses every
other kind of call. `LLM_CALL_LOG` overrides the path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "job-tracker"


def log_path() -> Path:
    override = os.environ.get("LLM_CALL_LOG")
    if override:
        return Path(override)
    root = os.environ.get("RECRUITING_AUTOMATION_WORKSPACE_ROOT")
    workspace = Path(root) if root else Path(__file__).resolve().parents[4]
    return workspace / "var" / "llm-calls.jsonl"


def log_call(*, step: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float | None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE,
        "step": step,
        "model": model,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cost_usd": round(float(cost_usd or 0.0), 6),
    }
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
