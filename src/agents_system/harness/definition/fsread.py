"""Contained file reads: resolving a path inside a root and reading it, bounded in size, without following a link out of that root."""

from __future__ import annotations

import errno
import os
import pathlib
import stat
from collections.abc import Callable


def _is_absolute_value(value: str) -> bool:
    # A Windows anchor is non-empty for a POSIX root ("/"), a Windows root,
    # a drive ("C:") and a UNC share: every form a join would not keep
    # under its base, whatever OS the loader runs on.
    return bool(pathlib.PureWindowsPath(value).anchor)


def resolve_within_root(
    root: pathlib.Path, base: pathlib.Path, relative: str
) -> pathlib.Path | None:
    """Resolve ``relative`` against ``base`` (the declaring agent's folder)
    and return the real path only if it lies strictly inside ``root``.

    Containment is checked AFTER ``Path.resolve()`` has followed every
    symlink and ``..`` (design.md D2 Resolved Decision), so a sibling such
    as ``../base-support`` passes while a ``..`` chain or a symlink leading
    out of ``root`` does not. ``root`` itself is not an agent folder and is
    rejected too. The comparison is lexical on real paths, so on a
    case-insensitive filesystem a differently-cased spelling fails closed.
    Never raises: every rejection is ``None``, reported once by the caller.
    """
    if _is_absolute_value(relative):
        return None
    try:
        real_root = root.resolve()
        resolved = (base / relative).resolve()
    except (OSError, RuntimeError, ValueError):  # loop, NUL byte, ...
        return None
    if resolved == real_root or not resolved.is_relative_to(real_root):
        return None
    return resolved


#: Issue #75 — whether ``read_within_root`` can walk directory handles
#: (``openat`` with ``O_NOFOLLOW``). True on Linux and macOS. Elsewhere
#: (Windows) a read falls back to the path, after the same resolve-then-check
#: containment, with the check-to-read window documented in
#: ``docs/platform/role.md``.
_DIR_FD_READS = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


#: Issue #127 (PR 97 review follow-up) — opening a directory with `O_PATH`
#: needs only search (execute) permission on the directory itself (and on
#: every ancestor in its path), never list/read permission, because the fd is
#: used only to walk further with `dir_fd=`, not to enumerate entries. The
#: previous `O_RDONLY|O_DIRECTORY` open needs list permission too, so a
#: folder that legitimately loads with search-only permission (mode
#: 0311/0711) failed the walk with a misleading "Permission denied" that
#: reads like a containment violation instead of a permissions mismatch.
#: Linux only: macOS has no `O_PATH`, so it keeps the previous behavior
#: unchanged there.
_DIR_OPEN_FLAG = os.O_PATH if hasattr(os, "O_PATH") else os.O_RDONLY


#: Issue #127 — a role/manifest/policy/skill file above this size is refused
#: before it is read. An importer folder is a trust boundary (issue #75):
#: without this cap, a sparse file can report an apparent size far beyond
#: what it costs on disk, forcing a full in-memory read at that reported
#: size. 1 MiB comfortably fits every real role/manifest/policy/skill file.
_MAX_DEFINITION_FILE_BYTES = 1024 * 1024


#: Chunk size for `_read_bounded`'s incremental read loop -- large enough to
#: keep the syscall count reasonable for a normal-sized file, small enough
#: that an oversized file is caught shortly after crossing the cap instead
#: of being pulled fully into memory first.
_READ_CHUNK_BYTES = 65536


def _read_bounded(read: Callable[[int], bytes], *, label: str) -> bytes:
    """Read from ``read`` (a callable taking a byte count and returning up
    to that many bytes, empty at EOF -- the shape of both ``os.read`` and a
    file object's ``.read()``) in bounded chunks, raising ``OSError`` with
    ``errno.EFBIG`` the moment the running total exceeds
    ``_MAX_DEFINITION_FILE_BYTES``, without ever accumulating more than that
    plus one chunk in memory.

    This is the cap's actual enforcement. The ``stat()``/``fstat()`` size
    check kept in both ``read_within_root`` branches ahead of this call is
    only a fast-path rejection for a file already oversized at that instant
    -- a point-in-time check that a file growing between the check and the
    read would otherwise slip past, since a plain ``read()``/``read_text()``
    call has no bound of its own (issue #127 PR review, TOCTOU). Bounding
    the read itself, not just the size it reported earlier, holds regardless
    of what the file does after the check.
    """
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_DEFINITION_FILE_BYTES:
            raise OSError(errno.EFBIG, "definition file too large", label)
        chunks.append(chunk)
    return b"".join(chunks)


