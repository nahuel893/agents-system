"""`extends:` resolution fails loudly and stays contained (design.md D2, PR1b).

An importer `extends:` value is resolved relative to the declaring agent's
own folder, fully resolved (symlinks followed), and accepted only when the
result lands strictly inside that agent's importer root. Bare names and
`platform/roles/<name>` resolve only in the predefined role tree. Nothing is
ever silently collapsed to a same-named predefined role.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.harness.loader import (
    AgentDefinition,
    DefinitionError,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RootConfig,
    _extends_target,
    _load_role_files,
    _platform_role_name_or_none,
    _resolve_role_chain,
    resolve,
)


def _agent(
    base: pathlib.Path, name: str, *, extends: str | None = None, policy: str = ""
) -> pathlib.Path:
    """Write a minimal, well-formed agent folder (role.md/manifest.md/policy.md)."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n{name} prose.\n', encoding="utf-8"
    )
    extends_line = f"extends: {extends}\n" if extends is not None else ""
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\n{extends_line}tools: [{name}_tool]\nskills: []\n"
        "context: {}\npermissions: []\n---\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\nexecution_limits: null\n{policy}---\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    """An importer agents root holding `vip-support` and `base-support`."""
    agents = tmp_path / "agents"
    _agent(agents, "vip-support")
    _agent(agents, "base-support")
    return agents


def _vip(root: pathlib.Path) -> FolderLocator:
    return FolderLocator(path=root / "vip-support", root=root)


def _message(value: object, *, current: object, roots: RootConfig) -> str:
    with pytest.raises(DefinitionError) as excinfo:
        _extends_target(value, current=current, roots=roots)  # type: ignore[arg-type]
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# PR1b-T1 — unit level: `_extends_target` and its helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("agent", "agent"),
        ("platform/roles/sales-agent", "sales-agent"),
        ("roles/agent", None),
        ("some/importer/path/agent", None),
        ("platform/roles/a/b", None),
        ("platform/roles/..", None),
        ("..", None),
        (".", None),
    ],
)
def test_platform_role_name_accepts_only_the_two_platform_forms(
    value: str, expected: str | None
) -> None:
    assert _platform_role_name_or_none(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_or_whitespace_value_rejected(root: pathlib.Path, value: str) -> None:
    message = _message(value, current=_vip(root), roots=RootConfig())
    assert "an empty 'extends:' value" in message
    assert str(root / "vip-support") in message


def test_non_string_value_rejected_without_repr(root: pathlib.Path) -> None:
    message = _message(["agent"], current=_vip(root), roots=RootConfig())
    assert "of type list" in message
    assert "['agent']" not in message


@pytest.mark.parametrize("value", ["agent/", "platform/roles/sales-agent/"])
def test_platform_forms_resolve_to_the_bare_role_name(
    root: pathlib.Path, value: str
) -> None:
    expected = value.rstrip("/").rsplit("/", 1)[-1]
    assert _extends_target(value, current=_vip(root), roots=RootConfig()) == expected


@pytest.mark.parametrize("value", ["base-support", "platform/roles/base-support"])
def test_platform_shaped_missing_role_never_falls_back_to_importer(
    root: pathlib.Path, value: str
) -> None:
    """D2 case 2: `<root>/base-support` exists, but a platform-shaped value
    is only ever looked up in the predefined role tree."""
    message = _message(value, current=_vip(root), roots=RootConfig())
    assert f"'extends: {value}'" in message
    assert "predefined role 'base-support'" in message


def _inline(name: str, parent: str | None = None) -> InlineLocator:
    raw = RawDefinition(
        role_name=name,
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=[],
        skills=[],
        context={},
        permissions=[],
        autonomy="",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    return InlineLocator(raw=raw, parent=parent)


@pytest.mark.parametrize("current", ["sales-agent", _inline("inline-bot")])
def test_path_value_without_importer_root_rejected(
    current: str | InlineLocator,
) -> None:
    """D2 case 3: only a folder-loaded agent has a root to resolve a path in."""
    message = _message("some/path", current=current, roots=RootConfig())
    assert "'extends: some/path'" in message
    assert "no importer root" in message


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "/", "/platform/roles/agent", "C:/Windows", "\\\\host\\share"],
)
def test_absolute_path_rejected(
    root: pathlib.Path, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2 case 4: the only syntactic rejection — no filesystem access at all."""

    def _no_fs(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("filesystem touched before the absolute-path check")

    roots, current = RootConfig(), _vip(root)
    # The patch is undone before pytest formats any failure (it uses Path too).
    with monkeypatch.context() as patched, pytest.raises(DefinitionError) as excinfo:
        for method in ("resolve", "is_dir", "exists", "stat"):
            patched.setattr(pathlib.Path, method, _no_fs)
        _extends_target(value, current=current, roots=roots)
    assert "which is an absolute path" in str(excinfo.value)


def test_dotdot_sibling_accepted(root: pathlib.Path) -> None:
    """D2 case 5a (Resolved Decision): `..` is fine while it stays inside."""
    target = _extends_target("../base-support/", current=_vip(root), roots=RootConfig())
    assert target == FolderLocator(path=(root / "base-support").resolve(), root=root)


def test_symlink_inside_root_is_followed(root: pathlib.Path) -> None:
    (root / "alias").symlink_to(root / "base-support", target_is_directory=True)
    target = _extends_target("../alias", current=_vip(root), roots=RootConfig())
    assert isinstance(target, FolderLocator)
    assert target.path == (root / "base-support").resolve()


def test_dotdot_escape_rejected(root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """D2 case 5b: rejected after resolution even though the target exists."""
    _agent(tmp_path, "outside")
    for value in ("../../outside", "../../../etc", "nested/../../../outside"):
        message = _message(value, current=_vip(root), roots=RootConfig())
        assert f"'extends: {value}'" in message
        assert "escapes the importer root" in message


def test_symlink_escape_rejected(root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """D2 case 5c: same message shape as a `..` escape."""
    _agent(tmp_path, "outside")
    (root / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    current = _vip(root)
    via_link = _message("../linked", current=current, roots=RootConfig())
    via_dotdot = _message("../../outside", current=current, roots=RootConfig())
    assert via_link.replace("../linked", "V") == via_dotdot.replace(
        "../../outside", "V"
    )


def test_importer_root_itself_rejected(root: pathlib.Path) -> None:
    message = _message("..", current=_vip(root), roots=RootConfig())
    assert "escapes the importer root" in message


def test_missing_folder_inside_root_rejected(root: pathlib.Path) -> None:
    """D2 case 6, folder half."""
    message = _message("../nope", current=_vip(root), roots=RootConfig())
    assert "'extends: ../nope'" in message
    assert "not an existing folder" in message


def test_nonexistent_path_never_collapses_to_last_segment(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Spec: a platform role named `custom-agent` exists, and the old
    last-segment stripping would have silently extended it."""
    _agent(tmp_path / "platform" / "roles", "custom-agent")
    roots = RootConfig(platform_root=tmp_path / "platform")
    value = "some/nonexistent/path/custom-agent"
    message = _message(value, current=_vip(root), roots=roots)
    assert f"'extends: {value}'" in message


def test_last_segment_collision_with_generic_agent_raises(root: pathlib.Path) -> None:
    """Spec: `agent` is a real predefined role; the importer path is not."""
    value = "some/importer/path/agent"
    message = _message(value, current=_vip(root), roots=RootConfig())
    assert f"'extends: {value}'" in message
    assert "predefined role tree" in message


def test_unambiguous_importer_path_succeeds(root: pathlib.Path) -> None:
    _agent(root / "shared", "base")
    target = _extends_target("../shared/base", current=_vip(root), roots=RootConfig())
    assert isinstance(target, FolderLocator)
    assert target.path == (root / "shared" / "base").resolve()


# ---------------------------------------------------------------------------
# PR1b-T1 — chain level: missing files, cycles, depth, inline parents
# ---------------------------------------------------------------------------


def test_parent_missing_a_required_file_names_it(root: pathlib.Path) -> None:
    """D2 case 6, file half: same wrapped shape a platform parent raises."""
    _agent(root, "partial")
    (root / "partial" / "policy.md").unlink()
    _agent(root, "child", extends="../partial")
    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(FolderLocator(path=root / "child", root=root), RootConfig())
    message = str(excinfo.value)
    assert "could not be loaded" in message
    assert "policy.md" in message


def test_importer_cycle_still_detected(root: pathlib.Path) -> None:
    """D2 case 7 (regression): `_locator_key`-based cycle detection."""
    _agent(root, "a", extends="../b")
    _agent(root, "b", extends="../a")
    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(FolderLocator(path=root / "a", root=root), RootConfig())
    message = str(excinfo.value)
    assert "inheritance cycle" in message
    assert f"folder:{(root / 'a').resolve()}" in message


def test_importer_chain_depth_still_capped(root: pathlib.Path) -> None:
    """D2 case 8 (regression): `_MAX_ROLE_CHAIN_DEPTH` still fires."""
    for i in range(10):
        _agent(root, f"deep-{i}", extends=f"../deep-{i + 1}" if i < 9 else None)
    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(
            FolderLocator(path=root / "deep-0", root=root), RootConfig()
        )
    assert "deeper than" in str(excinfo.value)


def test_nested_parent_extends_error_is_not_masked(root: pathlib.Path) -> None:
    """A parent's own bad `extends:` surfaces its real reason, not "missing"."""
    _agent(root, "mid", extends="../../escape")
    _agent(root, "leaf", extends="../mid")
    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(FolderLocator(path=root / "leaf", root=root), RootConfig())
    message = str(excinfo.value)
    assert "'extends: ../../escape'" in message
    assert "escapes the importer root" in message


def test_inline_string_parent_goes_through_extends_resolution() -> None:
    """InlineLocator.parent strings resolve through D2 like manifest values."""
    locator = _inline("inline-bot", parent="platform/roles/sales-agent")
    definition, _ = _resolve_role_chain(locator, RootConfig())
    assert "escalation_notifier" in definition.tools


# ---------------------------------------------------------------------------
# PR1b-T2 — folder integration: real manifests resolved end-to-end
# ---------------------------------------------------------------------------


def _resolve_folder(root: pathlib.Path, name: str) -> AgentDefinition:
    return resolve(FolderLocator(path=root / name, root=root), roots=RootConfig())


def test_importer_extends_generic_agent_by_bare_name(root: pathlib.Path) -> None:
    folder = _agent(root, "triage", extends="agent")
    _, parent, _ = _load_role_files(FolderLocator(path=folder, root=root), RootConfig())
    assert parent == "agent"  # the very locator a predefined role gets
    definition = _resolve_folder(root, "triage")
    assert {"triage_tool", "escalation_notifier"} <= set(definition.tools)


def test_importer_extends_predefined_role_by_path_form(root: pathlib.Path) -> None:
    _agent(root, "my-sales", extends="platform/roles/sales-agent")
    definition = _resolve_folder(root, "my-sales")
    sales = resolve("sales-agent", roots=RootConfig())
    assert definition.role_name == "my-sales"
    assert set(definition.tools) == set(sales.tools) | {"my-sales_tool"}


def test_inline_definition_extends_importer_folder(root: pathlib.Path) -> None:
    """Resolved from the folder: a platform lookup would hit a missing root."""
    parent = FolderLocator(path=root / "base-support", root=root)
    locator = InlineLocator(raw=_inline("inline-bot").raw, parent=parent)
    definition = resolve(locator, roots=RootConfig(platform_root=root / "absent"))
    assert definition.tools == ("base-support_tool",)


def test_sibling_via_dotdot_accepted(root: pathlib.Path) -> None:
    """Spec: "An importer agent extends a sibling importer agent"."""
    _agent(root, "vip", extends="../base-support")
    definition = _resolve_folder(root, "vip")
    prompt = definition.system_prompt
    assert definition.role_name == "vip"
    assert {"vip_tool", "base-support_tool"} <= set(definition.tools)
    assert prompt.index("base-support prose") < prompt.index("vip prose")


def test_escape_via_dotdot_rejected(root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    _agent(tmp_path, "etc")
    _agent(root, "climber", extends="../../etc")
    with pytest.raises(DefinitionError, match="escapes the importer root"):
        _resolve_folder(root, "climber")


def test_escape_via_symlink_rejected(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    _agent(tmp_path, "outside")
    (root / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    _agent(root, "linker", extends="../linked")
    with pytest.raises(DefinitionError, match="escapes the importer root"):
        _resolve_folder(root, "linker")


def test_absolute_rejected(root: pathlib.Path) -> None:
    _agent(root, "pinned", extends="/etc/passwd")
    with pytest.raises(DefinitionError, match="absolute path"):
        _resolve_folder(root, "pinned")


def test_empty_manifest_value_fails_instead_of_meaning_no_parent(
    root: pathlib.Path,
) -> None:
    _agent(root, "blank", extends='""')
    with pytest.raises(DefinitionError, match="empty 'extends:' value"):
        _resolve_folder(root, "blank")


def test_untrusted_input_stays_monotonic_across_importer_parents(
    root: pathlib.Path,
) -> None:
    """Threat matrix: an importer child cannot clear an importer parent's flag."""
    _agent(root, "exposed", policy="untrusted_input: true\n")
    _agent(root, "trusting", extends="../exposed", policy="untrusted_input: false\n")
    parent_flag = "untrusted_input=false, but its parent 'exposed'"
    with pytest.raises(DefinitionError, match=parent_flag):
        _resolve_folder(root, "trusting")
