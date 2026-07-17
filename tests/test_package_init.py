"""Edge coverage for job_tracker.__init__ env-key diagnostics."""

from __future__ import annotations

import job_tracker


def test_log_env_key_source_missing(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    job_tracker._log_env_key_source("ANTHROPIC_API_KEY")
    err = capsys.readouterr().out
    assert "WARNING" in err
    assert "not set" in err


def test_log_env_key_source_from_shell(monkeypatch, capsys, tmp_path):
    # Point both env files at empty temps so the value can only come from the shell.
    empty = tmp_path / "empty.env"
    empty.write_text("")
    monkeypatch.setattr(job_tracker, "_PROJECT_ROOT_ENV", empty)
    monkeypatch.setattr(job_tracker, "_SHARED_ENV", empty)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-key-xyz")
    job_tracker._log_env_key_source("ANTHROPIC_API_KEY")
    out = capsys.readouterr().out
    assert "pre-existing shell/process environment" in out


def test_log_env_key_source_from_local(monkeypatch, capsys, tmp_path):
    local = tmp_path / "local.env"
    shared = tmp_path / "shared.env"
    local.write_text("ANTHROPIC_API_KEY=local-key-abc\n")  # pragma: allowlist secret
    shared.write_text("ANTHROPIC_API_KEY=shared-key-def\n")  # pragma: allowlist secret
    monkeypatch.setattr(job_tracker, "_PROJECT_ROOT_ENV", local)
    monkeypatch.setattr(job_tracker, "_SHARED_ENV", shared)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-key-abc")  # pragma: allowlist secret
    job_tracker._log_env_key_source("ANTHROPIC_API_KEY")
    out = capsys.readouterr().out
    assert "local .env" in out


def test_log_env_key_source_from_shared(monkeypatch, capsys, tmp_path):
    local = tmp_path / "local.env"
    shared = tmp_path / "shared.env"
    local.write_text("")
    shared.write_text("ANTHROPIC_API_KEY=shared-key-def\n")  # pragma: allowlist secret
    monkeypatch.setattr(job_tracker, "_PROJECT_ROOT_ENV", local)
    monkeypatch.setattr(job_tracker, "_SHARED_ENV", shared)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shared-key-def")  # pragma: allowlist secret
    job_tracker._log_env_key_source("ANTHROPIC_API_KEY")
    out = capsys.readouterr().out
    assert "shared .env" in out
