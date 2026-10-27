"""Files an OpenAI-compatible adapter turn produced (issue #319).

An application registers ``create_app(output_files=OutputFiles(...))``. After
each ``/v1/chat/completions`` turn the adapter awaits the application's hook
with that request's ``AdapterRequestInfo`` and the turn's messages, and
appends the files it returns to the assistant message as Markdown: a small
image inline as a ``data:`` URI, anything else as a link to
``GET /v1/files/{token}``, a bearer-free download that expires. Nothing
configured means exactly today's response and no download route.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from conftest import create_test_app
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AnyMessage

from agents_system.agent.graph import TurnResult
from agents_system.config import get_settings
from agents_system.integration.output_files import (
    LocalDirectoryFileStore,
    OutputFile,
    OutputFiles,
    StoredFile,
    markdown_label,
    strip_rendered_files,
)
from agents_system.integration.request_context import AdapterRequestInfo

RUNTIME_ID = "acme__sales-agent"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
URL_RE = re.compile(r"\((http://[^)]+/v1/files/([A-Za-z0-9_-]+))\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents_system.integration.openai_adapter as adapter_mod

    fake_settings = MagicMock()
    fake_settings.adapter_api_key = "adapter-key"
    fake_settings.adapter_max_body_bytes = 1_048_576
    monkeypatch.setattr(adapter_mod, "get_settings", lambda: fake_settings)


def _fake_runtime(text: str = "ok") -> MagicMock:
    runtime = MagicMock()
    runtime.run_turn_with_usage = AsyncMock(
        return_value=TurnResult(messages=[AIMessage(content=text)], usage=None)  # type: ignore[arg-type]
    )
    return runtime


def _app(runtime: Any, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    app = create_test_app(**overrides)
    app.state.runtimes = {RUNTIME_ID: runtime}
    app.state.adapter_model_ids = frozenset({RUNTIME_ID})
    app.state.engine = MagicMock()
    _patch_settings(monkeypatch)
    return app


def _post(client: TestClient, messages: list[dict[str, Any]] | None = None) -> Any:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": RUNTIME_ID,
            "messages": messages or [{"role": "user", "content": "hola"}],
        },
        headers={"Authorization": "Bearer adapter-key"},
    )


def _content(response: Any) -> str:
    assert response.status_code == 200, response.text
    return str(response.json()["choices"][0]["message"]["content"])


class _Hook:
    """Returns fixed files and records what it was given."""

    def __init__(self, *files: OutputFile, result: Any = None) -> None:
        self.result = list(files) if result is None else result
        self.seen: list[tuple[AdapterRequestInfo, list[AnyMessage]]] = []

    async def __call__(
        self, info: AdapterRequestInfo, messages: Sequence[AnyMessage]
    ) -> Any:
        self.seen.append((info, list(messages)))
        return self.result


def _read(stored: StoredFile) -> bytes:
    if stored.data is not None:
        return stored.data
    assert stored.file is not None
    with stored.file as handle:
        return handle.read()


def _config(tmp_path: Path, hook: Any, **overrides: Any) -> OutputFiles:
    kwargs: dict[str, Any] = {
        "hook": hook,
        "store": LocalDirectoryFileStore(tmp_path / "files"),
    }
    kwargs.update(overrides)
    return OutputFiles(**kwargs)


# ---------------------------------------------------------------------------
# Nothing configured: exactly today's behavior
# ---------------------------------------------------------------------------


def test_without_output_files_the_response_and_routes_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(_fake_runtime(), monkeypatch)
    client = TestClient(app)

    assert app.state.output_files is None
    assert _content(_post(client)) == "ok"
    assert not any(
        getattr(route, "path", "") == "/v1/files/{token}" for route in app.routes
    )
    assert client.get("/v1/files/" + "a" * 43).status_code == 404


def test_without_output_files_inline_images_in_history_reach_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _fake_runtime()
    client = TestClient(_app(runtime, monkeypatch))
    earlier = "here ![c](data:image/png;base64,AAAA)"

    _post(
        client,
        [
            {"role": "user", "content": "chart"},
            {"role": "assistant", "content": earlier},
            {"role": "user", "content": "thanks"},
        ],
    )

    sent = runtime.run_turn_with_usage.await_args.kwargs["messages"]
    assert sent[1].content == earlier


def test_create_app_refuses_output_files_of_the_wrong_type() -> None:
    with pytest.raises(TypeError, match="output_files"):
        create_test_app(output_files={"hook": None})


# ---------------------------------------------------------------------------
# Configuration is validated when the app is built
# ---------------------------------------------------------------------------


def test_output_files_refuses_a_hook_that_is_not_callable(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="hook"):
        _config(tmp_path, "not a function")


def test_output_files_refuses_a_store_without_the_port(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="store"):
        OutputFiles(hook=_Hook(), store=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"inline_image_max_bytes": -1},
        {"max_file_bytes": 0},
        {"ttl_s": 0},
        {"public_base_url": "ftp://files.example.com"},
        {"public_base_url": ""},
    ],
)
def test_output_files_refuses_bad_limits_or_base_url(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, _Hook(), **overrides)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"filename": "a.png", "mime_type": "image/png"},
        {"filename": "a.png", "mime_type": "image/png", "data": b"x", "path": "a"},
        {"filename": "", "mime_type": "image/png", "data": b"x"},
        {"filename": "a.png", "mime_type": "image/png\r\nX-Evil: 1", "data": b"x"},
        {"filename": "a.png", "mime_type": "png", "data": b"x"},
    ],
)
def test_output_file_refuses_invalid_fields(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        OutputFile(**kwargs)


# ---------------------------------------------------------------------------
# The hook: request-local input
# ---------------------------------------------------------------------------


def test_the_hook_gets_this_requests_info_and_the_turns_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _fake_runtime()
    hook = _Hook()
    client = TestClient(
        _app(runtime, monkeypatch, output_files=_config(tmp_path, hook))
    )

    assert _content(_post(client)) == "ok"

    (info, messages), *_ = hook.seen
    assert info.runtime_id == RUNTIME_ID
    assert (
        info.session_id == runtime.run_turn_with_usage.await_args.kwargs["session_id"]
    )
    assert [m.content for m in messages] == ["ok"]


def test_a_hook_with_no_files_leaves_the_text_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for result in ([], None):
        client = TestClient(
            _app(
                _fake_runtime(),
                monkeypatch,
                output_files=_config(tmp_path, _Hook(result=result)),
            )
        )
        assert _content(_post(client)) == "ok"


# ---------------------------------------------------------------------------
# Rendering: inline image below the limit, link above it
# ---------------------------------------------------------------------------


def test_a_small_image_is_inlined_as_a_data_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _Hook(
        OutputFile(
            filename="chart.png", mime_type="image/png", data=PNG, caption="Sales"
        )
    )
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )

    encoded = base64.b64encode(PNG).decode()
    assert _content(_post(client)) == (
        f"ok\n\n![Sales](data:image/png;base64,{encoded})"
    )


def test_an_image_at_the_limit_is_inlined_and_one_byte_over_is_linked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    at_limit = OutputFile(filename="a.png", mime_type="image/png", data=b"a" * 10)
    over = OutputFile(filename="b.png", mime_type="image/png", data=b"b" * 11)
    client = TestClient(
        _app(
            _fake_runtime(),
            monkeypatch,
            output_files=_config(
                tmp_path, _Hook(at_limit, over), inline_image_max_bytes=10
            ),
        )
    )

    content = _content(_post(client))

    first, second = content.split("\n\n")[1:]
    assert first.startswith("![a.png](data:image/png;base64,")
    assert re.fullmatch(r"!\[b\.png\]\(http://testserver/v1/files/[\w-]+\)", second)
    url, _ = URL_RE.search(second).groups()  # type: ignore[union-attr]
    download = client.get(url)
    assert download.status_code == 200
    assert download.content == b"b" * 11


def test_a_non_image_file_is_a_download_link_with_safe_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    csv = b"region,total\nnorth,10\n"
    hook = _Hook(
        OutputFile(
            filename='ventas "Q3".csv',
            mime_type="text/csv",
            data=csv,
            caption="Quarterly sales",
        )
    )
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )

    content = _content(_post(client))
    link = content.split("\n\n")[1]
    assert re.fullmatch(
        r'\[ventas "Q3"\.csv\]\(http://testserver/v1/files/[\w-]+\) - Quarterly sales',
        link,
    )
    url, token = URL_RE.search(link).groups()  # type: ignore[union-attr]
    assert len(token) >= 43

    # No bearer key: the token is the capability.
    download = client.get(url)

    assert download.status_code == 200
    assert download.content == csv
    assert download.headers["content-type"].startswith("text/csv")
    assert download.headers["content-disposition"].startswith(
        'attachment; filename="ventas _Q3_.csv"'
    )
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"
    assert "sandbox" in download.headers["content-security-policy"]


def test_a_file_given_by_path_is_served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7 body")
    hook = _Hook(
        OutputFile(filename="report.pdf", mime_type="application/pdf", path=source)
    )
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )

    url, _ = URL_RE.search(_content(_post(client))).groups()  # type: ignore[union-attr]

    assert client.get(url).content == b"%PDF-1.7 body"


def test_a_public_base_url_replaces_the_request_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _Hook(OutputFile(filename="a.txt", mime_type="text/plain", data=b"x"))
    client = TestClient(
        _app(
            _fake_runtime(),
            monkeypatch,
            output_files=_config(
                tmp_path, hook, public_base_url="https://chat.example.com/agents/"
            ),
        )
    )

    assert re.search(
        r"\(https://chat\.example\.com/agents/v1/files/[\w-]+\)",
        _content(_post(client)),
    )


def test_an_empty_reply_carries_only_the_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _Hook(OutputFile(filename="a.png", mime_type="image/png", data=PNG))
    client = TestClient(
        _app(_fake_runtime(""), monkeypatch, output_files=_config(tmp_path, hook))
    )

    assert _content(_post(client)).startswith("![a.png](data:image/png;base64,")


def test_markdown_label_cannot_break_out_of_the_link() -> None:
    label = markdown_label("x](javascript:alert(1))\n<b>![y]")

    assert "\n" not in label
    assert re.search(r"(?<!\\)[\[\]()<>]", label) is None


# ---------------------------------------------------------------------------
# Limits: an oversize file is left out and logged, the turn still answers
# ---------------------------------------------------------------------------


def test_an_oversize_file_is_skipped_and_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * 101)
    hook = _Hook(
        OutputFile(filename="big.csv", mime_type="text/csv", data=b"y" * 101),
        OutputFile(filename="big.bin", mime_type="application/octet-stream", path=big),
        OutputFile(filename="ok.txt", mime_type="text/plain", data=b"fine"),
    )
    client = TestClient(
        _app(
            _fake_runtime(),
            monkeypatch,
            output_files=_config(tmp_path, hook, max_file_bytes=100),
        )
    )

    with structlog.testing.capture_logs() as logs:
        content = _content(_post(client))

    assert "big" not in content
    assert "[ok.txt](" in content
    skipped = [e for e in logs if e["event"] == "adapter.output_file_skipped"]
    assert [(e["reason"], e["size"], e["limit"]) for e in skipped] == [
        ("too_large", 101, 100),
        ("too_large", 101, 100),
    ]


class _Boom:
    async def __call__(self, info: Any, messages: Any) -> Any:
        raise RuntimeError("secret dsn postgres://u:p@h/db")


@pytest.mark.parametrize("hook", [_Boom(), _Hook(result=42), _Hook(result=["x"])])
def test_a_failing_hook_answers_the_text_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hook: Any
) -> None:
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )

    with structlog.testing.capture_logs() as logs:
        content = _content(_post(client))

    assert content == "ok"
    assert any(e["event"] == "adapter.output_files_failed" for e in logs)


# ---------------------------------------------------------------------------
# Download tokens: expire, unguessable, identical 404s
# ---------------------------------------------------------------------------


def test_expired_unknown_and_malformed_tokens_answer_the_same_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _Clock()
    store = LocalDirectoryFileStore(tmp_path / "files", clock=clock)
    hook = _Hook(OutputFile(filename="a.txt", mime_type="text/plain", data=b"x"))
    client = TestClient(
        _app(
            _fake_runtime(),
            monkeypatch,
            output_files=_config(tmp_path, hook, store=store, ttl_s=60),
        )
    )
    url, token = URL_RE.search(_content(_post(client))).groups()  # type: ignore[union-attr]

    clock.now += 59
    assert client.get(url).status_code == 200
    clock.now += 1
    expired = client.get(url)

    unknown = client.get("/v1/files/" + "A" * len(token))
    malformed = client.get("/v1/files/..%2F..%2Fetc%2Fpasswd")
    for response in (expired, unknown, malformed):
        assert response.status_code == 404
        assert response.content == unknown.content
        assert response.headers["content-type"] == unknown.headers["content-type"]


async def test_store_tokens_are_unique_and_files_use_server_names(
    tmp_path: Path,
) -> None:
    store = LocalDirectoryFileStore(tmp_path / "files")

    tokens = {
        await store.put(
            b"x", filename="../../escape.txt", mime_type="text/plain", ttl_s=60
        )
        for _ in range(50)
    }

    assert len(tokens) == 50
    assert all(len(t) >= 43 for t in tokens)
    assert not (tmp_path / "escape.txt").exists()
    names = [p.name for p in (tmp_path / "files").iterdir()]
    assert all(t not in n for t in tokens for n in names)
    assert all("escape" not in n for n in names)


async def test_store_cleanup_removes_only_expired_files(tmp_path: Path) -> None:
    clock = _Clock()
    store = LocalDirectoryFileStore(tmp_path / "files", clock=clock)
    old = await store.put(b"old", filename="o.txt", mime_type="text/plain", ttl_s=10)
    clock.now += 5
    fresh = await store.put(b"new", filename="n.txt", mime_type="text/plain", ttl_s=10)
    clock.now += 6

    assert await store.cleanup() == 1

    assert await store.get(old) is None
    stored = await store.get(fresh)
    assert stored is not None
    assert (_read(stored), stored.filename, stored.mime_type) == (
        b"new",
        "n.txt",
        "text/plain",
    )
    assert len(list((tmp_path / "files").iterdir())) == 2


# ---------------------------------------------------------------------------
# History: inline images the adapter rendered do not flow back to the model
# ---------------------------------------------------------------------------


def test_strip_rendered_files_keeps_the_label_and_drops_the_payload() -> None:
    content = (
        "see ![Sales \\[Q3\\]](data:image/png;base64,iVBORw0K+/=) and [r](http://x)"
    )

    assert strip_rendered_files(content) == (
        "see [image: Sales \\[Q3\\]] and [r](http://x)"
    )


def test_with_output_files_inline_images_in_history_are_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _fake_runtime()
    client = TestClient(
        _app(runtime, monkeypatch, output_files=_config(tmp_path, _Hook()))
    )

    _post(
        client,
        [
            {"role": "user", "content": "chart ![u](data:image/png;base64,AAAA)"},
            {"role": "assistant", "content": "here ![c](data:image/png;base64,AAAA)"},
            {"role": "user", "content": "thanks"},
        ],
    )

    sent = runtime.run_turn_with_usage.await_args.kwargs["messages"]
    assert sent[0].content == "chart ![u](data:image/png;base64,AAAA)"
    assert sent[1].content == "here [image: c]"


# ---------------------------------------------------------------------------
# Concurrency: files never leak between parallel requests
# ---------------------------------------------------------------------------


class _Participant:
    def __init__(self, id: object) -> None:
        self.id = id
        self.active = True


class _HeaderResolver:
    async def resolve(self, request: Any) -> _Participant | None:
        user = request.headers.get("x-test-user")
        return _Participant(user) if user else None


async def test_parallel_requests_each_get_only_their_own_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    users = [f"u{i}" for i in range(20)]

    async def hook(
        info: AdapterRequestInfo, messages: Sequence[AnyMessage]
    ) -> list[OutputFile]:
        assert info.subject is not None
        user = info.subject.key.removeprefix("participant:")
        # Later requests finish first, so the hooks interleave.
        await asyncio.sleep(0.002 * (len(users) - users.index(user)))
        return [
            OutputFile(
                filename=f"{user}.png", mime_type="image/png", data=user.encode()
            ),
            OutputFile(
                filename=f"{user}.txt", mime_type="text/plain", data=user.encode()
            ),
        ]

    app = _app(
        _fake_runtime(),
        monkeypatch,
        output_files=_config(tmp_path, hook),
        subject_resolver=_HeaderResolver(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": RUNTIME_ID,
                        "messages": [{"role": "user", "content": "hola"}],
                    },
                    headers={
                        "x-test-user": user,
                        "Authorization": "Bearer adapter-key",
                    },
                )
                for user in users
            )
        )
        for user, response in zip(users, responses, strict=True):
            content = _content(response)
            inline = base64.b64encode(user.encode()).decode()
            assert f"![{user}.png](data:image/png;base64,{inline})" in content
            assert f"[{user}.txt](" in content
            others = [u for u in users if u != user]
            assert all(f"[{o}.txt]" not in content for o in others)
            url, _ = URL_RE.search(content).groups()  # type: ignore[union-attr]
            assert (await client.get(url)).content == user.encode()


def test_download_token_never_reaches_the_request_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = _Hook(OutputFile(filename="r.csv", mime_type="text/csv", data=b"a,b"))
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )
    url, token = URL_RE.search(_content(_post(client))).groups()  # type: ignore[union-attr]

    with structlog.testing.capture_logs() as logs:
        assert client.get(url).status_code == 200
        assert client.get("/v1/files/" + "A" * len(token)).status_code == 404

    paths = [e["path"] for e in logs if e["event"].startswith("request.")]
    assert paths and all(p == "/v1/files/<redacted>" for p in paths)
    assert token not in repr(logs)


# ---------------------------------------------------------------------------
# Security review of #319: hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "![" * 50_000,
        "![](data:" * 50_000,
        "[](" * 50_000,
        "[a](http://x/v1/files/" * 50_000,
        "[" + "\\[" * 50_000,
    ],
    ids=["image-openers", "data-uris", "link-openers", "download-urls", "escapes"],
)
def test_strip_rendered_files_is_fast_on_hostile_input(hostile: str) -> None:
    start = time.monotonic()

    strip_rendered_files(hostile)

    assert time.monotonic() - start < 1.0


def test_strip_rendered_files_replaces_download_links_with_a_placeholder() -> None:
    token = "T" * 43
    content = (
        f"[ventas \\(Q3\\).csv](https://chat.example.com/v1/files/{token}) - Sales\n"
        f"![chart.png](http://testserver/v1/files/{token})\n"
        f"raw http://testserver/v1/files/{token} and [r](http://x)"
    )

    stripped = strip_rendered_files(content)

    assert token not in stripped
    assert stripped == (
        "[file: ventas \\(Q3\\).csv] - Sales\n"
        "[file: chart.png]\n"
        "raw http://testserver/v1/files/<redacted> and [r](http://x)"
    )


def test_with_output_files_download_links_in_history_never_reach_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _fake_runtime()
    client = TestClient(
        _app(runtime, monkeypatch, output_files=_config(tmp_path, _Hook()))
    )
    token = "k" * 43
    user_text = f"open [x](http://testserver/v1/files/{token})"

    _post(
        client,
        [
            {"role": "user", "content": user_text},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"[a.csv](http://t/v1/files/{token})"}
                ],
            },
            {"role": "user", "content": "thanks"},
        ],
    )

    sent = runtime.run_turn_with_usage.await_args.kwargs["messages"]
    assert sent[0].content == user_text
    assert token not in repr(sent[1].content)
    assert "[file: a.csv]" in repr(sent[1].content)


def test_uvicorn_access_log_redacts_the_download_token() -> None:
    from agents_system.observability import setup_logging

    setup_logging()
    setup_logging()  # idempotent: one filter, not two
    access = logging.getLogger("uvicorn.access")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    access.addHandler(handler)
    old_level = access.level
    access.setLevel(logging.INFO)
    token = "Z" * 43
    try:
        # uvicorn's own access-log call (protocols/http/h11_impl.py).
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "GET",
            f"/v1/files/{token}?download=1",
            "1.1",
            200,
        )
        access.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/v1/models", "1.1", 200
        )
    finally:
        access.removeHandler(handler)
        access.setLevel(old_level)

    messages = [record.getMessage() for record in records]
    assert messages == [
        '127.0.0.1:5000 - "GET /v1/files/<redacted> HTTP/1.1" 200',
        '127.0.0.1:5000 - "GET /v1/models HTTP/1.1" 200',
    ]
    assert (
        len([f for f in access.filters if type(f).__name__ == "DownloadTokenFilter"])
        == 1
    )


def _path_hook(path: Path) -> _Hook:
    return _Hook(
        OutputFile(filename="f.txt", mime_type="text/plain", path=path),
        OutputFile(filename="ok.txt", mime_type="text/plain", data=b"fine"),
    )


def _skips_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hook: Any, **overrides: Any
) -> tuple[str, list[dict[str, Any]]]:
    client = TestClient(
        _app(
            _fake_runtime(),
            monkeypatch,
            output_files=_config(tmp_path, hook, **overrides),
        )
    )
    with structlog.testing.capture_logs() as logs:
        content = _content(_post(client))
    return content, [e for e in logs if e["event"] == "adapter.output_file_skipped"]


def test_a_symlinked_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"DATABASE_URL=postgres://u:p@h/db")
    link = tmp_path / "innocent.txt"
    link.symlink_to(secret)

    content, skipped = _skips_for(monkeypatch, tmp_path, _path_hook(link))

    assert "[f.txt]" not in content and "[ok.txt](" in content
    assert [e["reason"] for e in skipped] == ["not_a_regular_file"]


@pytest.mark.parametrize("make", ["fifo", "dev_zero"])
def test_a_non_regular_file_is_refused_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make: str
) -> None:
    if make == "fifo":
        path = tmp_path / "pipe"
        os.mkfifo(path)
    else:
        path = Path("/dev/zero")
        if not path.exists():
            pytest.skip("no /dev/zero here")

    content, skipped = _skips_for(monkeypatch, tmp_path, _path_hook(path))

    assert "[f.txt]" not in content and "[ok.txt](" in content
    assert [e["reason"] for e in skipped] == ["not_a_regular_file"]


def test_a_file_whose_size_lies_is_read_only_up_to_the_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = Path("/proc/self/status")  # a regular file whose st_size is 0
    if not path.exists() or path.stat().st_size != 0:
        pytest.skip("no zero-size /proc file here")

    content, skipped = _skips_for(
        monkeypatch, tmp_path, _path_hook(path), max_file_bytes=10
    )

    assert "[f.txt]" not in content
    assert [(e["reason"], e["size"], e["limit"]) for e in skipped] == [
        ("too_large", 11, 10)
    ]


async def test_cleanup_only_touches_the_stores_own_files(tmp_path: Path) -> None:
    clock = _Clock(now=time.time())
    directory = tmp_path / "files"
    store = LocalDirectoryFileStore(directory, clock=clock, stale_tmp_s=60)
    token = await store.put(b"x", filename="a.txt", mime_type="text/plain", ttl_s=10)
    foreign = {
        "package.json": b'{"name": "app"}',
        "notes.bin": b"mine",
        ("a" * 64) + ".json.bak": b"{}",
        "report.json.tmp": b"draft",
    }
    for name, data in foreign.items():
        (directory / name).write_bytes(data)
    stale = directory / (("b" * 64) + ".bin.tmp")
    stale.write_bytes(b"half")
    os.utime(stale, (clock.now - 120, clock.now - 120))
    fresh = directory / (("c" * 64) + ".json.tmp")
    fresh.write_bytes(b"{")
    clock.now += 11

    await store.cleanup()

    assert await store.get(token) is None
    for name, data in foreign.items():
        assert (directory / name).read_bytes() == data
    assert not stale.exists()
    assert fresh.exists()


def test_files_beyond_the_per_turn_count_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _Hook(
        *(
            OutputFile(filename=f"f{i}.txt", mime_type="text/plain", data=b"x")
            for i in range(4)
        )
    )

    content, skipped = _skips_for(monkeypatch, tmp_path, hook, max_files_per_turn=2)

    assert "[f0.txt](" in content and "[f1.txt](" in content
    assert "f2" not in content and "f3" not in content
    assert [(e["reason"], e["index"]) for e in skipped] == [
        ("too_many_files", 2),
        ("too_many_files", 3),
    ]


def test_files_beyond_the_per_turn_total_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hook = _Hook(
        OutputFile(filename="a.txt", mime_type="text/plain", data=b"a" * 6),
        OutputFile(filename="b.txt", mime_type="text/plain", data=b"b" * 6),
        OutputFile(filename="c.txt", mime_type="text/plain", data=b"c" * 4),
    )

    content, skipped = _skips_for(monkeypatch, tmp_path, hook, max_total_bytes=10)

    assert "[a.txt](" in content and "[c.txt](" in content and "b.txt" not in content
    assert [(e["reason"], e["index"]) for e in skipped] == [("turn_total_too_large", 1)]


@pytest.mark.parametrize(
    "overrides", [{"max_files_per_turn": 0}, {"max_total_bytes": 0}]
)
def test_output_files_refuses_bad_per_turn_caps(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, _Hook(), **overrides)


async def test_the_local_store_hands_back_an_open_file_not_its_bytes(
    tmp_path: Path,
) -> None:
    store = LocalDirectoryFileStore(tmp_path / "files")
    token = await store.put(b"abc", filename="a.txt", mime_type="text/plain", ttl_s=60)

    stored = await store.get(token)

    assert stored is not None and stored.data is None and stored.size == 3
    for path in (tmp_path / "files").iterdir():
        path.unlink()  # an open handle survives the expiry sweep
    assert _read(stored) == b"abc"


def test_a_download_is_streamed_with_its_length_and_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = os.urandom(300_000)
    hook = _Hook(
        OutputFile(filename="big.bin", mime_type="application/zip", data=payload)
    )
    client = TestClient(
        _app(_fake_runtime(), monkeypatch, output_files=_config(tmp_path, hook))
    )
    url, _ = URL_RE.search(_content(_post(client))).groups()  # type: ignore[union-attr]

    download = client.get(url)

    assert download.content == payload
    assert download.headers["content-length"] == str(len(payload))
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"
