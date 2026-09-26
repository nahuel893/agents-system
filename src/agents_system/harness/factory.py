"""Agent factory — the assembler.

Combines the three harness primitives into a single, ready-to-run value object:

1. ``loader.resolve``            → a validated ``AgentDefinition`` (WHO / WHAT / HOW)
2. ``injector.resolve_tool_surface`` → the granted tool surface (Layer 1 RBAC)
3. skill files on disk           → the deployment's behavioural modules

It then composes the final system prompt (role body + skill bodies +
escalation rules) and returns a frozen ``EquippedRuntime``.

Scope boundary
--------------
The factory does NOT talk to an LLM and does NOT bind tools to a model. Turning
an ``EquippedRuntime`` into a live, executing agent (LangGraph, ``bind_tools``)
is the Agent Runtime's job — a later slice.

Prompt composition contract
---------------------------
The composed prompt is the role body, followed by each skill file's content
verbatim, joined by a ``---`` separator, in the order the skills are declared
in the manifest, followed by the resolved ``escalation_rules`` rendered as a
short block (issue #36 — see ``_render_escalation_block``). Skill files own
their own headings; the factory does not rewrite them (same principle as the
skill-resolver: pass content, preserve author intent). The ADR-002 B.9 base
contract (``_BASE_PROMPT_CONTRACT``) is always the LAST block, regardless of
skills or escalation content — see ``_compose_prompt``.

Skill content resolves through a 4-source precedence (design.md D4, PR3):
(1) the definition's own ``inline_skills`` (``Agent(skill_contents=...)``),
(2) its own ``skills_folder`` (``Agent.from_folder(path)``'s ``skills/``),
(3) ``deployments/{client}/{role_type}/skills/{name}.md`` — the original,
unchanged mechanism, which is still the ONLY source a predefined platform
role ever resolves skills from, and (4) none of the above, which fails
loud. A generic role with no client and no importer-owned source has no
skills, so its prompt is the role body plus its escalation block (if any).
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import structlog

from agents_system.harness.injector import (
    _emit,
    resolve_command_tool_surface,
    resolve_tool_surface,
)
from agents_system.harness.loader import (
    _BASE_PROMPT_CONTRACT,
    AgentDefinition,
    RoleLocator,
    RootConfig,
    _as_str_list,
    _read_within_root,
    _require_deployments_root,
    _resolve_within_root,
    _shown_path,
    _strip_base_contract,
    resolve,
)
from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec
from agents_system.permissions import (
    Permission,
    UnknownPermissionNameError,
    UntrustedInputGrantError,
    covers,
)
from agents_system.permissions.permission_registry import permission_registry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger()

_SKILL_SEPARATOR = "\n\n---\n\n"


class FactoryError(Exception):
    """Raised when a runtime cannot be assembled (e.g. a skill file is missing)."""


@dataclasses.dataclass(frozen=True)
class LoadedSkill:
    """A deployment skill module loaded from disk."""

    name: str
    content: str


@dataclasses.dataclass(frozen=True)
class EquippedRuntime:
    """A fully assembled, ready-to-run agent specification.

    This is NOT a live agent — it carries everything the Agent Runtime needs to
    instantiate one: the resolved definition (context / policy / autonomy live
    there), the composed system prompt, the granted tool surface, the denied
    tools (for audit), and the loaded skill modules.

    D-009: session_provider is an optional async_sessionmaker. When set,
    _execute_tools opens one AsyncSession per turn and forwards it to async
    connectors. Defaults to None for backward compatibility.
    """

    definition: AgentDefinition
    system_prompt: str
    tools: tuple[ToolSpec, ...]
    denied_tools: tuple[tuple[str, str], ...]
    skills: tuple[LoadedSkill, ...]
    session_provider: async_sessionmaker[AsyncSession] | None = None
    deploy_grant_ceiling: frozenset[type[Permission]] = frozenset()
    """The deploy-time grant ceiling (issue #38, design.md Resolved Decision
    5): the permission classes this runtime was explicitly granted at boot
    (`main.py`'s `DEPLOY_GRANTS`, or a library caller's
    `granted_permissions`), bounded by R3 coverage of `definition.permissions`
    — what the role DECLARES (PR #55 security review). Layer-2 revalidation
    (`interceptor.intercept`, `AgentRuntime.run_turn`'s default) bounds
    itself to THIS set — never to the role's full declared set directly —
    so a role that merely DECLARES a permission is not the same as a
    deployment actually GRANTING it, and a grant naming a permission the
    role never declares neither equips a tool for it nor appears here.
    `build_runtime` derives this from the SAME R3 computation that bounds
    `tools`/`denied_tools` (Layer-1), so a class-form and string-form grant
    of the same permission equip identically.
    """


def _read_skill(root: pathlib.Path, skills_dir: pathlib.Path, name: str) -> str | None:
    """The content of ``skills_dir/{name}.md``, or ``None`` when it is
    missing or would be read from outside ``skills_dir`` or ``root``.

    One check for both skill sources (issue #75): an importer's own
    ``skills/`` inside its importer root, and a deployment's
    ``{client}/{role}/skills/`` inside the deployments root. The path must
    resolve, after ``..`` and symlinks, inside ``root``
    (``harness.loader._resolve_within_root``) and inside ``skills_dir``
    itself, and is read walking from ``root`` without following symlinks
    (``_read_within_root``). A ``skills`` directory that is itself a symlink
    out of the root, a skill name with ``..`` in it, and a role name that
    walks out of the deployments root are all refused. Never raises for an
    escape: it is reported as "not found here", like a missing file.
    """
    path = _resolve_within_root(root, skills_dir, f"{name}.md")
    if path is None:
        return None
    try:
        if not path.is_relative_to(skills_dir.resolve()):
            return None
        return _read_within_root(root, path).strip()
    except (OSError, RuntimeError):
        return None


def _load_skills(
    definition: AgentDefinition,
    client: str | None,
    roots: RootConfig,
) -> tuple[LoadedSkill, ...]:
    """Load every declared skill's content, per D4's 4-source precedence:

    1. ``definition.inline_skills[name]`` — an importer's own Python-supplied
       content (``Agent(skill_contents=...)``).
    2. ``definition.skills_folder / f"{name}.md"`` — an importer's own folder
       (``Agent.from_folder(path)``'s ``path/skills/``), checked only when
       that file exists.
    3. ``deployments_root/{client}/{role_name}/skills/{name}.md`` — the
       existing, unchanged mechanism; reachable only when ``client`` is
       given, and the ONLY source a predefined platform role ever resolves
       skills from (it never has a ``skills_folder``/``inline_skills`` of its
       own — see loader.py's ``_load_role_files``).
    4. None of the above → ``FactoryError``, naming every source checked.
    """
    if not definition.skills:
        return ()

    # Guarded at the point of use, the same way the loader guards
    # `platform_root`. Defence in depth, and honestly labelled as such: on
    # the public path `build_runtime` calls `resolve` first, which already
    # raises for an absent root whenever `client is not None`, so this guard
    # fires only for a direct call to this private function. Computed once,
    # eagerly, whenever a client is given — exactly like before this PR —
    # so a broken deployments_root fails loud even when every declared skill
    # would actually have resolved from `inline_skills`/`skills_folder`.
    deployments_root: pathlib.Path | None = None
    deployment_skills_dir: pathlib.Path | None = None
    if client is not None:
        deployments_root = _require_deployments_root(roots.deployments_root)
        deployment_skills_dir = (
            deployments_root / client / definition.role_name / "skills"
        )

    # The importer root an importer's own skills/ must stay inside. An
    # `AgentDefinition` built by hand may carry `skills_folder` alone: its
    # skills are then bounded by that folder.
    folder_root = definition.importer_root or definition.skills_folder

    loaded: list[LoadedSkill] = []
    for name in definition.skills:
        folder_path = (
            definition.skills_folder / f"{name}.md"
            if definition.skills_folder is not None
            else None
        )
        deployment_path = (
            deployment_skills_dir / f"{name}.md"
            if deployment_skills_dir is not None
            else None
        )

        content = definition.inline_skills.get(name)
        if content is None and folder_root is not None and definition.skills_folder:
            content = _read_skill(folder_root, definition.skills_folder, name)
        if content is None and deployments_root is not None and deployment_skills_dir:
            content = _read_skill(deployments_root, deployment_skills_dir, name)

        if content is None:
            # Named relative to their roots, never by host path (issue #75).
            checked = [
                "its inline content",
                f"its own folder ({_shown_path(folder_root, folder_path)})"
                if folder_root is not None and folder_path is not None
                else "its own folder (none — not an importer-folder agent)",
                f"its deployment ({_shown_path(deployments_root, deployment_path)})"
                if deployments_root is not None and deployment_path is not None
                else "its deployment (none — no client deployment was given)",
            ]
            failure_path = folder_path or deployment_path
            logger.error(
                "factory.skill_missing",
                skill=name,
                role=definition.role_name,
                deployment=definition.deployment,
                path=str(failure_path) if failure_path is not None else "<none>",
            )
            # D-007: record skill_missing event before raising
            _emit(
                "record_skill_missing",
                definition=definition,
                skill=name,
                path=str(failure_path) if failure_path is not None else "<none>",
            )
            raise FactoryError(
                f"Skill '{name}' declared by {definition.role_name} was not "
                f"found in any of its sources: {'; '.join(checked)}."
            )

        loaded.append(LoadedSkill(name=name, content=content))
        logger.info(
            "factory.skill_loaded",
            skill=name,
            role=definition.role_name,
            deployment=definition.deployment,
        )
        # D-007: record skill_loaded event
        _emit(
            "record_skill_loaded",
            definition=definition,
            skill=name,
        )

    return tuple(loaded)


_ESCALATION_HEADING = "## escalation rules"


def _normalize_condition_name(raw: str) -> str:
    """Normalize one ``escalation_rules.conditions`` entry (#88 follow-up).

    A condition name is a structural identifier -- it is looked up by exact
    string (``descriptions.get(condition)`` below) and rendered as its own
    bullet line -- not free prose like a description. Internal whitespace
    runs (a stray double space or tab) are collapsed to one space, the same
    way a description's is (``" ".join(text.split())`` below). An embedded
    newline is different in kind, not just untidy formatting: silently
    collapsing it away would hide exactly what let one condition fragment
    ``_render_escalation_block``'s output into extra bullet/heading-shaped
    lines, so it is rejected instead of normalized.
    """
    if "\n" in raw or "\r" in raw:
        raise FactoryError(
            f"Invariant violation — escalation_rules.conditions: condition "
            f"name {raw!r} contains a newline, which is not a valid "
            f"condition name. Fix the source that declared it (a role's "
            f"frontmatter, a deployment override, or an importer locator)."
        )
    return " ".join(raw.split())


def _render_escalation_block(escalation_rules: Mapping[str, Any]) -> str:
    """Render the resolved ``escalation_rules`` into a short, model-facing
    block, or ``""`` when there is nothing to say (issue #36).

    ``escalation_rules`` (``escalate_to`` + ``conditions`` + ``descriptions``)
    is structured policy data parsed from ``policy.md`` by
    ``harness.loader.resolve()`` — but before this function existed, nothing
    ever read it back out. ``policy.md``'s prose explains WHY a condition
    exists; role.md was the only place its NAME ever reached the model, and
    only if a role.md author happened to restate it there. ``accountant-agent``
    is the case that proved the gap: its ``role.md`` never mentions
    escalation, so ``figure_requested_outside_report_catalog`` — declared
    only in its ``policy.md`` — never influenced the model at all.

    Unknown/empty input renders nothing: a role that declares no escalation
    policy gets no block, so this is a strict addition for roles that do.

    ``conditions`` is coerced through ``loader._as_str_list`` — the same
    coercion ``loader._resolve_mapping_directive`` already applies to this
    exact field for the ``add``/``remove`` deployment directives — rather
    than iterated directly. YAML permits a bare scalar
    (``conditions: single_condition``) as well as a list; iterating a bare
    *string* directly yields one bullet per CHARACTER, not one condition.

    Issue #88: ``descriptions`` (``loader._parse_escalation_descriptions``'s
    output, ``{condition_name: prose}``) renders each condition as
    ``- name — description`` instead of the bare name alone. #82 showed the
    bare name was not enough for a model to reliably act on: it understood
    the data gap but explained it to the user instead of calling
    ``escalation_notifier``. A condition absent from ``descriptions`` — a
    deployment-added condition, or an importer agent that supplied none —
    still renders its bare name rather than being dropped or failing;
    ``descriptions`` is deliberately treated as a best-effort enrichment, not
    a requirement this rendering function itself enforces (that requirement,
    for every PREDEFINED role, is `tests/test_role_contract_suite.py`'s
    contract test instead).
    """
    escalate_to = str(escalation_rules.get("escalate_to") or "").strip()
    conditions = [
        condition
        for condition in (
            _normalize_condition_name(c)
            for c in _as_str_list(escalation_rules.get("conditions"))
        )
        if condition
    ]
    raw_descriptions = escalation_rules.get("descriptions")
    descriptions: Mapping[str, Any] = (
        raw_descriptions if isinstance(raw_descriptions, Mapping) else {}
    )
    # PR #99 review follow-up: `conditions` above is normalized through
    # `_normalize_condition_name` before it becomes the lookup key into
    # `descriptions` -- but `descriptions` itself (a role/deployment
    # frontmatter `escalation_rules.descriptions:`, or an importer's raw
    # `InlineLocator`/`FolderLocator` dict) is keyed by the condition's
    # UNNORMALIZED raw name. Without normalizing this mapping's own keys the
    # same way, a condition whose declared name needs whitespace collapsing
    # silently loses its description and falls back to the bare-name
    # render -- exactly the failure mode issue #88 exists to eliminate.
    # This restores the exact-string-lookup invariant: both sides of the
    # comparison go through the identical whitespace-collapsing rule.
    normalized_descriptions = {
        " ".join(str(key).split()): value for key, value in descriptions.items()
    }

    if not escalate_to and not conditions:
        return ""

    lines = [_ESCALATION_HEADING, ""]
    if escalate_to:
        lines.append(f"Escalate to: {escalate_to}")
    if conditions:
        if escalate_to:
            lines.append("")
        lines.append(
            "Escalate immediately, rather than guess, whenever any of these "
            "apply. Meeting one means CALLING `escalation_notifier` — "
            "telling the user is not enough:"
        )
        for condition in conditions:
            # PR #89 review (finding 1): a description value that reaches
            # this function without passing through the prose parser's
            # continuation-joining -- a deployment override's frontmatter
            # `descriptions:`, or an importer's InlineLocator -- is never
            # validated to be single-line. Collapsing all internal
            # whitespace (not just leading/trailing) the same way the
            # parser's continuation-joining already does means an embedded
            # newline can never fragment one bullet into several
            # bullet/heading-shaped lines, regardless of which source
            # supplied it.
            description = " ".join(
                str(normalized_descriptions.get(condition) or "").split()
            )
            if description:
                lines.append(f"- {condition} — {description}")
            else:
                lines.append(f"- {condition}")

    return "\n".join(lines)


def _compose_prompt(
    definition: AgentDefinition,
    skills: tuple[LoadedSkill, ...],
) -> str:
    """Compose the final system prompt: role body + skill bodies +
    escalation rules + base contract.

    ADR-002 B.9's six-clause base contract must be the LAST block of the
    prompt the model actually receives: the Agent Runtime sends THIS
    function's return value (``EquippedRuntime.system_prompt``) to the
    model, not ``definition.system_prompt`` (`agent/graph.py`'s
    `_call_model` uses ``equipped.system_prompt``). ``resolve()`` already
    appends the contract to ``definition.system_prompt`` so that value
    alone still satisfies B.9 for a caller reading it directly, but if this
    function simply appended skill (or escalation) content after it, the
    contract would end up in the middle rather than last.

    When there is neither skill content nor a resolved escalation policy to
    insert, ``definition.system_prompt`` is already correct as-is (the
    contract is already its final block) and is returned unchanged. Whenever
    either exists, the contract is stripped back off
    (``loader._strip_base_contract``, the exact inverse of
    ``loader._append_base_contract``), skill content and then the escalation
    block (issue #36) are inserted in that order, and the contract is
    appended once more — so it still appears exactly once overall, now
    genuinely last.
    """
    escalation_block = _render_escalation_block(definition.escalation_rules)

    if not skills and not escalation_block:
        return definition.system_prompt.strip()

    role_prompt = _strip_base_contract(definition.system_prompt).strip()
    parts = [role_prompt]
    parts.extend(skill.content for skill in skills)
    if escalation_block:
        parts.append(escalation_block)
    parts.append(_BASE_PROMPT_CONTRACT.strip())
    return _SKILL_SEPARATOR.join(p for p in parts if p)


def build_runtime(
    role_type: RoleLocator,
    registry: ToolRegistry,
    granted_permissions: Iterable[str | type[Permission]],
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
    session_provider: async_sessionmaker[AsyncSession] | None = None,
) -> EquippedRuntime:
    """Assemble an ``EquippedRuntime`` for a role (optionally a client deployment).

    Parameters
    ----------
    role_type:
        A predefined-role name (e.g. ``"sales-agent"``), or an importer
        locator (``FolderLocator``/``InlineLocator`` — what
        ``Agent._to_locator()`` returns). Passed unchanged to ``resolve()``,
        which already accepts every ``RoleLocator`` and is where each
        importer invariant is enforced (the safety ceiling, R4, ``extends:``
        containment), so accepting one here adds no path around them. The
        name stays ``role_type`` for existing keyword callers. ``client=``
        is valid only with a ``str`` — ``resolve()`` rejects the rest.
    registry:
        The live tool registry the granted surface is resolved against.
    granted_permissions:
        The requesting identity's permission grants. Accepts registered
        wire-name strings, ``Permission`` subclasses, or a mixture of both
        (spec: "Accepted grant forms"); every entry is resolved through the
        registry to a class. The effective tool surface (Layer-1, the
        injector) and ``EquippedRuntime.deploy_grant_ceiling`` (Layer-2,
        issue #38) are both derived from the SAME R3 coverage computation:
        ``definition.permissions`` — what the role DECLARES — bounded by
        whichever of those declared permissions a granted class actually
        COVERS. A permission the role never declares equips no tool and
        never appears in the ceiling; an ``untrusted_input`` role granted
        any T3-tier permission (declared or not) raises
        ``UntrustedInputGrantError`` (R4) before either is computed.
    client:
        Optional deployment client. When given, the deployment override is
        merged on top of the generic role and its skill files are loaded.
    roots:
        Injectable path config. Defaults to the real repo roots.
    """
    if roots is None:
        roots = RootConfig()

    # Materialised once: an `Iterable` may be a one-shot generator, and it is
    # consumed twice below (resolving to classes, then re-deriving the
    # declared-name subset the injector needs).
    granted = list(granted_permissions)

    definition = resolve(role_type, client=client, roots=roots)

    # Resolve every raw grant entry (string or class) to a class ONCE.
    # Unknown wire names fail loudly here (registry) before any R3/R4
    # bounding below, so a typo'd DEPLOY_GRANTS entry cannot silently
    # disappear as "covers nothing" instead of surfacing the typo.
    raw_grant_classes = frozenset(
        permission_registry.resolve(p) if isinstance(p, str) else p for p in granted
    )

    # PR #55 security review (HIGH) — R4 at grant time (spec scenario:
    # "Untrusted role cannot be equipped with a T3 grant at deploy time").
    # Independent of R4 at load time (`harness.loader`) and the injector's
    # T3 barrier (defense in depth): an untrusted_input role must never be
    # equipped with a T3-tier deploy grant, even one it doesn't declare.
    if definition.untrusted_input:
        for cls in raw_grant_classes:
            if cls.tier is Tier.T3:
                try:
                    offending_name = permission_registry.reverse(cls)
                except UnknownPermissionNameError:
                    offending_name = cls.__name__
                raise UntrustedInputGrantError(
                    offending_name, cls, definition.role_name
                )

    # PR #55 security review (MEDIUM-HIGH) — the ceiling is
    # `definition.permissions ∩ grant` under R3 (a granted class must cover
    # a permission the role actually DECLARES), never a raw union of the
    # grant: a DEPLOY_GRANTS entry naming a permission the role never
    # declares must neither equip a tool for it (below) nor appear in the
    # persisted ceiling Layer-2 trusts.
    #
    # Tolerant resolution here (skip, don't raise) mirrors
    # `injector._deny_reason`'s established pattern for the SAME reason: a
    # role's own manifest is validated at LOAD time, not here, and an
    # unregistered declared name simply covers nothing -- it must not make
    # `build_runtime` itself the (new, out of scope) enforcement point for
    # manifest-authoring mistakes in unrelated fixtures/roles.
    declared_name_to_class: dict[str, type[Permission]] = {}
    for name in definition.permissions:
        try:
            declared_name_to_class[name] = permission_registry.resolve(name)
        except UnknownPermissionNameError:
            continue
    declared_classes = set(declared_name_to_class.values())
    deploy_grant_ceiling = frozenset(
        granted_cls
        for granted_cls in raw_grant_classes
        if any(covers(granted_cls, declared_cls) for declared_cls in declared_classes)
    )

    # PR #55 security review (MEDIUM) — the SAME bound, expressed as the
    # role's own wire-name strings, feeds Layer-1
    # (`resolve_tool_surface`/`resolve_command_tool_surface`, which are
    # string-keyed): passing the raw mixed-type `granted` list there let a
    # class-form grant (e.g. granting the `Write` class directly) silently
    # equip NO tools, since a class object never equals a wire-name string.
    # Deriving the covered declared names here makes a class-form grant and
    # its string-form equivalent equip identically.
    covered_declared_names = [
        name
        for name, declared_cls in declared_name_to_class.items()
        if any(covers(granted_cls, declared_cls) for granted_cls in raw_grant_classes)
    ]

    surface = resolve_tool_surface(definition, registry, covered_declared_names)
    command_surface = resolve_command_tool_surface(definition, covered_declared_names)
    skills = _load_skills(definition, client, roots)
    system_prompt = _compose_prompt(definition, skills)

    granted_tools = surface.granted + command_surface.granted
    denied_tools = surface.denied + command_surface.denied

    logger.info(
        "factory.runtime_built",
        role=definition.role_name,
        deployment=definition.deployment,
        tools=len(granted_tools),
        denied=len(denied_tools),
        skills=len(skills),
    )
    # D-007: record runtime_built event
    _emit(
        "record_runtime_built",
        definition=definition,
        tools_count=len(granted_tools),
        denied_count=len(denied_tools),
        skills_count=len(skills),
    )

    return EquippedRuntime(
        definition=definition,
        system_prompt=system_prompt,
        tools=granted_tools,
        denied_tools=denied_tools,
        skills=skills,
        session_provider=session_provider,
        deploy_grant_ceiling=deploy_grant_ceiling,
    )
