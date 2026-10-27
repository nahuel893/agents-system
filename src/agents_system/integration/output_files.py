"""Files an OpenAI-compatible adapter turn produced (issue #319).

A channel turn can deliver the files it produced (a spreadsheet, a chart)
because the application owns delivery there. A ``/v1/chat/completions`` turn
cannot: chat completions has no field for assistant files, so the adapter
used to answer the text alone. This module closes the gap.

An application passes ``create_app(output_files=OutputFiles(...))``. After
each adapter turn, the adapter awaits ``OutputFiles.hook`` with the same
``AdapterRequestInfo`` the request-context hook gets (#249) and the turn's
messages, and appends the files it returns to the assistant message as
Markdown a standard client renders:

- an image at or below ``inline_image_max_bytes`` as an inline ``data:`` URI;
- anything else as a link to ``GET /v1/files/{token}``, a download served
  without the bearer key: the token is the capability. Tokens come from
  ``secrets``, expire after ``ttl_s``, and are stored only as a hash, so an
  unknown, expired or malformed token all answer the same 404.

Files are kept behind ``OutputFileStore``, a small port; the default is
``LocalDirectoryFileStore``. Nothing in this module depends on the web stack,
so a hook can be written and tested without the ``server`` extra.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import quote

import structlog
from langchain_core.messages import AnyMessage

from agents_system.integration.request_context import AdapterRequestInfo

DEFAULT_INLINE_IMAGE_MAX_BYTES = 256 * 1024
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_TTL_S = 3600.0
DEFAULT_MAX_FILES_PER_TURN = 10
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024

# type/subtype only: no parameters, nothing that could end a header or a
# data: URI early.
_MIME_RE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
)
# secrets.token_urlsafe(32) is 43 characters; anything else is not ours.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_\[\]()<>!#|~])")
# The rendered-file patterns run over client-sent history, so they must stay
# linear on hostile input (#319 review, B1). A match starts only at an
# unescaped `[` (or `![`), a label cannot contain an unescaped bracket and is
# bounded in length, and a URL stops at the first bracket, so no two match
# attempts scan the same text. Labels from `markdown_label` escape every
# bracket, so the adapter's own output always matches.
_LABEL_MAX = 1000
_LABEL = rf"((?:[^\[\]\\\n]|\\.){{0,{_LABEL_MAX}}})"
_INLINE_IMAGE_RE = re.compile(r"(?<!\\)!\[" + _LABEL + r"\]\(data:[^)\s\[\]]*\)")
_DOWNLOAD_LINK_RE = re.compile(
    r"(?:(?<!\\)!|(?<![\\!]))\["
    + _LABEL
    + r"\]\([^)\s\[\]]*?/v1/files/[A-Za-z0-9_-]+\)"
)
_DOWNLOAD_PATH_RE = re.compile(r"/v1/files/[A-Za-z0-9_-]+")
# The store's own names: a SHA-256 key, then .json, .bin or a temp file.
_RECORD_RE = re.compile(r"([0-9a-f]{64})\.json")
_TMP_RE = re.compile(r"[0-9a-f]{64}\.(?:json|bin)\.tmp")


@dataclass(frozen=True)
class OutputFile:
    """One file a turn produced, as the application's hook hands it over.

    Exactly one of ``data`` (the bytes) or ``path`` (a file the adapter reads)
    is set. ``filename`` is shown to the user and offered as the download
    name; it is never used on disk. ``mime_type`` is a bare ``type/subtype``
    (no parameters). ``caption`` is optional text shown with the file.
    """

    filename: str
    mime_type: str
    data: bytes | None = None
    path: Path | str | None = None
    caption: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not self.filename.strip():
            raise ValueError("OutputFile.filename must be non-empty text")
        if not isinstance(self.mime_type, str) or not _MIME_RE.fullmatch(
            self.mime_type.lower()
        ):
            raise ValueError("OutputFile.mime_type must be a bare type/subtype")
        object.__setattr__(self, "mime_type", self.mime_type.lower())
        if (self.data is None) == (self.path is None):
            raise ValueError("OutputFile takes exactly one of data or path")
        if self.data is not None and not isinstance(self.data, bytes):
            raise TypeError("OutputFile.data must be bytes")
        if self.path is not None:
            object.__setattr__(self, "path", Path(self.path))
        if self.caption is not None and not isinstance(self.caption, str):
            raise TypeError("OutputFile.caption must be text or None")


OutputFilesHook = Callable[
    [AdapterRequestInfo, Sequence[AnyMessage]],
    Awaitable[Sequence[OutputFile] | None],
]
"""Returns the files one adapter turn produced, or ``None`` for none.

