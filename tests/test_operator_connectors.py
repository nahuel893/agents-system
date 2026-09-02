"""The two basic operator tools, and the boundaries that make them shippable.

`use_term` is arbitrary code execution and `read_file` is arbitrary data
exfiltration from the host. Both fail CLOSED: an unconfigured policy runs
nothing and reads nothing, so a deployment that forgets to configure them
gets an inert tool rather than an open one.

The tests that matter here are the escape attempts. A happy-path test proves
the tool works; only these prove it is safe to have.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from agentsys.connectors.operator import (
    TerminalPolicy,
    build_file_reader_connector,
    build_terminal_connector,
)


def _policy(root: pathlib.Path, **kw: object) -> TerminalPolicy:
    defaults: dict[str, object] = {
        "root": root,
        "allowed_commands": frozenset({sys.executable, "echo"}),
    }
    defaults.update(kw)
    return TerminalPolicy(**defaults)  # type: ignore[arg-type]


# --- use_term: fails closed ------------------------------------------------


async def test_an_unconfigured_allowlist_runs_nothing(tmp_path: pathlib.Path) -> None:
    """The default is inert, not open.

    A deployment that declares the tool and forgets the policy must get a
    refusal, never a shell. This is the single most important assertion in
    the file.
    """
    connector = build_terminal_connector(
        TerminalPolicy(root=tmp_path, allowed_commands=frozenset())
    )

    result = await connector({"argv": ["echo", "hello"]})

    assert "error" in result
    assert result.get("error_kind") == "command_not_allowed"
    assert "stdout" not in result


async def test_a_command_outside_the_allowlist_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["rm", "-rf", "/"]})

    assert result.get("error_kind") == "command_not_allowed"
    assert "stdout" not in result


async def test_shell_metacharacters_are_not_interpreted(
    tmp_path: pathlib.Path,
) -> None:
    """No shell. `;` and `&&` must be argv, never operators.

    Running through a shell would make the allowlist meaningless: `echo` is
    allowed, and `echo x; rm -rf /` starts with `echo`.
    """
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["echo", "safe; rm -rf /tmp && whoami"]})

    assert result["exit_code"] == 0
    # The metacharacters came back as literal text, so nothing executed them.
    assert "safe; rm -rf /tmp && whoami" in result["stdout"]


async def test_a_string_command_is_refused(tmp_path: pathlib.Path) -> None:
    """`argv` must be a list. A bare string is how a shell sneaks back in."""
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": "echo hello"})

    assert result.get("error_kind") == "invalid_argv"


async def test_the_command_runs_rooted_at_the_policy_root(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector(
        {"argv": [sys.executable, "-c", "import os; print(os.getcwd())"]}
    )

    assert str(tmp_path.resolve()) in result["stdout"]


async def test_a_command_that_hangs_is_killed(tmp_path: pathlib.Path) -> None:
    connector = build_terminal_connector(_policy(tmp_path, timeout_s=0.5))

    result = await connector(
        {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    )

    assert result.get("error_kind") == "timeout"


async def test_output_is_truncated(tmp_path: pathlib.Path) -> None:
    """An unbounded stdout is a denial of service against the model's context."""
    connector = build_terminal_connector(_policy(tmp_path, max_output_bytes=64))

    result = await connector({"argv": [sys.executable, "-c", "print('x' * 10000)"]})

    assert len(result["stdout"]) <= 64
    assert result["truncated"] is True


# --- read_file: rooted, no escapes -----------------------------------------


async def test_reads_a_file_under_the_root(tmp_path: pathlib.Path) -> None:
    (tmp_path / "notes.txt").write_text("hola", encoding="utf-8")
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "notes.txt"})

    assert result["content"] == "hola"


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.txt",
        "sub/../../outside.txt",
        "/etc/passwd",
        "./././../outside.txt",
    ],
)
async def test_paths_outside_the_root_are_refused(
    tmp_path: pathlib.Path, escape: str
) -> None:
    """Traversal, absolute paths and normalisation tricks all refused.

    Checked after resolution, not by string matching: `sub/../../outside.txt`
    contains no leading `..` and still escapes.
    """
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": escape})

    assert result.get("error_kind") == "path_outside_root"
    assert "content" not in result


async def test_a_symlink_pointing_outside_the_root_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """The escape a pure string check cannot see.

    `link.txt` is under the root by every textual measure and resolves
    somewhere else entirely.
    """
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(secret)
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "link.txt"})

    assert result.get("error_kind") == "path_outside_root"
    assert "content" not in result


async def test_a_large_file_is_truncated(tmp_path: pathlib.Path) -> None:
    (tmp_path / "big.txt").write_text("y" * 10000, encoding="utf-8")
    connector = build_file_reader_connector(_policy(tmp_path, max_output_bytes=100))

    result = await connector({"path": "big.txt"})

    assert len(result["content"]) <= 100
    assert result["truncated"] is True


async def test_a_missing_file_is_an_error_not_an_empty_read(
    tmp_path: pathlib.Path,
) -> None:
    """Empty and absent must not look the same to the model."""
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "nope.txt"})

    assert result.get("error_kind") == "not_found"
    assert "content" not in result
