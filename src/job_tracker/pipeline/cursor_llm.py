"""Text-only Grok calls billed to Cursor Ultra.

The recruiting pipeline used to call Anthropic directly. Those requests never
drew from the Ultra allowance. This client sends the same system/user pair to
a local Cursor agent pinned to Grok 4.7 High Fast, with no tools and a
throwaway working directory, so the tokens land in the Cursor Models pool.

`messages.create` keeps the shape the existing callers and their test fakes
already use. Anthropic-only arguments (`thinking`, `cache_control`) are
accepted and ignored.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from types import SimpleNamespace

# Catalog id from Cursor.models.list(). The usage page prints this preset as
# grok-4.7-high-fast; the SDK selects it with effort=high and fast=true.
GROK_HIGH_FAST = "grok-4.7"

# Grok 4.7 Fast, USD per million tokens: input, output, cache read.
# Standard speed is half of these. Cache write is not a separate Grok rate.
GROK_FAST_RATES = (4.0, 12.0, 1.0)

_TIMEOUT_S = 120.0
_KEY_ENV = "CURSOR_API_KEY"

# Tests replace this with a stub whose `.prompt` returns a RunResult-shaped
# object. Production leaves it unset and uses cursor_sdk.Agent.
Agent = None

_REJECT_PARTS = ("claude", "gpt", "gemini", "composer", "opus", "sonnet", "haiku", "fable")


class CursorLLMError(RuntimeError):
    """The Grok call could not be started or did not finish with text."""


def require_grok(model: str | None) -> str:
    """Pin an explicit model id to Grok. An empty value means the Ultra preset."""
    name = (model or "").strip()
    if not name:
        return GROK_HIGH_FAST
    lowered = name.lower()
    if lowered in {"auto", "auto-smart"} or any(part in lowered for part in _REJECT_PARTS):
        raise CursorLLMError(
            f"Recruiting scripts call only Grok through Cursor Ultra. Refusing model {name!r}."
        )
    if not lowered.startswith("grok-"):
        raise CursorLLMError(
            f"Recruiting scripts call only Grok through Cursor Ultra. Refusing model {name!r}."
        )
    return name


def api_key() -> str:
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise CursorLLMError(
            "CURSOR_API_KEY is not set. Add a user key from "
            "https://cursor.com/dashboard/integrations to the workspace .env."
        )
    return key


def grok_selection(model: str):
    """The catalog id plus the High Fast parameters for a Grok 4.7 preset."""
    from cursor_sdk.types import ModelParameterValue, ModelSelection

    name = require_grok(model)
    if name == GROK_HIGH_FAST or name.startswith("grok-4.7-"):
        return ModelSelection(
            id=GROK_HIGH_FAST,
            params=(
                ModelParameterValue(id="context", value="256k"),
                ModelParameterValue(id="reasoning_effort", value="high"),
                ModelParameterValue(id="fast", value="true"),
            ),
        )
    return ModelSelection(id=name)


def grok_rates(model: str) -> tuple[float, float, float] | None:
    """Input, output, and cache-read USD per million tokens for a Grok id."""
    if model == GROK_HIGH_FAST or str(model).startswith("grok-4.7"):
        return GROK_FAST_RATES
    return None


class _Messages:
    def __init__(self, owner: "CursorMessages") -> None:
        self._owner = owner

    def create(self, *, model: str, max_tokens: int, system, messages, **_ignored):
        return self._owner.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )


class CursorMessages:
    """Stand-in for `anthropic.Anthropic`: one `.messages.create` call per prompt."""

    def __init__(self, *, api_key_value: str | None = None, timeout_s: float = _TIMEOUT_S) -> None:
        self._api_key = api_key_value if api_key_value is not None else api_key()
        self._timeout_s = timeout_s
        self.messages = _Messages(self)

    def create(self, *, model: str, max_tokens: int, system, messages, **_ignored):
        pinned = require_grok(model)
        user = _message_text(messages)
        prompt = _compose(system, user, max_tokens)
        cwd = tempfile.mkdtemp(prefix="cursor-grok-")
        try:
            from cursor_sdk import AgentOptions, LocalAgentOptions

            options = AgentOptions(
                model=grok_selection(pinned),
                api_key=self._api_key,
                tools=[],
                local=LocalAgentOptions(cwd=cwd, setting_sources=[]),
            )
            result = _prompt_with_deadline(prompt, options, self._timeout_s, cwd, self._api_key)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        status = str(getattr(result, "status", "") or "")
        if status and status not in {"finished", "completed"}:
            detail = getattr(result, "error_message", None) or status
            raise CursorLLMError(f"Grok agent ended with {detail}")
        text = getattr(result, "result", "") or ""
        usage = getattr(result, "usage", None)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_input_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                cache_creation_input_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
            ),
        )


def _agent_cls():
    if Agent is not None:
        return Agent
    from cursor_sdk import Agent as SdkAgent

    return SdkAgent


def _prompt_with_deadline(prompt: str, options, timeout_s: float, cwd: str, key: str):
    """Call `Agent.prompt` and cancel the local run if it is still going."""
    agent_cls = _agent_cls()
    box: dict = {}

    def go() -> None:
        try:
            box["result"] = agent_cls.prompt(prompt, options)
        except BaseException as exc:  # noqa: BLE001 — handed back to the caller
            box["error"] = exc
        finally:
            box["done"] = True

    thread = threading.Thread(target=go, name="cursor-grok-prompt", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if not box.get("done"):
        _cancel_local_runs(cwd, key)
        raise CursorLLMError(f"Grok agent exceeded {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def _cancel_local_runs(cwd: str, key: str) -> None:
    """Best-effort stop for a prompt that outlived the deadline."""
    try:
        agent_cls = _agent_cls()
        listed = agent_cls.list({"runtime": "local", "cwd": cwd, "apiKey": key})
    except Exception:
        return
    for info in getattr(listed, "items", []) or []:
        agent_id = getattr(info, "agent_id", "") or getattr(info, "id", "")
        if not agent_id:
            continue
        try:
            runs = agent_cls.list_runs(agent_id, cwd=cwd)
        except Exception:
            continue
        for run in getattr(runs, "items", []) or []:
            status = str(getattr(run, "status", "") or "")
            if status in {"finished", "completed", "error", "cancelled", "canceled"}:
                continue
            try:
                run.cancel()
            except Exception:
                continue


def _compose(system, user: str, max_tokens: int) -> str:
    return (
        "You are answering one structured request for a recruiting pipeline. "
        "Reply with only the answer the system instructions ask for. "
        "Do not use tools, do not edit files, and do not add commentary. "
        f"Keep the answer within {max_tokens} tokens.\n\n"
        "SYSTEM INSTRUCTIONS:\n"
        f"{_as_text(system)}\n\n"
        "USER:\n"
        f"{user}"
    )


def _message_text(messages) -> str:
    if not messages:
        return ""
    first = messages[0]
    content = first.get("content") if isinstance(first, dict) else getattr(first, "content", "")
    return _as_text(content)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        return "\n".join(parts)
    return str(value)


def list_grok_model_ids(key: str | None = None) -> list[str]:
    """Ids from `Cursor.models.list()` whose id or display name mentions Grok."""
    from cursor_sdk import Cursor

    models = Cursor.models.list(api_key=key or api_key())
    found = []
    for model in models:
        label = f"{model.id} {model.display_name}".lower()
        if "grok" not in label:
            continue
        found.append(model.id)
        for variant in model.variants:
            variant_label = f"{variant.display_name}".strip()
            if variant_label:
                found.append(f"{model.id} [{variant_label}]")
    return found
