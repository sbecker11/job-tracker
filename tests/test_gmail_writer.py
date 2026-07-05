"""Tests for the Gmail write helpers (email/gmail_writer.py).

Uses a minimal fake Gmail API service double — no real network calls.
"""

from __future__ import annotations

from job_tracker.email import gmail_writer


class _FakeLabelsResource:
    def __init__(self, existing: list[dict]):
        self._existing = existing
        self.created: list[dict] = []

    def list(self, userId):  # noqa: N803 - matches google-api-python-client's kwarg casing
        return _FakeExecutable({"labels": self._existing})

    def create(self, userId, body):  # noqa: N803
        new = {"id": f"Label_{len(self._existing) + len(self.created) + 1}", "name": body["name"]}
        self.created.append(new)
        return _FakeExecutable(new)


class _FakeMessagesResource:
    def __init__(self):
        self.modify_calls: list[dict] = []

    def modify(self, userId, id, body):  # noqa: N803
        self.modify_calls.append({"id": id, "body": body})
        return _FakeExecutable({"id": id})


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeUsers:
    def __init__(self, labels_resource, messages_resource):
        self._labels = labels_resource
        self._messages = messages_resource

    def labels(self):
        return self._labels

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, existing_labels: list[dict] | None = None):
        self.labels_resource = _FakeLabelsResource(existing_labels or [])
        self.messages_resource = _FakeMessagesResource()

    def users(self):
        return _FakeUsers(self.labels_resource, self.messages_resource)


def test_get_or_create_label_returns_existing_id_without_creating():
    service = _FakeService(existing_labels=[{"id": "Label_1", "name": "JobTracker/ACCEPT"}])
    label_id = gmail_writer.get_or_create_label(service, "JobTracker/ACCEPT")
    assert label_id == "Label_1"
    assert service.labels_resource.created == []


def test_get_or_create_label_creates_when_missing():
    service = _FakeService(existing_labels=[])
    label_id = gmail_writer.get_or_create_label(service, "JobTracker/DENY")
    assert label_id is not None
    assert len(service.labels_resource.created) == 1
    assert service.labels_resource.created[0]["name"] == "JobTracker/DENY"


def test_label_and_archive_adds_label_and_removes_inbox():
    service = _FakeService()
    gmail_writer.label_and_archive(service, "msg-123", "Label_1")
    assert len(service.messages_resource.modify_calls) == 1
    call = service.messages_resource.modify_calls[0]
    assert call["id"] == "msg-123"
    assert call["body"]["addLabelIds"] == ["Label_1"]
    assert call["body"]["removeLabelIds"] == ["INBOX"]


def test_outcome_label_constants_are_distinct_and_prefixed():
    assert len(set(gmail_writer.ALL_OUTCOME_LABELS)) == 3
    assert all(label.startswith(gmail_writer.LABEL_PREFIX) for label in gmail_writer.ALL_OUTCOME_LABELS)
