"""Load-time coverage for ADR-002 C.12 declarative `command_tools`.

Strict TDD: written to fail until `harness/loader.py` parses and validates
`command_tools:` manifest entries. Mirrors `test_harness_loader.py` /
`test_harness_loader_edge.py`'s conventions — direct unit tests against the
internal parsing helpers for the per-entry shape rules, and fixture-driven
`resolve()` tests for the end-to-end / deployment-override behaviour.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from agents_system.harness.loader import (
    CommandToolParam,
    DefinitionError,
    Tier,
    _parse_command_tools,
    _validate_argv_template,
)

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_FIXTURE_BASE = _REPO_ROOT / "tests" / "fixtures" / "agents" / "command_tools"
_SOURCE = pathlib.Path("manifest.md")  # only used for error messages below


def _string_param(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": "string"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# _validate_argv_template — the whole-element placeholder rule
# ---------------------------------------------------------------------------
def test_partial_element_placeholder_is_rejected() -> None:
    """`--flag={x}` must fail to load — a partial-element placeholder is how
    option injection sneaks in (ADR-002 C.12)."""
    params = {"x": CommandToolParam(type="string")}

    with pytest.raises(DefinitionError, match="partial-element|WHOLE"):
        _validate_argv_template(
            ["/usr/bin/echo", "--flag={x}"],
            params,
            tool_name="bad",
            source=_SOURCE,
        )


def test_whole_element_placeholder_is_accepted() -> None:
    params = {"sku": CommandToolParam(type="string")}

    argv = _validate_argv_template(
        ["/usr/bin/echo", "{sku}"], params, tool_name="ok", source=_SOURCE
    )

    assert argv == ("/usr/bin/echo", "{sku}")


def test_argv0_may_not_be_a_placeholder() -> None:
    params = {"bin": CommandToolParam(type="string")}

    with pytest.raises(DefinitionError, match="argv\\[0\\]"):
        _validate_argv_template(
            ["{bin}", "--format", "json"], params, tool_name="bad", source=_SOURCE
        )


def test_unknown_placeholder_has_no_matching_param_raises() -> None:
    with pytest.raises(DefinitionError, match="no matching param"):
        _validate_argv_template(
            ["/usr/bin/echo", "{sku}"], {}, tool_name="bad", source=_SOURCE
        )


def test_unused_declared_param_raises() -> None:
    params = {
        "sku": CommandToolParam(type="string"),
        "unused": CommandToolParam(type="string"),
    }

    with pytest.raises(DefinitionError, match="never appear"):
        _validate_argv_template(
            ["/usr/bin/echo", "{sku}"], params, tool_name="bad", source=_SOURCE
        )


def test_argv_must_be_a_non_empty_list_of_strings() -> None:
    with pytest.raises(DefinitionError, match="non-empty list"):
        _validate_argv_template("not-a-list", {}, tool_name="bad", source=_SOURCE)


# ---------------------------------------------------------------------------
# argv[0] resolution to an absolute path at load time
# ---------------------------------------------------------------------------
def test_argv0_already_absolute_is_kept_as_is() -> None:
    argv = _validate_argv_template(
        ["/usr/bin/echo", "literal"], {}, tool_name="ok", source=_SOURCE
    )
    assert argv[0] == "/usr/bin/echo"


def test_argv0_bare_name_resolves_on_path() -> None:
    argv = _validate_argv_template(
        ["echo", "literal"], {}, tool_name="ok", source=_SOURCE
    )
    assert pathlib.Path(argv[0]).is_absolute()
    assert argv[0].endswith("echo")


def test_argv0_unresolvable_name_raises() -> None:
    with pytest.raises(DefinitionError, match="not an absolute path"):
        _validate_argv_template(
            ["definitely-not-a-real-binary-xyz", "literal"],
            {},
            tool_name="bad",
            source=_SOURCE,
        )


# ---------------------------------------------------------------------------
# _parse_command_tools — the full per-entry manifest shape
# ---------------------------------------------------------------------------
def test_parse_command_tools_absent_key_returns_empty() -> None:
    assert _parse_command_tools({}, source=_SOURCE) == []


def test_parse_command_tools_valid_entry() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "check_stock",
                "argv": ["/usr/bin/echo", "--sku", "{sku}"],
                "params": {
                    "sku": _string_param(pattern="^[A-Za-z0-9_-]{1,32}$", max_length=32)
                },
                "tier": "T2",
                "permission": "run:check_stock",
            }
        ]
    }

    declarations = _parse_command_tools(manifest_fm, source=_SOURCE)

    assert len(declarations) == 1
    decl = declarations[0]
    assert decl.name == "check_stock"
    assert decl.argv == ("/usr/bin/echo", "--sku", "{sku}")
    assert decl.tier == Tier.T2
    assert decl.permission == "run:check_stock"
    assert decl.params["sku"].pattern == "^[A-Za-z0-9_-]{1,32}$"
    assert decl.params["sku"].max_length == 32


def test_parse_command_tools_duplicate_name_raises() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "dup",
                "argv": ["/usr/bin/echo"],
                "tier": "T2",
                "permission": "run:dup",
            },
            {
                "name": "dup",
                "argv": ["/usr/bin/true"],
                "tier": "T2",
                "permission": "run:dup",
            },
        ]
    }

    with pytest.raises(DefinitionError, match="more than once"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


def test_parse_command_tools_invalid_tier_raises() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "bad_tier",
                "argv": ["/usr/bin/echo"],
                "tier": "T9",
                "permission": "run:bad_tier",
            }
        ]
    }

    with pytest.raises(DefinitionError, match="tier"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


@pytest.mark.parametrize("tier", ["T0", "T1"])
def test_parse_command_tools_below_t2_tier_rejected(tier: str) -> None:
    """PR #147 review follow-up — a run:* permission below T2 is never
    revalidated at call time (interceptor._is_sensitive), which would let an
    untrusted_input role reach an unrevalidated host command. Rejected at
    load, naming the offending tool."""
    manifest_fm = {
        "command_tools": [
            {
                "name": "too_low_tier",
                "argv": ["/usr/bin/echo", "{sku}"],
                "params": {"sku": _string_param(pattern="^[A-Za-z0-9_-]{1,32}$")},
                "tier": tier,
                "permission": "run:too_low_tier",
            }
        ]
    }

    with pytest.raises(DefinitionError, match="too_low_tier"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


def test_parse_command_tools_t2_tier_is_accepted() -> None:
    """T2 stays allowed — ADR-002 C.12's own worked example uses it."""
    manifest_fm = {
        "command_tools": [
            {
                "name": "check_stock",
                "argv": ["/usr/bin/echo", "{sku}"],
                "params": {"sku": _string_param(pattern="^[A-Za-z0-9_-]{1,32}$")},
                "tier": "T2",
                "permission": "run:check_stock",
            }
        ]
    }

    declarations = _parse_command_tools(manifest_fm, source=_SOURCE)

    assert declarations[0].tier == Tier.T2


