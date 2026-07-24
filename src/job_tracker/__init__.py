"""job_tracker package.

Loads `.env` (project root, next to pyproject.toml) as soon as the package is
imported, so every CLI entry point picks up secrets like ANTHROPIC_API_KEY
without each script having to remember to call load_dotenv() itself. Existing
environment variables are never overridden.

Also falls back to the shared `.env` one level up, in the
`workspace-recruiting-automation` parent that holds this repo and its
siblings (`comms-migration`, `recruiting-automation`) — that's where
ANTHROPIC_API_KEY genuinely lives now (2026-07-15) rather than being
duplicated per-repo, since both this repo's --llm-fallback extraction and
comms-migration's LLM classification fallback need the same key. The
project-root `.env` above is loaded first and always wins for any key it
sets locally; the shared one only fills in whatever's still missing.

The shared parent's location is derived from `_PROJECT_ROOT_ENV.parent`
(one level above the already-correct project root) rather than its own
independently-counted `Path(__file__).parents[N]`, specifically so it can
never drift out of sync with `_PROJECT_ROOT_ENV` if this file's own depth
in the repo ever changes — recruiting-automation's `status.sh`/`run_cycle.sh`
independently derive the very same directory via a shell `WORKSPACE_ROOT`
var (see that repo's install.sh), so `RECRUITING_AUTOMATION_WORKSPACE_ROOT`
is checked first here too, for the rare case this package is imported from
inside a subprocess that already had a non-default workspace root exported.

Prints one diagnostic line about ANTHROPIC_API_KEY's source at import time
(added 2026-07-15) so every hourly cycle's log durably records whether the
shared-.env fallback actually worked, instead of that only being answerable
via an ad-hoc `python -c` check — and so a missing key surfaces here as a
clear one-line warning instead of a confusing 401 deep inside whichever
Anthropic call site hits it first.

Gated behind `JOB_TRACKER_LOG_ENV_SOURCE=1` (added 2026-07-24) since printing
this on every single interactive CLI invocation (list_leads, apply_package,
etc.) turned out to be pure noise for a human running commands one at a
time — the durable-log value described above only actually matters for the
unattended hourly cycle, so `run_cycle.sh` sets the env var and everything
else stays quiet by default. The missing-key WARNING below is NOT gated —
that's an actionable error worth seeing regardless of context.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_PROJECT_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
_WORKSPACE_ROOT_OVERRIDE = os.environ.get("RECRUITING_AUTOMATION_WORKSPACE_ROOT")
_SHARED_ENV = (
    Path(_WORKSPACE_ROOT_OVERRIDE) / ".env"
    if _WORKSPACE_ROOT_OVERRIDE
    else _PROJECT_ROOT_ENV.parent.parent / ".env"
)
load_dotenv(_PROJECT_ROOT_ENV)
load_dotenv(_SHARED_ENV)


def _log_env_key_source(key: str) -> None:
    value = os.environ.get(key, "")
    if not value:
        print(
            f"[job_tracker] WARNING: {key} is not set (checked "
            f"{_PROJECT_ROOT_ENV}, {_SHARED_ENV}, and the shell environment) "
            "— any LLM-fallback call will fail."
        )
        return
    # Attribute by exact value match rather than mere presence, so a
    # present-but-blank entry in either file (which load_dotenv treats as
    # "already set" and won't let a later call override) can't be misreported
    # as the source when it actually contributed nothing.
    if os.environ.get("JOB_TRACKER_LOG_ENV_SOURCE") != "1":
        return
    if dotenv_values(_PROJECT_ROOT_ENV).get(key) == value:
        source = f"local .env ({_PROJECT_ROOT_ENV})"
    elif dotenv_values(_SHARED_ENV).get(key) == value:
        source = f"shared .env ({_SHARED_ENV})"
    else:
        source = "pre-existing shell/process environment"
    print(f"[job_tracker] {key}: loaded from {source} ({len(value)} chars).")


_log_env_key_source("ANTHROPIC_API_KEY")
