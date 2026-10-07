"""Operator export and delete routes (ADR-011 part 6, #257)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import create_test_app
from fastapi.testclient import TestClient

from agents_system.config import Settings, get_settings
from agents_system.subject_data import SubjectDataRegistry, SubjectRef

KEY = "operator-secret"
AUTH = {"Authorization": f"Bearer {KEY}"}
BODY = {"runtime_id": "rt", "subject_key": "participant:1"}


class Store:
    name = "facts"
    tables = frozenset({"facts"})

    def __init__(self) -> None:
        self.rows = {
            ("rt", "participant:1"): [{"text": "mine"}],
            ("rt", "participant:2"): [{"text": "theirs"}],
        }

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        return list(self.rows.get((subject.runtime_id, subject.subject_key), []))

    async def delete(self, subject: SubjectRef) -> int:
        return len(self.rows.pop((subject.runtime_id, subject.subject_key), []))


class Threads:
    def __init__(self) -> None:
        self.threads = {"t1": ["hi"], "t2": ["yo"]}

    async def export_thread(self, thread_id: str) -> dict[str, Any] | None:
        return (
            {"thread_id": thread_id, "messages": self.threads[thread_id]}
            if thread_id in self.threads
            else None
        )

    async def delete_thread(self, thread_id: str) -> int:
        return 1 if self.threads.pop(thread_id, None) is not None else 0


@pytest.fixture(autouse=True)
def _clear() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(
    monkeypatch: pytest.MonkeyPatch, *, key: str = KEY
) -> tuple[TestClient, Store, Threads]:
    monkeypatch.setenv("OPERATOR_API_KEY", key)
    app = create_test_app()
    registry, store, threads = SubjectDataRegistry(), Store(), Threads()
    registry.register(store)
    app.state.subject_data_registry = registry
    app.state.checkpoint_threads = threads

    async def subject_threads(subject: SubjectRef) -> list[str]:
        return ["t1"]

    app.state.subject_threads = subject_threads
    return TestClient(app), store, threads


def test_routes_do_not_exist_without_an_operator_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPERATOR_API_KEY", raising=False)
    client = TestClient(create_test_app())
    for path in ("export", "delete"):
        assert (
            client.post(
                f"/operator/subjects/{path}", json=BODY, headers=AUTH
            ).status_code
            == 404
        )


@pytest.mark.parametrize("path", ["export", "delete"])
def test_missing_or_wrong_key_is_refused_and_nothing_is_touched(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    client, store, threads = _client(monkeypatch)
    url = f"/operator/subjects/{path}"
    assert client.post(url, json=BODY).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post(url, json=BODY, headers=wrong).status_code == 401
    # The adapter's key is not the operator's.
    assert (
        client.post(url, json=BODY, headers={"Authorization": "Basic x"}).status_code
        == 401
    )
    assert ("rt", "participant:1") in store.rows and "t1" in threads.threads


def test_export_then_delete_one_subject_leaves_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, threads = _client(monkeypatch)
    exported = client.post("/operator/subjects/export", json=BODY, headers=AUTH)
    assert exported.status_code == 200
    assert exported.json()["stores"] == {"facts": [{"text": "mine"}]}
    assert exported.json()["threads"] == [{"thread_id": "t1", "messages": ["hi"]}]

    deleted = client.post(
        "/operator/subjects/delete",
        json=BODY,
        headers={**AUTH, "X-Operator-Id": "alice-ops"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["stores"] == {"facts": 1}
    assert deleted.json()["threads"] == 1
    assert ("rt", "participant:1") not in store.rows
    assert ("rt", "participant:2") in store.rows
    assert "t1" not in threads.threads and "t2" in threads.threads


def test_body_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch)
    r = client.post(
        "/operator/subjects/export", json={"runtime_id": "rt"}, headers=AUTH
    )
    assert r.status_code == 422
    r = client.post(
        "/operator/subjects/delete",
        json={**BODY, "subject_key": ""},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_a_failing_store_reports_partial_deletion_with_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _client(monkeypatch)

    async def boom(subject: SubjectRef) -> int:
        raise RuntimeError("db down")

    store.delete = boom  # type: ignore[method-assign]
    r = client.post("/operator/subjects/delete", json=BODY, headers=AUTH)
    assert r.status_code == 500
    assert r.json()["detail"]["failed"] == ["facts"]


def test_operator_key_must_differ_from_the_adapter_key() -> None:
    with pytest.raises(ValueError, match="operator_api_key"):
        Settings(operator_api_key="same", adapter_api_key="same")  # type: ignore[call-arg]


def test_subject_data_retention_parses_never_and_seconds() -> None:
    s = Settings(subject_data_retention={"facts": "never", "usage": "60"})  # type: ignore[call-arg]
    assert s.subject_data_retention == {"facts": None, "usage": 60}
    with pytest.raises(ValueError):
        Settings(subject_data_retention={"facts": 0})  # type: ignore[call-arg]