Called after the turn finished, with the request's ``AdapterRequestInfo`` and
the turn's messages. Raising, or returning anything but a sequence of
``OutputFile``, logs ``adapter.output_files_failed`` and the reply carries
the text alone: the turn already ran and its cost is spent.
"""


@dataclass(frozen=True)
class StoredFile:
    """A file a store hands back for a download.

    Exactly one of ``data`` (the bytes) or ``file`` (a handle open for
    reading, which the download streams and then closes) is set. ``size`` is
    the length in bytes.
    """

    filename: str
    mime_type: str
    size: int
    data: bytes | None = None
    file: BinaryIO | None = None

    def __post_init__(self) -> None:
        if (self.data is None) == (self.file is None):
            raise ValueError("StoredFile takes exactly one of data or file")


@runtime_checkable
class OutputFileStore(Protocol):
    """Where linked output files wait for their download.

    ``put`` stores the bytes and returns a new unguessable token; ``get``
    returns the file for a live token, or ``None`` for an unknown, expired or
    malformed one, without telling which; ``cleanup`` deletes expired files
    and returns how many it removed.
    """

    async def put(
        self, data: bytes, *, filename: str, mime_type: str, ttl_s: float
    ) -> str: ...

    async def get(self, token: str) -> StoredFile | None: ...

    async def cleanup(self) -> int: ...


class LocalDirectoryFileStore:
    """The default store: one directory on local disk, with TTL cleanup.

    Each file is written under a server-chosen name, the SHA-256 of its
    token, never the client filename or the token itself: a directory listing
    reveals no token, and a lookup never compares a token byte by byte.
    Expired files are deleted when looked up and by ``cleanup``, which ``put``
    also runs at most once every ``cleanup_interval_s``. ``cleanup`` touches
    only the store's own names, so a directory that holds anything else keeps
    it; temp files a crashed write left behind go once older than
    ``stale_tmp_s``. Several processes can share the directory. ``get`` hands
    back an open file, so a download streams from disk and survives the
    file's removal mid-download.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        cleanup_interval_s: float = 60.0,
        stale_tmp_s: float = DEFAULT_TTL_S,
    ) -> None:
        self._directory = Path(directory)
        self._clock = clock
        self._cleanup_interval_s = cleanup_interval_s
        self._stale_tmp_s = stale_tmp_s
        self._next_cleanup = float("-inf")

    async def put(
        self, data: bytes, *, filename: str, mime_type: str, ttl_s: float
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        meta = {"filename": filename, "mime_type": mime_type, "expires_at": now + ttl_s}
        await asyncio.to_thread(self._write, _key(token), data, meta)
        if now >= self._next_cleanup:
            self._next_cleanup = now + self._cleanup_interval_s
            await self.cleanup()
        return token

    async def get(self, token: str) -> StoredFile | None:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return None
        return await asyncio.to_thread(self._read, _key(token), self._clock())

    async def cleanup(self) -> int:
        return await asyncio.to_thread(self._cleanup, self._clock())

    def _write(self, key: str, data: bytes, meta: dict[str, Any]) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Metadata first: a crash between the two writes leaves a record
        # cleanup can still expire, never an orphan payload it cannot.
        _write_private(self._directory / f"{key}.json", json.dumps(meta).encode())
        _write_private(self._directory / f"{key}.bin", data)

    def _read(self, key: str, now: float) -> StoredFile | None:
        meta = self._meta(key)
        if meta is None or now >= meta["expires_at"]:
            self._remove(key)
            return None
        try:
            handle = (self._directory / f"{key}.bin").open("rb")
        except OSError:
            return None
        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            handle.close()
            return None
        return StoredFile(
            filename=meta["filename"],
            mime_type=meta["mime_type"],
            size=size,
            file=handle,
        )

    def _meta(self, key: str) -> dict[str, Any] | None:
        try:
            meta = json.loads((self._directory / f"{key}.json").read_bytes())
        except (OSError, ValueError):
            return None
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("filename"), str)
            or not isinstance(meta.get("mime_type"), str)
            or not isinstance(meta.get("expires_at"), int | float)
        ):
            return None
        return meta

    def _cleanup(self, now: float) -> int:
        removed = 0
        try:
            names = [entry.name for entry in os.scandir(self._directory)]
        except OSError:
            return 0
        for name in names:
            record = _RECORD_RE.fullmatch(name)
            if record is not None:
                key = record.group(1)
                meta = self._meta(key)
                if meta is None or now >= meta["expires_at"]:
                    self._remove(key)
                    removed += 1
            elif _TMP_RE.fullmatch(name):
                path = self._directory / name
                try:
                    if now - path.stat().st_mtime > self._stale_tmp_s:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
        return removed

    def _remove(self, key: str) -> None:
        for suffix in (".bin", ".json"):
            try:
                (self._directory / f"{key}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


@dataclass(frozen=True)
class OutputFiles:
    """The adapter's output-file configuration, for ``create_app(output_files=...)``.

    ``hook`` returns each turn's files. ``store`` keeps linked files until
    they expire. An image at or below ``inline_image_max_bytes`` is inlined
    (``0`` links every image). A file above ``max_file_bytes``, beyond the
    first ``max_files_per_turn``, or that would take the turn past
    ``max_total_bytes`` is left out and logged. A link expires ``ttl_s``
    seconds after the reply. Links are built from the request's base URL
    (its ``Host`` header), or from ``public_base_url``, which production
    should set.
    """

    hook: OutputFilesHook
    store: OutputFileStore
    inline_image_max_bytes: int = DEFAULT_INLINE_IMAGE_MAX_BYTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    ttl_s: float = DEFAULT_TTL_S
    public_base_url: str | None = None
    max_files_per_turn: int = DEFAULT_MAX_FILES_PER_TURN
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        if not callable(self.hook):
            raise TypeError("OutputFiles.hook must be an async callable")
        if not isinstance(self.store, OutputFileStore):
            raise TypeError("OutputFiles.store must implement OutputFileStore")
        if not _is_int(self.inline_image_max_bytes) or self.inline_image_max_bytes < 0:
            raise ValueError("OutputFiles.inline_image_max_bytes must be >= 0")
        if not _is_int(self.max_file_bytes) or self.max_file_bytes <= 0:
            raise ValueError("OutputFiles.max_file_bytes must be > 0")
        if not isinstance(self.ttl_s, int | float) or not self.ttl_s > 0:
            raise ValueError("OutputFiles.ttl_s must be > 0")
        if not _is_int(self.max_files_per_turn) or self.max_files_per_turn <= 0:
            raise ValueError("OutputFiles.max_files_per_turn must be > 0")
        if not _is_int(self.max_total_bytes) or self.max_total_bytes <= 0:
            raise ValueError("OutputFiles.max_total_bytes must be > 0")
        if self.public_base_url is not None:
            if not isinstance(self.public_base_url, str) or not re.match(
                r"https?://[^\s/]+", self.public_base_url
            ):
                raise ValueError("OutputFiles.public_base_url must be an http(s) URL")
            object.__setattr__(
                self, "public_base_url", self.public_base_url.rstrip("/")
            )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def markdown_label(text: str) -> str:
    """``text`` made safe as Markdown link or image text: on one line, with
    every character that could open or close markup backslash-escaped, and
    short enough that ``strip_rendered_files`` always recognizes it."""
    one_line = " ".join(text.split())[:200]
    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", one_line)


def strip_rendered_files(content: str) -> str:
    """Take the files the adapter rendered back out of ``content``.

    A client sends the whole history back on the next request. Without this,
    an inline image would reach the model again as base64 text, and a
    download link would hand a live token to the model provider. An inline
    image becomes ``[image: label]``, a download link ``[file: label]``, and
    any other download path loses its token.
    """
    content = _INLINE_IMAGE_RE.sub(r"[image: \1]", content)
    content = _DOWNLOAD_LINK_RE.sub(r"[file: \1]", content)
    return _DOWNLOAD_PATH_RE.sub("/v1/files/<redacted>", content)


def append_to_content(text: str, parts: Sequence[str]) -> str:
    """The assistant text followed by the rendered files, one per paragraph."""
    return "\n\n".join([text, *parts] if text else parts)


def validated_files(result: object) -> list[OutputFile]:
    """The hook's result as a list, or ``TypeError`` for anything but ``None``
    or a sequence of ``OutputFile``."""
    if result is None:
        return []
    if isinstance(result, str | bytes) or not isinstance(result, Sequence):
        raise TypeError(
            f"output files hook returned {type(result).__name__}, "
            "expected a sequence of OutputFile or None"
        )
    files = list(result)
    for item in files:
        if not isinstance(item, OutputFile):
            raise TypeError(
                f"output files hook returned a {type(item).__name__}, "
                "expected OutputFile"
            )
    return files


async def render_output_files(
    config: OutputFiles,
    files: Sequence[OutputFile],
    *,
    base_url: str,
    log_fields: dict[str, Any],
) -> list[str]:
    """Render ``files`` as Markdown, storing every one that is linked.

    A file over a limit, unreadable, not a regular file, or that the store
    refused is left out and logged as ``adapter.output_file_skipped`` with
    its ``reason``; the others still render.
    """
    log = structlog.get_logger(__name__)
    parts: list[str] = []
    total = 0
    for index, file in enumerate(files):
        fields = {**log_fields, "index": index, "mime_type": file.mime_type}
        if index >= config.max_files_per_turn:
            log.warning(
                "adapter.output_file_skipped",
                reason="too_many_files",
                limit=config.max_files_per_turn,
                **fields,
            )
            continue
        try:
            data, size = await _load(file, config.max_file_bytes)
        except _NotRegularFile:
            log.warning(
                "adapter.output_file_skipped", reason="not_a_regular_file", **fields
            )
            continue
        except OSError:
            log.warning("adapter.output_file_skipped", reason="unreadable", **fields)
            continue
        if data is None:
            log.warning(
                "adapter.output_file_skipped",
                reason="too_large",
                size=size,
                limit=config.max_file_bytes,
                **fields,
            )
            continue
        if total + size > config.max_total_bytes:
            log.warning(
                "adapter.output_file_skipped",
                reason="turn_total_too_large",
                size=size,
                limit=config.max_total_bytes,
                **fields,
            )
            continue
        total += size
        part = await _render(config, file, data, base_url, log, fields)
        if part:
            parts.append(part)
    return parts


class _NotRegularFile(Exception):
    """The hook's path is a symlink, a device, a FIFO or anything but a
    regular file."""


async def _load(file: OutputFile, limit: int) -> tuple[bytes | None, int]:
    """The file's bytes and size; ``None`` bytes when it is over ``limit``."""
    if file.data is not None:
        size = len(file.data)
        return (file.data if size <= limit else None), size
    return await asyncio.to_thread(_read_regular_file, Path(file.path or ""), limit)


def _read_regular_file(path: Path, limit: int) -> tuple[bytes | None, int]:
    """Read at most ``limit + 1`` bytes of a regular file, never a symlink.

    The reported size is not trusted (``/proc`` files report 0), so the read
    itself is bounded. ``O_NONBLOCK`` keeps a FIFO from blocking the open;
    the ``S_ISREG`` check then refuses it, a device or a socket. Only the
    last path component is checked for a symlink: a hook resolves its paths
    under a root it owns.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise _NotRegularFile
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _NotRegularFile from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotRegularFile
        chunks: list[bytes] = []
        read = 0
        while read <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - read))
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
    finally:
        os.close(fd)
    return (b"".join(chunks) if read <= limit else None), read


async def _render(
    config: OutputFiles,
    file: OutputFile,
    data: bytes,
    base_url: str,
    log: Any,
    fields: dict[str, Any],
) -> str | None:
    is_image = file.mime_type.startswith("image/")
    label = markdown_label(file.caption or file.filename)
    if is_image and len(data) <= config.inline_image_max_bytes:
        encoded = base64.b64encode(data).decode("ascii")
        return f"![{label}](data:{file.mime_type};base64,{encoded})"
    try:
        token = await config.store.put(
            data, filename=file.filename, mime_type=file.mime_type, ttl_s=config.ttl_s
        )
    except Exception:
        log.exception("adapter.output_file_skipped", reason="store_failed", **fields)
        return None
    url = f"{base_url}/v1/files/{token}"
    if is_image:
        return f"![{label}]({url})"
    link = f"[{markdown_label(file.filename)}]({url})"
    return f"{link} - {markdown_label(file.caption)}" if file.caption else link


def content_disposition(filename: str) -> str:
    """An ``attachment`` header value that cannot break the header: an ASCII
    fallback name plus the RFC 5987 UTF-8 form."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")[:200] or "download"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(name[:200] or 'download', safe='')}"
    )