# ---------------------------------------------------------------------------
# Narrowness is required, not opt-in — PR #147 review follow-up. A string
# param with no `pattern`/`enum` let the reviewer read `/etc/hostname`
# through a `check_stock`-shaped tool; every string param must now declare
# one, and `max_length` (when given at all) is capped.
# ---------------------------------------------------------------------------
def test_string_param_without_pattern_or_enum_rejected_at_load() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "unconstrained",
                "argv": ["/usr/bin/echo", "{path}"],
                "params": {"path": {"type": "string"}},
                "tier": "T2",
                "permission": "run:unconstrained",
            }
        ]
    }

    with pytest.raises(DefinitionError, match="pattern.*enum|enum.*pattern"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


def test_string_param_with_only_pattern_is_accepted() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "ok_pattern",
                "argv": ["/usr/bin/echo", "{sku}"],
                "params": {"sku": _string_param(pattern="^[A-Za-z0-9_-]{1,32}$")},
                "tier": "T2",
                "permission": "run:ok_pattern",
            }
        ]
    }

    declarations = _parse_command_tools(manifest_fm, source=_SOURCE)

    assert declarations[0].params["sku"].pattern is not None


def test_string_param_with_only_enum_is_accepted() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "ok_enum",
                "argv": ["/usr/bin/echo", "{status}"],
                "params": {"status": _string_param(enum=["open", "closed"])},
                "tier": "T2",
                "permission": "run:ok_enum",
            }
        ]
    }

    declarations = _parse_command_tools(manifest_fm, source=_SOURCE)

    assert declarations[0].params["status"].enum == ("open", "closed")


