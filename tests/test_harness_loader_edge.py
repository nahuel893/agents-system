"""Edge-case and merge-directive coverage for the loader internals.

The merge engine is deterministic and security-relevant — these tests pin down
the full directive vocabulary (inherit / add / remove / override), the
frontmatter parser edges, the value coercion, and the validation edges. They
complement test_harness_loader.py (which drives the public resolve() path).
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

from agentsys.harness.loader import (
    DefinitionError,
    RawDefinition,
    _as_str_list,
    _read_md,
    _resolve_list_directive,
    _resolve_mapping_directive,
    _resolve_permissions,
    _split_frontmatter,
    merge,
    resolve,
)


def _raw(**overrides: Any) -> RawDefinition:
    """Build a RawDefinition with sensible defaults, overriding chosen fields."""
    base: dict[str, Any] = dict(
        role_name="role",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=[],
        skills=[],
        context={},
        permissions=[],
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    base.update(overrides)
    return RawDefinition(**base)


# ---------------------------------------------------------------------------
# _split_frontmatter
# ---------------------------------------------------------------------------
def test_split_frontmatter_no_fence_returns_empty() -> None:
    fm, body = _split_frontmatter("# just markdown\nno yaml here")
    assert fm == {}
    assert "just markdown" in body


def test_split_frontmatter_unterminated_fence_returns_empty() -> None:
    fm, body = _split_frontmatter("---\nkey: value\nnever closes")
    assert fm == {}


def test_split_frontmatter_valid() -> None:
    fm, body = _split_frontmatter("---\nname: sales\n---\nSYSTEM PROMPT BODY")
    assert fm["name"] == "sales"
    assert body.strip() == "SYSTEM PROMPT BODY"


# ---------------------------------------------------------------------------
# _as_str_list
# ---------------------------------------------------------------------------
def test_as_str_list_none() -> None:
    assert _as_str_list(None) == []


def test_as_str_list_str_wraps() -> None:
    assert _as_str_list("solo") == ["solo"]


def test_as_str_list_list_coerces_elements() -> None:
    assert _as_str_list(["a", 1]) == ["a", "1"]


def test_as_str_list_scalar_fallback() -> None:
    assert _as_str_list(42) == ["42"]


# ---------------------------------------------------------------------------
# _resolve_list_directive — the full vocabulary
# ---------------------------------------------------------------------------
def test_list_absent_inherits() -> None:
    assert _resolve_list_directive(["a"], None) == ["a"]


def test_list_inherit_keyword() -> None:
    assert _resolve_list_directive(["a", "b"], "inherit") == ["a", "b"]


def test_list_plain_list_replaces() -> None:
    assert _resolve_list_directive(["a"], ["b", "c"]) == ["b", "c"]


def test_list_add_appends() -> None:
    assert _resolve_list_directive(["a"], {"inherit": True, "add": ["b"]}) == ["a", "b"]


def test_list_add_dedupes() -> None:
    assert _resolve_list_directive(["a"], {"inherit": True, "add": ["a", "b"]}) == ["a", "b"]


def test_list_remove_subtracts() -> None:
    assert _resolve_list_directive(["a", "b"], {"inherit": True, "remove": ["a"]}) == ["b"]


def test_list_override_replaces() -> None:
    assert _resolve_list_directive(["a"], {"override": ["x", "y"]}) == ["x", "y"]


def test_list_unknown_dict_falls_back_to_parent() -> None:
    assert _resolve_list_directive(["a"], {"foo": 1}) == ["a"]


# ---------------------------------------------------------------------------
# _resolve_mapping_directive
# ---------------------------------------------------------------------------
def test_mapping_absent_inherits() -> None:
    assert _resolve_mapping_directive({"a": 1}, None) == {"a": 1}


def test_mapping_inherit_keyword() -> None:
    assert _resolve_mapping_directive({"a": 1}, "inherit") == {"a": 1}


def test_mapping_plain_dict_replaces() -> None:
    assert _resolve_mapping_directive({"a": 1}, {"b": 2}) == {"b": 2}


def test_mapping_inherit_overlay_adds_conditions() -> None:
    parent = {"escalate_to": "human", "conditions": ["x"]}
    out = _resolve_mapping_directive(parent, {"inherit": True, "add": ["y"]})
    assert out["escalate_to"] == "human"
    assert out["conditions"] == ["x", "y"]


def test_mapping_inherit_removes_conditions() -> None:
    out = _resolve_mapping_directive({"conditions": ["x", "y"]}, {"inherit": True, "remove": ["x"]})
    assert out["conditions"] == ["y"]


def test_mapping_non_dict_falls_back() -> None:
    assert _resolve_mapping_directive({"a": 1}, 42) == {"a": 1}


# ---------------------------------------------------------------------------
# _read_md — missing file fails loud
# ---------------------------------------------------------------------------
def test_read_md_missing_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(DefinitionError):
        _read_md(tmp_path / "does-not-exist.md")


# ---------------------------------------------------------------------------
# merge — autonomy validation edges
# ---------------------------------------------------------------------------
def test_merge_unknown_parent_autonomy_raises() -> None:
    with pytest.raises(DefinitionError, match="autonomy"):
        merge(_raw(autonomy="weird"), _raw(deployment="d", autonomy="supervised"))


def test_merge_unknown_override_autonomy_raises() -> None:
    with pytest.raises(DefinitionError, match="autonomy"):
        merge(_raw(autonomy="supervised"), _raw(deployment="d", autonomy="weird"))


# ---------------------------------------------------------------------------
# merge — execution_limits resolution branches
# ---------------------------------------------------------------------------
def test_merge_exec_limits_inherit_keyword() -> None:
    out = merge(
        _raw(execution_limits={"max_tool_calls": 5}),
        _raw(deployment="d", execution_limits="inherit"),
    )
    assert out.execution_limits == {"max_tool_calls": 5}


def test_merge_exec_limits_unexpected_scalar_becomes_none() -> None:
    out = merge(
        _raw(execution_limits={"max_tool_calls": 5}),
        _raw(deployment="d", execution_limits=123),
    )
    assert out.execution_limits is None


# ---------------------------------------------------------------------------
# merge — context inheritance (override context empty → inherit parent)
# ---------------------------------------------------------------------------
def test_merge_empty_override_context_inherits_parent() -> None:
    out = merge(
        _raw(context={"session": True}),
        _raw(deployment="d", context={}),
    )
    assert out.context == {"session": True}


# ---------------------------------------------------------------------------
# merge — permissions subset invariant (override resolves to extra perms)
# ---------------------------------------------------------------------------
def test_merge_permissions_not_in_parent_raises() -> None:
    with pytest.raises(DefinitionError, match="permissions"):
        merge(
            _raw(permissions=["read:catalog"]),
            _raw(deployment="d", permissions=["read:catalog", "write:orders"]),
        )


# ---------------------------------------------------------------------------
# merge — execution_limits key absent from baseline is allowed (new limit)
# ---------------------------------------------------------------------------
def test_merge_exec_limits_new_key_allowed() -> None:
    out = merge(
        _raw(execution_limits=None),  # baseline = platform defaults
        _raw(deployment="d", execution_limits={"custom_budget": 5}),
    )
    assert out.execution_limits == {"custom_budget": 5}


# ---------------------------------------------------------------------------
# _resolve_permissions — non-list, non-"inherit" value falls back to parent
# ---------------------------------------------------------------------------
def test_resolve_permissions_unexpected_value_falls_back() -> None:
    assert _resolve_permissions(["a", "b"], 42) == ["a", "b"]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _resolve_mapping_directive — plain key under inherit is overlaid
# ---------------------------------------------------------------------------
def test_mapping_inherit_overlays_plain_key() -> None:
    out = _resolve_mapping_directive({"a": 1}, {"inherit": True, "escalate_to": "slack"})
    assert out == {"a": 1, "escalate_to": "slack"}


# ---------------------------------------------------------------------------
# Default roots — calling without an explicit RootConfig uses the real repo
# ---------------------------------------------------------------------------
def test_resolve_default_roots_generic() -> None:
    definition = resolve("sales-agent")  # no roots, no client → real generic
    assert definition.role_name == "sales-agent"
    assert definition.deployment is None


def test_resolve_default_roots_with_real_deployment() -> None:
    definition = resolve("sales-agent", client="badie")  # default roots
    assert definition.deployment == "badie"


def test_load_generic_default_roots() -> None:
    from agentsys.harness.loader import load_generic

    raw = load_generic("sales-agent")  # called directly, no roots → real platform/
    assert raw.role_name == "sales-agent"


def test_load_override_default_roots() -> None:
    from agentsys.harness.loader import load_override

    raw = load_override("badie", "sales-agent")  # called directly, no roots
    assert raw is not None
    assert raw.deployment == "badie"
