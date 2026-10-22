import pytest


@pytest.fixture(autouse=True)
def _isolated_llm_call_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALL_LOG", str(tmp_path / "llm-calls.jsonl"))