def test_max_length_beyond_platform_cap_rejected() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "too_long",
                "argv": ["/usr/bin/echo", "{sku}"],
                "params": {
                    "sku": _string_param(pattern="^[A-Za-z0-9_-]+$", max_length=100_000)
                },
                "tier": "T2",
                "permission": "run:too_long",
            }
        ]
    }

    with pytest.raises(DefinitionError, match="max_length"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


def test_max_length_within_platform_cap_is_accepted() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "ok_length",
                "argv": ["/usr/bin/echo", "{sku}"],
                "params": {
                    "sku": _string_param(pattern="^[A-Za-z0-9_-]+$", max_length=64)
                },
                "tier": "T2",
                "permission": "run:ok_length",
            }
        ]
    }

    declarations = _parse_command_tools(manifest_fm, source=_SOURCE)

    assert declarations[0].params["sku"].max_length == 64


@pytest.mark.parametrize(
    "permission", ["exec:bad_tool", "read:bad_tool", "write:bad_tool"]
)
def test_parse_command_tools_permission_must_start_with_run(permission: str) -> None:
    """The permission family is what keeps `command_tools` out of C.11's
    `exec:*` mutual-exclusion invariant — enforced at load time, not left to
    convention."""
    manifest_fm = {
        "command_tools": [
            {
                "name": "bad_perm",
                "argv": ["/usr/bin/echo"],
                "tier": "T2",
                "permission": permission,
            }
        ]
    }

    with pytest.raises(DefinitionError, match="run:"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


def test_parse_command_tools_param_type_must_be_known() -> None:
    manifest_fm = {
        "command_tools": [
            {
                "name": "bad_param_type",
                "argv": ["/usr/bin/echo", "{x}"],
                "params": {"x": {"type": "float"}},
                "tier": "T1",
                "permission": "run:bad_param_type",
            }
        ]
    }

    with pytest.raises(DefinitionError, match="type"):
        _parse_command_tools(manifest_fm, source=_SOURCE)


# ---------------------------------------------------------------------------
# End-to-end via resolve() against real fixture files
# ---------------------------------------------------------------------------
def _cmdtool_roots() -> Any:
    from agents_system.harness.loader import RootConfig

    return RootConfig(
        platform_root=_FIXTURE_BASE,
        deployments_root=_FIXTURE_BASE / "deployments",
    )


def test_resolve_generic_role_carries_command_tools() -> None:
    from agents_system.harness.loader import resolve

    definition = resolve("cmdtool-role", roots=_cmdtool_roots())

    assert len(definition.command_tools) == 1
    assert definition.command_tools[0].name == "check_stock"
    assert definition.command_tools[0].permission == "run:check_stock"


def test_untrusted_input_role_with_only_command_tools_resolves_cleanly() -> None:
    """ADR-002 C.12 acceptance criterion: an `untrusted_input` role holding
    only `command_tools`-declared tools (permission family `run:`, never
    `exec:*`) must pass C.11's mutual-exclusion invariant check."""
    from agents_system.harness.loader import resolve

    definition = resolve("cmdtool-role", roots=_cmdtool_roots())

    assert definition.untrusted_input is True
    assert definition.command_tools[0].permission.startswith("run:")


def test_deployment_override_may_keep_a_declared_command_tool() -> None:
    from agents_system.harness.loader import resolve

    definition = resolve("cmdtool-role", client="keep-subset", roots=_cmdtool_roots())

    assert {d.name for d in definition.command_tools} == {"check_stock"}


def test_deployment_override_adding_undeclared_command_tool_raises() -> None:
    from agents_system.harness.loader import resolve

    with pytest.raises(DefinitionError, match="command_tools"):
        resolve("cmdtool-role", client="add-extra", roots=_cmdtool_roots())
