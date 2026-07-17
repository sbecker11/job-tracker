"""Fake-service coverage for gmail_reader network helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from job_tracker.email import gmail_reader


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeService:
    def __init__(self, pages=None, messages=None):
        self._pages = list(pages or [{"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"}, {"messages": [{"id": "m3"}]}])
        self._idx = 0
        self._messages = messages or {}

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        page = self._pages[min(self._idx, len(self._pages) - 1)]
        self._idx += 1
        return _Exec(page)

    def get(self, **kwargs):
        return _Exec(self._messages[kwargs["id"]])


def test_repo_root_and_account_paths(monkeypatch, tmp_path: Path):
    assert gmail_reader.repo_root().name == "job-tracker"
    monkeypatch.setenv("JOB_TRACKER_GMAIL_CREDENTIALS", str(tmp_path / "c.json"))
    monkeypatch.setenv("JOB_TRACKER_GMAIL_TOKEN", str(tmp_path / "t.json"))
    monkeypatch.setenv("JOB_TRACKER_GMAIL_TOKEN_MODIFY", str(tmp_path / "tm.json"))
    assert gmail_reader.default_credentials_path() == tmp_path / "c.json"
    assert gmail_reader.default_token_path() == tmp_path / "t.json"
    assert gmail_reader.default_token_path(writable=True) == tmp_path / "tm.json"
    # named account ignores env overrides
    assert "personal_hub" in str(gmail_reader.default_credentials_path("personal_hub"))


def test_default_paths_prefer_config_then_local(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("JOB_TRACKER_GMAIL_CREDENTIALS", raising=False)
    monkeypatch.delenv("JOB_TRACKER_GMAIL_TOKEN", raising=False)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(gmail_reader, "CONFIG_DIR", cfg)
    monkeypatch.setattr(gmail_reader, "repo_root", lambda: tmp_path)
    local_creds = tmp_path / "credentials.json"
    local_creds.write_text("{}")
    assert gmail_reader.default_credentials_path() == local_creds
    # when config file exists, prefer it
    (cfg / "credentials.json").write_text("{}")
    assert gmail_reader.default_credentials_path() == cfg / "credentials.json"


def test_list_and_fetch_message():
    raw = {
        "id": "m1",
        "threadId": "t",
        "snippet": "s",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "Subject", "value": "Hi"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }
    svc = _FakeService(messages={"m1": raw})
    ids = gmail_reader.list_message_ids(svc, query="in:inbox", limit=2, newer_than_days=1)
    assert ids == ["m1", "m2"]
    svc2 = _FakeService(messages={"m1": raw})
    msg = gmail_reader.fetch_message(svc2, "m1")
    assert msg.id == "m1"
    assert msg.from_address == "a@b.com"


def test_list_message_ids_pagination():
    svc = _FakeService()
    ids = gmail_reader.list_message_ids(svc, query="in:inbox")
    assert ids == ["m1", "m2", "m3"]


def test_get_gmail_service_missing_creds(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        gmail_reader.get_gmail_service(tmp_path / "missing.json", tmp_path / "tok.json")


def test_get_gmail_service_valid_token(monkeypatch, tmp_path: Path):
    creds_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    creds_file.write_text("{}")
    token_file.write_text("{}")

    fake_creds = SimpleNamespace(valid=True, expired=False, refresh_token=None, to_json=lambda: "{}")
    built = object()

    monkeypatch.setattr(gmail_reader, "_require_google_libs", lambda: None)

    import google.oauth2.credentials as cred_mod
    import googleapiclient.discovery as disc

    monkeypatch.setattr(cred_mod.Credentials, "from_authorized_user_file", lambda *a, **k: fake_creds)
    monkeypatch.setattr(disc, "build", lambda *a, **k: built)

    # Re-import path uses local imports inside function — patch via sys.modules injection
    # by monkeypatching the names the function will import
    import job_tracker.email.gmail_reader as gr

    class FakeCreds:
        @staticmethod
        def from_authorized_user_file(*a, **k):
            return fake_creds

    class FakeFlow:
        @staticmethod
        def from_client_secrets_file(*a, **k):
            raise AssertionError("should not open browser")

    monkeypatch.setitem(
        __import__("sys").modules,
        "google.oauth2.credentials",
        SimpleNamespace(Credentials=FakeCreds),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "google.auth.transport.requests",
        SimpleNamespace(Request=object),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "google_auth_oauthlib.flow",
        SimpleNamespace(InstalledAppFlow=FakeFlow),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "googleapiclient.discovery",
        SimpleNamespace(build=lambda *a, **k: built),
    )

    svc = gr.get_gmail_service(creds_file, token_file)
    assert svc is built


def test_fetch_unread_and_by_id_use_service(monkeypatch, tmp_path: Path):
    raw = {
        "id": "m9",
        "threadId": "t",
        "snippet": "s",
        "labelIds": [],
        "payload": {
            "headers": [{"name": "From", "value": "r@x.com"}, {"name": "Subject", "value": "S"}],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }
    svc = _FakeService(pages=[{"messages": [{"id": "m9"}]}], messages={"m9": raw})
    monkeypatch.setattr(gmail_reader, "get_gmail_service", lambda *a, **k: svc)
    msgs = gmail_reader.fetch_unread(limit=1, credentials_path=tmp_path / "c", token_path=tmp_path / "t")
    assert len(msgs) == 1
    assert msgs[0].id == "m9"
    one = gmail_reader.fetch_message_by_id("m9", credentials_path=tmp_path / "c", token_path=tmp_path / "t")
    assert one.id == "m9"


def test_writable_service_uses_modify_scope(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_get(credentials_path, token_path, *, account=None, scopes=()):
        seen["scopes"] = scopes
        seen["token"] = token_path
        return "svc"

    monkeypatch.setattr(gmail_reader, "get_gmail_service", fake_get)
    monkeypatch.setattr(gmail_reader, "default_credentials_path", lambda a=None: tmp_path / "c.json")
    monkeypatch.setattr(
        gmail_reader, "default_token_path", lambda a=None, writable=False: tmp_path / ("tm.json" if writable else "t.json")
    )
    assert gmail_reader.get_gmail_service_writable() == "svc"
    assert seen["scopes"] == gmail_reader.GMAIL_SCOPES_MODIFY
    assert seen["token"].name == "tm.json"


def test_get_gmail_service_refresh_then_relogin(monkeypatch, tmp_path: Path):
    creds_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    creds_file.write_text("{}")
    token_file.write_text("{}")

    state = {"refreshed": False}

    class Creds:
        valid = False
        expired = True
        refresh_token = "rt"

        def refresh(self, request):
            state["refreshed"] = True
            raise RuntimeError("invalid_grant")

        def to_json(self):
            return '{"ok":true}'

    fresh = SimpleNamespace(valid=True, to_json=lambda: '{"ok":true}')

    class FakeFlow:
        @staticmethod
        def from_client_secrets_file(*a, **k):
            return SimpleNamespace(run_local_server=lambda port=0: fresh)

    monkeypatch.setattr(gmail_reader, "_require_google_libs", lambda: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "google.oauth2.credentials",
        SimpleNamespace(Credentials=SimpleNamespace(from_authorized_user_file=lambda *a, **k: Creds())),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "google.auth.transport.requests",
        SimpleNamespace(Request=object),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "google_auth_oauthlib.flow",
        SimpleNamespace(InstalledAppFlow=FakeFlow),
    )
    built = object()
    monkeypatch.setitem(
        __import__("sys").modules,
        "googleapiclient.discovery",
        SimpleNamespace(build=lambda *a, **k: built),
    )

    svc = gmail_reader.get_gmail_service(creds_file, token_file, account="personal_hub")
    assert svc is built
    assert state["refreshed"] is True
    assert token_file.read_text() == '{"ok":true}'
