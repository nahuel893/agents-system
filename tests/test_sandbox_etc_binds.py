"""Issue #238 -- the terminal sandbox must not bind the host `/etc` whole.

`/etc/agents-system/agents-system.env` holds the deployment's provider keys
and database credentials, so an allowed reader command (`cat`) must not be
able to print it, nor `/etc/passwd`. Only an explicit allowlist of `/etc`
entries is bound; the commands the repo's tests and reference agents use
must keep working. Real-bwrap tests skip cleanly without `bwrap`.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from agents_system.connectors import operator as operator_module
from agents_system.connectors.operator import (
    SandboxPolicy,
    TerminalPolicy,
    _bwrap_argv,
    build_terminal_connector,
)

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="needs bwrap, which is not installed"
)


def _policy(root: pathlib.Path, *commands: str, **sandbox: object) -> TerminalPolicy:
    return TerminalPolicy(
        root=root,
        allowed_commands=frozenset(commands),
        sandbox=SandboxPolicy(**sandbox),  # type: ignore[arg-type]
        timeout_s=30.0,
    )


def _binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, t in enumerate(argv) if t.startswith("--ro-bind")]


def test_the_whole_etc_is_never_bound(tmp_path: pathlib.Path) -> None:
    wrapped = _bwrap_argv(["echo"], policy=_policy(tmp_path, "echo"), bwrap="bwrap")
    binds = _binds(wrapped)
    assert "/etc" not in binds
    assert "/etc" not in operator_module._SYSTEM_RO_BINDS
    assert all(b.startswith("/etc/") for b in binds if b.startswith("/etc"))
    assert not any("passwd" in b or "agents-system" in b for b in binds)


def test_network_declared_commands_get_resolver_files_only(
    tmp_path: pathlib.Path,
) -> None:
    closed = _binds(_bwrap_argv(["x"], policy=_policy(tmp_path, "x"), bwrap="bwrap"))
    opened = _binds(
        _bwrap_argv(["x"], policy=_policy(tmp_path, "x", network=True), bwrap="bwrap")
    )
    assert "/etc/resolv.conf" not in closed
    assert "/etc/resolv.conf" in opened


@pytest.mark.parametrize("forbidden", [pathlib.Path("/etc")])
def test_extra_ro_binds_refuses_the_whole_etc(forbidden: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="forbids"):
        SandboxPolicy(extra_ro_binds=(forbidden,))


@pytest.mark.parametrize(
    "forbidden",
    [
        "/etc/agents-system",
        "/etc/agents-system/agents-system.env",
        "/etc/agents-system/x/y",
    ],
)
def test_extra_ro_binds_refuses_the_deployment_secrets_dir(forbidden: str) -> None:
    with pytest.raises(ValueError, match="agents-system"):
        SandboxPolicy(extra_ro_binds=(pathlib.Path(forbidden),))


def test_extra_ro_binds_refuses_a_symlink_into_the_secrets_dir(
    tmp_path: pathlib.Path,
) -> None:
    link = tmp_path / "link"
    link.symlink_to("/etc/agents-system")
    with pytest.raises(ValueError, match="agents-system"):
        SandboxPolicy(extra_ro_binds=(link,))


def test_extra_ro_binds_allows_a_lookalike_sibling_of_the_secrets_dir() -> None:
    SandboxPolicy(extra_ro_binds=(pathlib.Path("/etc/agents-system-docs"),))


def test_network_commands_do_not_get_etc_hosts(tmp_path: pathlib.Path) -> None:
    opened = _binds(
        _bwrap_argv(["x"], policy=_policy(tmp_path, "x", network=True), bwrap="bwrap")
    )
    assert "/etc/hosts" not in opened
    assert "/etc/resolv.conf" in opened


def test_extra_ro_binds_can_still_add_an_etc_subpath() -> None:
    SandboxPolicy(extra_ro_binds=(pathlib.Path("/etc/ssl"),))  # must not raise


@needs_bwrap
@pytest.mark.parametrize("target", ["/etc/passwd", "/etc/group", "/etc/os-release"])
async def test_an_allowed_reader_cannot_read_host_etc_files(
    tmp_path: pathlib.Path, target: str
) -> None:
    connector = build_terminal_connector(_policy(tmp_path, "cat"))
    result = await connector({"argv": ["cat", target]})
    assert result["exit_code"] != 0
    assert result["stdout"] == ""


@needs_bwrap
async def test_an_allowed_reader_cannot_see_the_deployment_secrets_dir(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path, "ls", "cat"))
    listing = await connector({"argv": ["ls", "/etc"]})
    assert "agents-system" not in listing["stdout"].split()
    assert "passwd" not in listing["stdout"].split()
    env = await connector({"argv": ["cat", "/etc/agents-system/agents-system.env"]})
    assert env["exit_code"] != 0
    assert env["stdout"] == ""


@needs_bwrap
async def test_a_symlink_in_the_root_to_etc_passwd_reads_nothing(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "link").symlink_to("/etc/passwd")
    connector = build_terminal_connector(_policy(tmp_path, "cat"))
    result = await connector({"argv": ["cat", "link"]})
    assert result["exit_code"] != 0
    assert result["stdout"] == ""


@needs_bwrap
async def test_the_proc_filesystem_does_not_expose_the_host(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path, "cat"))
    result = await connector({"argv": ["cat", "/proc/1/environ"]})
    # PID 1 here is the sandbox's own init, whose environment is the cleared one.
    assert "HOME=/home" not in result["stdout"]
    assert "DBUS" not in result["stdout"]


@needs_bwrap
@pytest.mark.parametrize(
    "argv",
    [["echo", "hi"], ["ls", "."], ["factor", "91"], ["sleep", "0"], ["cat", "f.txt"]],
)
async def test_the_commands_the_repo_uses_still_run(
    tmp_path: pathlib.Path, argv: list[str]
) -> None:
    (tmp_path / "f.txt").write_text("seeded\n", encoding="utf-8")
    connector = build_terminal_connector(_policy(tmp_path, argv[0]))
    result = await connector({"argv": argv})
    assert result["exit_code"] == 0, result


@needs_bwrap
async def test_a_network_command_cannot_read_etc_hosts(tmp_path: pathlib.Path) -> None:
    connector = build_terminal_connector(_policy(tmp_path, "cat", network=True))
    result = await connector({"argv": ["cat", "/etc/hosts"]})
    assert result["exit_code"] != 0
    assert result["stdout"] == ""