def read_within_root(root: pathlib.Path, path: pathlib.Path) -> str:
    """Read ``path``, a real path strictly inside ``root`` (what
    ``resolve_within_root`` returns), without following any symlink on the
    way (issue #75, TOCTOU).

    ``resolve_within_root`` checks a path at one moment and the read comes
    later: a folder swapped for a symlink in between would be followed by a
    plain ``path.read_text()``. This opens ``root`` instead and walks
    ``path``'s components one directory handle at a time, each opened with
    ``O_NOFOLLOW`` (and, on Linux, ``O_PATH`` — issue #127: search permission
    only, not list/read). A component that is a symlink by now fails the open
    instead of being followed, so the file read is always the one under
    ``root``. The file is opened the same way, non-blocking, and must be a
    regular file: a FIFO cannot stall the load, and a directory or FIFO is
    refused with its handle closed. A file already oversized at ``stat()``/
    ``fstat()`` time is rejected before it is read at all, but the cap is
    actually enforced by ``_read_bounded`` bounding the read call itself, so
    a file that grows past the cap after that check still cannot force a
    read beyond ``_MAX_DEFINITION_FILE_BYTES`` (issue #127 PR review,
    TOCTOU).

    Every refusal is an ``OSError`` (a path outside ``root``, a lexical
    ``.``/``..`` component, and invalid UTF-8 content all included); callers
    turn it into their own error. Hard links and mounts are out of scope:
    see the threat model in ``docs/platform/role.md``.
    """
    # Issue #127 — defense in depth. Every current caller pre-resolves the
    # path with `resolve_within_root` first, whose `.resolve()` already
    # collapses `.`/`..`, so this never fires today. It exists for whatever
    # calls this function next without going through that helper: the
    # `is_relative_to` check below is lexical (string comparison, not
    # filesystem-aware), so an UNRESOLVED path like
    # `root/agent/../../outside/role.md` lexically starts with `root`'s own
    # parts and would otherwise pass it, then walk right out of `root` via a
    # literal `..` directory-fd open.
    if any(part in (".", "..") for part in path.parts):
        raise PermissionError(
            errno.EACCES, "path has an unresolved '.'/'..' component", path.name
        )
    real_root = root.resolve()
    if path == real_root or not path.is_relative_to(real_root):
        raise PermissionError(errno.EACCES, "outside the importer root", path.name)
    parts = path.relative_to(real_root).parts
    if not _DIR_FD_READS:
        try:
            if path.stat().st_size > _MAX_DEFINITION_FILE_BYTES:
                raise OSError(errno.EFBIG, "definition file too large", path.name)
            with path.open("rb") as handle:
                raw = _read_bounded(handle.read, label=path.name)
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OSError(errno.EINVAL, f"not valid UTF-8 ({exc})", path.name) from exc

    directory = _DIR_OPEN_FLAG | os.O_DIRECTORY
    fd = os.open(real_root, directory)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
    finally:
        os.close(fd)
    # Checked before `open()` takes the handle: `open()` refuses a directory
    # but never closes a descriptor it was handed, so a folder with a
    # directory where a file belongs leaked one per load (PR #97 review).
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", parts[-1])
        if info.st_size > _MAX_DEFINITION_FILE_BYTES:
            raise OSError(errno.EFBIG, "definition file too large", parts[-1])
    except BaseException:
        os.close(file_fd)
        raise
    try:
        raw = _read_bounded(lambda size: os.read(file_fd, size), label=parts[-1])
    finally:
        os.close(file_fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError(errno.EINVAL, f"not valid UTF-8 ({exc})", parts[-1]) from exc
