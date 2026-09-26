# Design: Library-first custom agents (ADR-004)

## Technical Approach

One resolution pipeline for every agent shape. `harness/loader.py`'s `role_type: str`
parameter (today: "a name under `platform_root/roles`") widens into a small
discriminated union, `RoleLocator = str | FolderLocator | InlineLocator`, threaded
through `_load_role_files` → `_resolve_role_chain` → `load_generic` → `resolve()`
unchanged in every other respect. The platform-role branch (`isinstance(locator, str)`)
is today's code, byte-for-byte. `merge()`, the injector
(`resolve_tool_surface`/`resolve_command_tool_surface`), `_append_base_contract`,
`_validate_untrusted_input_exec`, and `_fold_parent_into_child` are **untouched** —
they already operate on `RawDefinition`/`AgentDefinition`, which is exactly why every
library invariant (ADR-002 B.8/B.9, C.10, C.11, ADR-003) applies to an importer-defined
agent "for free," per the proposal's stated goal.

`Agent` (new, `src/agents_system/agent/spec.py`) is a thin, frozen builder that never
talks to `resolve()`/`merge()` itself — it only produces a `RoleLocator`
(`Agent._to_locator()`), which `resolve()`/`build_runtime()` consume identically to a
platform-role string. `create_app` gains an optional `agents: Mapping[str, Agent | str]`
parameter; when omitted, `lifespan()` falls back to the existing Settings-driven boot
path (env vars), now re-keyed onto a new `AGENT_REGISTRATIONS` scheme that replaces
`{deployment}__{role}` string encoding (see D5). `demo.py`'s existing
`create_app(registry_factory=..., title=...)` call needs **zero changes** — it already
omits `agents`, so it keeps using the Settings-driven fallback path unchanged.

## Architecture Decisions

### D1 — `RoleLocator`: a type union, not a new wrapper class

**Choice**: `RoleLocator = str | FolderLocator | InlineLocator`, defined in
`harness/loader.py` immediately after `RawDefinition` (after line 297).

```python
@dataclasses.dataclass(frozen=True)
class FolderLocator:
    """An importer-supplied folder, read the same way `_load_role_files`
    reads a platform role folder today (role.md/manifest.md/policy.md).

    `root` bounds the importer-space `extends:` search (D2): a relative
    `extends:` value found while resolving *this* locator's chain may
    reference a sibling/descendant folder under `root`, never outside it.
    `overrides` carries `Agent.from_folder(path, **overrides)`'s Python
    params, applied (field-replace, not merge — see D3) after the folder
    is read.
    """

    path: pathlib.Path
    root: pathlib.Path
    overrides: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class InlineLocator:
    """An already-built `RawDefinition` — skips disk entirely.

    `parent` is the (not yet resolved) `extends:` value: a bare/prefixed
    platform-role string (resolved exactly like a manifest's own `extends:`
    frontmatter, lazily, at chain-walk time via D2's algorithm), or `None`.
    An `Agent(extends=<another Agent>)` is resolved eagerly at `Agent.__init__`
    time instead (object identity, no string parsing needed — see D3), so
    `parent` here is only ever `str | None` in practice, but is typed
    `RoleLocator | None` for uniformity with `_resolve_role_chain`'s walk.
    """

    raw: RawDefinition
    parent: "RoleLocator | None" = None
```

**Alternatives considered**:
- A `RoleSource` protocol (exploration Approach 3) — rejected as over-engineered for
  a folder + Python-params need with no other source kind on the roadmap; also the
  proposal's own Scope rejects it explicitly.
- A single locator *class* wrapping a discriminant enum
  (`Locator(kind=Kind.PLATFORM, ...)`) instead of a `str | FolderLocator | InlineLocator`
  union — rejected: it would force every existing call site passing a plain role-name
  string to wrap it (`Locator(kind=Kind.PLATFORM, name="sales-agent")`), which is the
  opposite of "the platform-role branch is today's behavior, byte-for-byte." A bare
  `str` staying a bare `str` is what makes every existing call site
  (`resolve("sales-agent", ...)`, 14+ callers, and every test) require zero changes.

**Rationale**: `isinstance(locator, str | FolderLocator | InlineLocator)` dispatch is
exactly as small as the proposal asks for ("a small discriminated locator"), needs no
new abstraction ceremony, and every function in the chain already pattern-matches on
`role_type` in exactly one place each (`_role_folder`'s call site inside
`_load_role_files`) — so widening is a contained, mechanical diff per function.

### D2 — `extends:` resolution: the exact algorithm and fail-loud cases

**Choice**: replace `_extends_target(raw: Any) -> str`
(`src/agents_system/harness/loader.py:869-876`, the last-segment-stripping bug) with:

```python
def _extends_target(
    raw: Any, *, current: RoleLocator, roots: RootConfig
) -> RoleLocator:
    value = str(raw).strip().rstrip("/")
    if not value:
        raise DefinitionError("Invariant violation — extends: empty value.")

    platform_name = _platform_role_name_or_none(value)  # bare name, or the
    # single segment after
    # a "platform/roles/" prefix
    if platform_name is not None:
        folder = _role_folder(
            _require_platform_root(roots.platform_root), platform_name
        )
        if folder.is_dir():
            return platform_name
        raise DefinitionError(
            f"Invariant violation — extends: {value!r} names platform role "
            f"{platform_name!r}, which has no folder at {folder}."
        )

    importer_root = _importer_root_of(current)
    if importer_root is not None:
        resolved = _resolve_within_root(importer_root, value)
        if resolved is not None and resolved.is_dir():
            return FolderLocator(path=resolved, root=importer_root)

    raise DefinitionError(
        f"Invariant violation — extends: {value!r} does not resolve inside "
        "the platform role tree (bare name, or 'platform/roles/<name>') or "
        "inside the current agent's own folder root. A value with path "
        "segments that is not 'platform/roles/<name>' is only valid when the "
        "current agent was built via Agent.from_folder, and must stay inside "
        "that folder's own root (no '..', no absolute path, no symlink escape)."
    )
```

with two small helpers:

```python
def _resolve_within_root(root: pathlib.Path, relative: str) -> pathlib.Path | None:
    """Resolve `relative` against `root`, then check containment AFTER
    resolution — never by rejecting a `..` token on sight. `relative` is
    resolved relative to the child agent's own folder (i.e. `root`, the
    importer agents root the deployer passed via RootConfig — see
    FolderLocator.root), fully resolved with `Path.resolve()` (following
    symlinks), and accepted only if that final path lands inside `root`.
    Only an absolute value is rejected before resolution — a relative value
    containing `..` is resolved and then checked structurally, so
    `extends: ../base-support` from `<root>/vip-support/` succeeds when
    `<root>/base-support/` exists (Resolved Decision, this tasks phase — see
    the worked example after the enumerated case table below), while
    `extends: ../../etc` from the same folder fails once resolution lands
    outside `root`. Never raises — returns None for anything whose fully
    resolved path does not land inside `root` (an escaping `..` chain or a
    symlink resolving outside it), so every "cannot place this extends:
    value" case raises exactly ONE DefinitionError from the caller instead
    of two different messages for "traversal attempt" vs. "genuinely
    missing folder"."""
    candidate = pathlib.PurePosixPath(relative)
    if candidate.is_absolute():
        return None
    resolved_root = root.resolve()
    resolved_candidate = (root / candidate).resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        return None
    return resolved_candidate


def _importer_root_of(locator: RoleLocator) -> pathlib.Path | None:
    if isinstance(locator, FolderLocator):
        return locator.root
    return None  # str (platform) and InlineLocator (pure-Python) have no folder root
```

`_platform_role_name_or_none` accepts exactly the two forms already written on disk
today: a bare single segment matching `_SAFE_SEGMENT` (`loader.py:676`), or a value
whose entire path is `platform/roles/<single segment>`. Anything else (a multi-segment
value not prefixed `platform/roles/`, e.g. today's `some/importer/path/agent`) is
**never** treated as a platform-role reference — it falls straight to the importer-space
attempt, or fails loud if `current` has no importer root at all.

**Every fail-loud case, enumerated**:

| # | Case | Result |
|---|---|---|
| 1 | Empty/whitespace-only `extends:` | `DefinitionError` |
| 2 | Bare name or `platform/roles/<name>`, folder missing | `DefinitionError` (was: falls through, now: fails immediately — no importer-space fallback attempted, to avoid a bare name accidentally resolving against an importer folder of the same name) |
| 3 | Multi-segment, not platform-prefixed, `current` has no importer root (pure-Python `Agent(extends="some/path")`) | `DefinitionError` |
| 4 | Multi-segment, not platform-prefixed, absolute | `DefinitionError` (rejected before any filesystem check — the ONLY syntactic rejection; see Resolved Decision below for why `..` alone is not rejected here) |
| 5 | Multi-segment, not platform-prefixed, relative (MAY contain `..`), and the fully resolved real path (`Path.resolve()`, following symlinks) does NOT land inside `root` | `DefinitionError` (rejected AFTER resolution, same message shape as #4 — covers both a `..` escape such as `../../etc` and a symlink whose target resolves outside `root`) |
| 6 | Resolves inside `root`, but the folder is missing or lacks one of the 3 required files | `DefinitionError` (unchanged shape — same error `_load_role_files` raises today for a missing platform folder) |
| 7 | Cycle in the walked chain | `DefinitionError` — **unchanged**, `_resolve_role_chain`'s existing `seen`-list check (`loader.py:1149-1153`), now keyed by `_locator_key(locator)` (`f"platform:{name}"` / `f"folder:{path.resolve()}"` / `f"inline:{id(raw)}"`) instead of a bare string |
| 8 | Chain deeper than `_MAX_ROLE_CHAIN_DEPTH` (8, `loader.py:722`) | `DefinitionError` — **unchanged** |

**Resolved Decision (this tasks phase) — `..` containment is checked AFTER
resolution, not rejected on sight**: an earlier draft of this section
rejected any `..` segment syntactically, before resolution (`".." in
candidate.parts: return None`). That conflicted with this same design's own
"An importer agent extends a sibling importer agent" scenario below (and the
spec's matching requirement), which requires `extends: ../base-support` from
`<root>/vip-support/` to succeed when `<root>/base-support/` exists. The
resolved algorithm: `relative` is resolved relative to the child agent's own
folder, under the importer agents root the deployer passed via `RootConfig`
(`FolderLocator.root`); the result is fully resolved (`Path.resolve()`,
following symlinks); it is accepted only if that final path lands inside
`root`, and rejected with `DefinitionError` if it escapes `root` (whether via
`..` or via a symlink), or if the original value was absolute. Concretely: a
sibling reference such as `../base-support` is accepted when it stays inside
`root`; `../../etc` is rejected once resolution lands outside `root`; an
absolute path is rejected before resolution is even attempted. Case #4/#5
above and `_resolve_within_root`'s implementation (this section's code block)
reflect this resolved decision.

Case #2's "no importer-space fallback for a bare/platform-shaped name" is the one
place this design is stricter than the minimum the bug fix requires, and it is
deliberate: it closes the exact class of surprise the current bug produces (a value
that *looks* like it means one thing silently resolving to another) rather than
reopening a narrower version of it.

**Alternatives considered**: keep `_extends_target(raw: Any) -> str` and add a
*second*, separate importer-resolution function called only for `FolderLocator`
chains — rejected because `_resolve_role_chain`'s loop body would then need to know
which of the two functions to call, duplicating the platform-vs-importer branching
`_extends_target` itself should own.

**Rationale**: fixes the flagged bug (proposal Scope item 3; exploration's Key
Learning #1) by construction — every value that cannot be placed in exactly one of
the two allowed spaces raises, with no silent re-resolution.

### D3 — `Agent` public API: immutable, folder-and/or-params, one output shape

**Choice**: `Agent` is a frozen dataclass in `src/agents_system/agent/spec.py` (see D6
for why not `agent.py`), producing a `RoleLocator` via `_to_locator()` — never an
`AgentDefinition` itself. `RawDefinition` is the intermediate shape `Agent` builds;
`AgentDefinition` only exists after `resolve()`/`build_runtime()` folds inheritance,
appends the base contract, and validates invariants — identical to how a predefined
role's `RawDefinition` never leaves `loader.py` unresolved.

```python
@dataclasses.dataclass(frozen=True)
class Agent:
    name: str
    extends: "str | Agent" = "platform/roles/agent"
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    skill_contents: Mapping[str, str] = dataclasses.field(default_factory=dict)
    context: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    autonomy: str = ""
    escalation_rules: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    delegation_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    memory_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    audit_policy: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    execution_limits: Mapping[str, Any] | str | None = None
    untrusted_input: bool | None = None
    system_prompt: str = ""
    version: str = "1.0"
    _folder: pathlib.Path | None = dataclasses.field(default=None, repr=False)
    _folder_overrides: Mapping[str, Any] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        if self.skill_contents.keys() - set(self.skills):
            raise DefinitionError(
                "Agent: skill_contents names a skill not listed in `skills`: "
                f"{sorted(self.skill_contents.keys() - set(self.skills))}"
            )
        if self._folder is not None:
            unknown = set(self._folder_overrides) - _AGENT_OVERRIDABLE_FIELDS
            if unknown:
                raise DefinitionError(
                    f"Agent.from_folder: unknown override(s) {sorted(unknown)}"
                )

    @classmethod
    def from_folder(cls, path: str | pathlib.Path, /, **overrides: Any) -> "Agent":
        """Build an Agent lazily from `path`'s role.md/manifest.md/policy.md
        (the same 3-file contract a platform role uses). No filesystem
        access happens here — matches RootConfig's own "validate at first
        use" philosophy (loader.py:84-96) — `path` is read the first time
        this Agent's locator is actually resolved. `overrides` REPLACES
        (does not merge with) the folder's own field, applied after the
        read — a caller wanting additive tools writes
        `tools=[*folder_tools, "extra"]` explicitly; this stays predictable
        rather than guessing a merge direction per field."""
        folder_path = pathlib.Path(path)
        return cls(
            name=str(overrides.get("name", folder_path.name)),
            _folder=folder_path,
            _folder_overrides=dict(overrides),
        )

    def _to_locator(self) -> "RoleLocator":
        if self._folder is not None:
            return FolderLocator(
                path=self._folder,
                root=self._folder.parent,
                overrides=self._folder_overrides,
            )
        raw = RawDefinition(
            role_name=self.name,
            version=self.version,
            deployment=None,
            system_prompt=self.system_prompt,
            tools=list(self.tools),
            skills=list(self.skills),
            context=dict(self.context),
            permissions=list(self.permissions),
            autonomy=self.autonomy,
            escalation_rules=dict(self.escalation_rules),
            delegation_policy=dict(self.delegation_policy),
            memory_policy=dict(self.memory_policy),
            audit_policy=dict(self.audit_policy),
            execution_limits=self.execution_limits,
            untrusted_input=self.untrusted_input,
        )
        parent: RoleLocator | None
        if isinstance(self.extends, Agent):
            parent = self.extends._to_locator()  # eager — object already exists, no I/O
        else:
            parent = self.extends  # str, resolved lazily via D2 at chain-walk time
        return InlineLocator(raw=raw, parent=parent)
```

`skill_contents` is threaded through (design D4 consumes it via new `AgentDefinition`
fields), but its *loading* mechanics belong entirely to the skills PR (PR3) — the
class's full parameter surface ships once, in PR2, so reviewers see the whole public
API in one place instead of it growing again in PR3.

**Why `extends: str | Agent`, resolved eagerly for `Agent` and lazily for `str`**:
an `Agent` value already exists in memory (fully constructed, immutable) when passed
as `extends=`, so resolving it to a locator is a pure Python call with no I/O and no
cycle risk — Python cannot construct an object before its own dependencies exist, so
`Agent`-to-`Agent` extends chains are cycle-free *by construction*, which is also the
main argument for `Agent` being frozen: a mutable `Agent` could be assigned into its
own `extends` after the fact, reopening exactly the cycle risk immutability closes.
A `str` value (a platform-role name, or — only reachable through `from_folder`'s
folder-based chain, never through a pure-Python `Agent` — a path relative to an
importer root) still needs `roots`/folder context that only exists at
`resolve()`/`build_runtime()` time, so it stays a lazy `RawDefinition`/`FolderLocator`
`parent`, walked by D2's algorithm exactly like a manifest's own `extends:` frontmatter.

**Alternatives considered**:
- Additive-merge overrides for `Agent.from_folder(path, tools=[...])` (folder tools ∪
  Python tools) — rejected: ambiguous for dict-typed fields (`context`, policy dicts),
  and a caller wanting union semantics can express it themselves in one line; a
  hidden per-field merge policy is a worse reviewability tradeoff than an explicit one.
- Eager folder read at `from_folder()` call time (become an `InlineLocator`
  immediately) — rejected: breaks the "importer's own root" `extends:` search (D2),
  which needs `FolderLocator` for not-yet-read ancestors reached by walking the
  chain; keeping the leaf itself lazy too means `_load_role_files`'s `FolderLocator`
  branch has exactly one code path for every disk-based hop, leaf or ancestor, with
  no special-casing.

### D4 — Skills: precedence order and the `AgentDefinition` shape that carries it

**Choice**: extend `AgentDefinition` (`loader.py:192-220`) with two new, defaulted,
backward-compatible fields:

```python
skills_folder: pathlib.Path | None = None
inline_skills: Mapping[str, str] = dataclasses.field(default_factory=dict)
```

Populated only by the `FolderLocator`/`InlineLocator` branches of `_load_role_files`/
`resolve()` (a platform role's `AgentDefinition` gets `skills_folder=None,
inline_skills={}` — unchanged output for every existing test comparing
`AgentDefinition` field-by-field, since both new fields default).

`_load_skills` (`factory.py:124-193`) resolution order, per declared skill name in
`definition.skills`:

1. `definition.inline_skills[name]` if present — an importer's own Python-supplied
   content (`Agent(skill_contents={...})`).
2. `definition.skills_folder / f"{name}.md"` if `skills_folder` is set and the file
   exists — an importer's own `Agent.from_folder(path)`'s `path/skills/` directory.
3. `deployments_root/{client}/{role_name}/skills/{name}.md` if `client is not None` —
   the **existing, unchanged** deployment-skills mechanism.
4. None of the above → `FactoryError` (reworded: today's message assumes "no client
   deployment" is the only possible cause; it must now name all three sources it
   checked).

**Alternatives considered**: deployment skills taking precedence over an agent's own
folder/inline content — rejected: a deployment override exists to *customize* what a
role/agent already ships (ADR's subtractive-override philosophy), not to silently
replace an importer's own explicit choice; inline (most explicit, closest to the call
site) before folder (the agent's own shipped default) before deployment (the
narrowing/customization layer) matches that philosophy end to end.

**Doc contradiction fix**: `docs/platform/deployment.md:91-93` ("There are no generic
skills. The `skills/` folder exists only in deployments.") is rewritten to state the
three sources above and their precedence; `docs/platform/role.md:7`'s "optional
`skills/` subdirectory" becomes accurate rather than aspirational.

### D5 — Registration redesign: `create_app`, and how env-driven boot maps onto it

**Choice**: `create_app` (`main.py:581-620`) gains three new, all-optional keyword
parameters:

```python
def create_app(
    *,
    registry_factory: RegistryFactory,
    agents: Mapping[str, "Agent | str"] | None = None,
    grants: Mapping[str, Sequence[str]] | None = None,
    clients: Mapping[str, str] | None = None,
    participant_directory: ParticipantDirectory | None = None,
    conversation_recorder: ConversationRecorder | None = None,
    roots: RootConfig | None = None,
    title: str = "agents_system",
) -> FastAPI:
```

- `agents`: deployer-chosen runtime id → either an `Agent` (custom) or a bare `str`
  (platform role name, resolved exactly as today — zero behavior change). Replaces
  `{deployment}__{role}` / the `_generic` sentinel entirely.
- `grants`: id → granted permission wire names. When `None`, `lifespan()` falls back
  to `settings.deploy_grants` — unchanged dict shape, now keyed by the caller's own
  opaque ids (Backward Compatibility item 2 in the proposal already assumes this).
- `clients`: id → deployment client name, **only** meaningful when `agents[id]` is a
  `str` (Q5: deployment overrides stay predefined-role-only). Registering
  `clients[id]` against an `Agent`-valued entry raises `DefinitionError` at boot —
  fail loud, not a silent no-op.
- **Backward compatibility**: `agents=None` (the default) makes `lifespan()` fall back
  to the *existing* Settings-driven path — now re-keyed (see below) rather than
  removed. `demo.py:72-77`'s `build_app` call
  (`create_app(registry_factory=..., title="agents_system demo")`) needs **zero
  changes** and keeps working exactly as documented in
  `docs/platform/demo-entrypoint.md`.

`lifespan()`'s runtime loop (`main.py:271-380`, esp. the `model_id.split("__", 1)` at
`main.py:280-281`) is rewritten to iterate the **resolved** `{id: RoleLocator}` map
(from `agents`, normalizing `Agent → Agent._to_locator()` and `str → str`) built either
from the explicit `agents`/`grants`/`clients` params or, when `agents is None`, from
`Settings`. The untrusted_input/WhatsApp check (`main.py:313-325`), the execution-limit
lease check (`main.py:331-342`), and the grant-lookup-before-`build_runtime`
sequencing (`main.py:344-374`) are unchanged in substance — only their key changes
from a parsed `(deployment, role)` pair to the caller's own opaque id plus the
already-resolved locator.

**Env-driven boot mapping** (the concrete answer to "how do `ADAPTER_RUNTIMES`,
`WHATSAPP_RUNTIME_ID`, `DEPLOY_GRANTS` map onto this"): `main.py`'s own Settings-driven
boot can only ever serve **platform roles** — an env var cannot carry a Python tool
connector or a `permissions=[...]` list, so it structurally cannot express an
arbitrary custom `Agent`. A new `Settings` field replaces the encoding
`{deployment}__{role}` used to carry:

```python
# config.py — new field, replaces the encoding ADAPTER_RUNTIMES/WHATSAPP_RUNTIME_ID
# ids used to carry
agent_registrations: dict[str, str] = {}
# e.g. AGENT_REGISTRATIONS='{"my-sales-bot": "sales-agent@acme", "support": "support-agent"}'
# value shape: "{role}" or "{role}@{client}" — ONE new, explicit separator,
# parsed by ONE new private helper in main.py (not openai_adapter.py — see D6),
# never duplicated.
```

`ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID` keep their existing types (`list[str]` /
`str`) but their values become **bare, opaque ids** — exactly the proposal's own
Backward Compatibility item 1 ("they hold deployer-chosen ids directly, with no
embedded role/deployment encoding to parse"). `settings.deploy_grants` is unchanged
in shape, now keyed by these same opaque ids (item 2). `lifespan()`, when `agents is
None`, builds `{id: role for id, (role, _) in parsed}` and
`{id: client for id, (_, client) in parsed if client}` from
`settings.agent_registrations`, then proceeds through the same loop `agents`-driven
boot uses.

**Alternatives considered**: keep `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID` values
`{deployment}__{role}`-shaped and just rename the parsing function — rejected: the
proposal's own Backward Compatibility section already commits to opaque ids with no
embedded encoding, and keeping the encoding would mean deployer-chosen ids are not
actually arbitrary (they'd still need `__`-free deployment/role names).

### D6 — Runtime id validation, and module placement corrections

**Choice**: `_validate_runtime_id(id: str) -> str` (new, `main.py`), reusing
`loader._SAFE_SEGMENT`'s character class (`^[A-Za-z0-9][A-Za-z0-9_-]*$`,
`loader.py:676`) for consistency with every other user-supplied filesystem-adjacent
segment in this codebase. Called once per id while `lifespan()` builds its working
map, whether the id came from `agents`/`grants`/`clients` or from
`settings.agent_registrations`; a malformed id fails boot loudly
(`DefinitionError`), not at first request.

`to_model_id`/`parse_model_id` (`integration/openai_adapter.py:54-74`) are **deleted**,
not consolidated — confirmed only 4 references repo-wide (`openai_adapter.py` itself,
`tests/test_openai_adapter.py`, and this proposal/exploration's own text); no other
production code imports them. The new `AGENT_REGISTRATIONS` `"{role}@{client}"` parser
is intentionally **not** placed in `openai_adapter.py` and not exported: it is
`main.py`'s own env-boot convention, not a public string-format contract library
consumers should parse or produce (a library consumer registers `Agent`/`str` objects
directly via `create_app(agents=...)`, never a string).

**Module placement correction** (proposal text says `src/agents_system/agent_spec.py`
*or* `src/agents_system/agent.py` — the second option does not exist as a real
choice): `src/agents_system/agent/` is **already a package**
(`agent/graph.py`, `agent/reasoning.py`, `agent/state.py`, confirmed by direct read;
`agents_system.agent.graph.AgentRuntime` is the existing `_EXPORTS` entry at
`__init__.py:79`). A sibling *file* `agent.py` cannot coexist with the directory
`agent/` in the same parent package — Python's import system does not allow both.
This design places the new module at **`src/agents_system/agent/spec.py`**, grouping
"how an agent is defined" (`spec.py`) next to "how an agent runs" (`graph.py`) inside
the one package that already owns the concept. `agent/__init__.py` is currently empty
(confirmed by direct read) and stays empty — `spec.py` only imports from
`harness.loader` (already safe: no LangGraph import), so
`test_import_agents_system_does_not_import_agent_graph`
(`tests/test_public_api.py:150-158`) is unaffected: resolving `agents_system.Agent`
imports `agents_system.agent` (the empty package init) + `agents_system.agent.spec`
only, never touching `agent/graph.py`.

`__init__.py`'s `_EXPORTS` dict (`__init__.py:66-80`) gains one entry:

```python
"Agent": ("agents_system.agent.spec", "Agent"),
```

`tests/test_public_api.py`'s `_EXPECTED_EXPORTS` dict (`:26-40`) gains the matching
entry — both dicts stay hand-maintained and independently checked, per that test
file's own existing pattern (no self-referential check against `_EXPORTS` itself).

### D7 — `demo.py` → `examples/`, and whether a `python -m` entry remains

**Choice**: `src/agents_system/demo.py` (confirmed: `demo.py:1-162`, imports
`create_app`, `build_app(engine, model)`, `main()` reading `DEMO_DATABASE_URL`/
`DEMO_HOST`/`DEMO_PORT`) moves to **`examples/demo/app.py`**, byte-identical apart
from its module docstring and the removal of the now-inapplicable
`if __name__ == "__main__":` guard's module path in comments. No `python -m` entry
remains: `examples/` ships **no** `__init__.py` and is not part of the installed
package (`pyproject.toml`'s package discovery already only covers `src/agents_system`
— confirmed by the absence of `examples` from any packaged-artifact reference in this
repo), so `python -m examples.demo.app` would only work by accident of `cwd`-relative
`sys.path` resolution in a dev checkout — the same fragility this ADR removes
`agents_system.demo`'s in-package special case *for*. The documented invocation
becomes a plain script run: `python examples/demo/app.py`. This is strictly simpler
for a new reader (no package-discovery question to answer) and matches "the package
ships no default deployments path and no client-specific data" (Scope item 8) in
spirit — `examples/` is deliberately outside the installable surface, not a
degraded-but-still-packaged corner of it.

`docs/platform/demo-entrypoint.md` is updated to the new invocation; its documented
behavior (serve the OpenAI-compatible API over the demo database) is otherwise
unchanged — `build_app`'s body does not change at all, only its file location and
import path (`from agents_system.config import get_settings` etc. — absolute imports
of the installed library, unaffected by moving the *caller*).

**Confirmed no action needed** (matching the proposal's own Risk table, verified by
direct read of `loader.py:78-82`): `_DEFAULT_DEPLOYMENTS_ROOT = _REPO_ROOT /
"deployments"` is already a **dev-checkout-relative fallback**, the same pattern as
`_default_platform_root`'s `_CHECKOUT_PLATFORM_ROOT` — not a path packaged inside
`site-packages/agents_system/`. There is no in-package default deployments path to
remove; this scope item is already satisfied by the existing code, not new work.

### D8 — Guard 1: what counts as "last released," concretely

**Choice**: a checked-in, reviewed **snapshot fixture** — not a git tag read at test
time — extending `tests/platform_role_contract.py`'s existing `EXPECTED_ROLE_TOOLS`
(`:79-153`) pattern, split into **two independent enforcement halves** because they
run at two different times in this repo's actual release pipeline:

```python
@dataclasses.dataclass(frozen=True)
class RoleGovernanceSnapshot:
    tools: frozenset[str]
    permissions: frozenset[str]
    version: str  # "MAJOR.MINOR", the role's frontmatter `version:` as of
    # this reviewed snapshot


EXPECTED_ROLE_SURFACE: dict[str, RoleGovernanceSnapshot] = {
    "agent": RoleGovernanceSnapshot(
        tools=frozenset({"session_state", "escalation_notifier"}),
        permissions=frozenset({"read:session", "send:escalation"}),
        version="1.0",
    ),
    # ... one entry per PINNED_ROLES member, seeded from the CURRENT resolved
    # surface at PR1's merge time (a reviewed, deliberate copy — same
    # philosophy EXPECTED_ROLE_TOOLS already documents at :77-78).
}
```

1. **pytest contract test** (new, extends `tests/platform_role_contract.py`'s
   consumers — runs on every PR, including this one's own): for each predefined role,
   resolve it and compare `(tools, permissions)` against `EXPECTED_ROLE_SURFACE`. On a
   diff, require the resolved role's `version`'s **MAJOR** component to be strictly
   greater than the snapshot's — else fail with a message naming exactly what changed
   and what to do (bump `version:`, update the snapshot in the same PR).
2. **CI step** (new, not pytest): on a PR touching any `platform/roles/**/manifest.md`,
   require at least one commit in the PR to carry a conventional-commit
   `BREAKING CHANGE:` footer or a `!` marker (the same convention release-please
   already reads, per `release-please-config.json`'s `changelog-sections`).

**Why two halves, not one "CHANGELOG.md entry" check** (the proposal's phrasing,
Scope item "Guard 1"): `CHANGELOG.md` is generated by release-please **only** inside
the separate `chore(main): release vX.Y.Z` PR, after the role-changing PR has already
merged (confirmed: this repo's own recent history —
`d3fad1f chore(main): release 0.2.0 (#61)` is a distinct commit from every feature/fix
PR it summarizes). A pytest contract test running *on* the role-changing PR cannot see
a CHANGELOG entry that does not exist yet. Requiring the conventional-commit marker at
PR time is what actually *produces* the CHANGELOG entry at the next release — checking
for the marker directly is the enforceable, correctly-timed equivalent of what the
proposal asked for, not a scope change.

**Interaction with `bump-minor-pre-major: true`**: unchanged from the proposal's own
framing — the package's own SemVer (release-please, driven by the *same*
`BREAKING CHANGE:` footer CI step #2 above checks for) and a role's own `version:`
frontmatter (MAJOR-bumped by step #1) are independent spaces. A predefined-role
breaking change bumps **both**: the role's own `version:` (this guard) and, via the
conventional-commit marker, the package's MINOR version at the next release (existing
release-please config, already exercised for the permission-model release,
v0.1.0 → v0.2.0).

**Ground-truth correction**: `exploration.md`'s "No `CHANGELOG.md` exists... greenfield
for guard #1" and the proposal's own Risks table entry repeating it are **stale** —
`CHANGELOG.md` exists in this worktree today (confirmed by direct read: the `[0.2.0]`
section, dated 2026-09-25, with a `### ⚠ BREAKING CHANGES` section already present for
the permission-model release) and git tags `v0.1.0`/`v0.2.0` exist (confirmed via
`CHANGELOG.md`'s own `compare/v0.1.0...v0.2.0` link and this session's git status
showing the release commit). This does not change the mechanism chosen above (a
snapshot fixture, not a git-tag read, keeps the contract test git-independent — no new
git-shelling dependency in the test suite), but the proposal's framing of guard #1 as
starting from a genuinely empty CHANGELOG is no longer accurate and should not be
carried into `tasks.md` verbatim.

## Data Flow

**Define → resolve → equip → register → serve** — one pipeline, three locator
entry points converging at `resolve()`:

```
Platform role name (str)          Agent.from_folder(path)         Agent(name=..., **params)
        │                                   │                              │
        │                          FolderLocator(path, root)       InlineLocator(RawDefinition)
        │                                   │                              │
        └───────────────┬───────────────────┴──────────────┬───────────────┘
                         ▼                                  ▼
                 _load_role_files(locator, roots)   (same function, 3-way isinstance dispatch)
                         │
                         ▼
                 _extends_target(parent_raw, current=locator, roots)   ── D2 ── platform space
                         │                                                  │  or importer space
                         ▼                                                  │  (never both)
                 _resolve_role_chain — walks parent locators,          ◄────┘
                 folds root-first via _fold_parent_into_child
                 (additive — UNCHANGED, agent-shape-agnostic)
                         │
                         ▼
                 load_generic — autonomy/untrusted_input defaults,
                 abstract-role rejection (UNCHANGED)
                         │
              ┌──────────┴──────────┐
              │ client given?       │
              ▼ yes                 ▼ no
        load_override + merge   _append_base_contract directly
        (platform-role-only,        │
         UNCHANGED)                 │
              └──────────┬──────────┘
                         ▼
                 AgentDefinition  (frozen — B.8/B.9 contract, C.10/C.11
                                   invariants ALREADY applied here,
                                   for every agent shape alike)
                         │
                         ▼
                 build_runtime(locator, registry, granted_permissions, ...)
                         │
              ┌──────────┼───────────────────┐
              ▼          ▼                   ▼
      resolve_tool_   resolve_command_   _load_skills — D4 precedence:
      surface (R3)    tool_surface (R3)  inline → own folder → deployment
              │          │                   │
              └──────────┴───────────────────┘
                         ▼
                 EquippedRuntime (deploy_grant_ceiling — Layer-2, UNCHANGED)
                         │
                         ▼
                 create_app(agents={id: Agent | str}, grants, clients)  ── D5
                         │           (or Settings-driven fallback, D5)
                         ▼
                 lifespan() — one loop over the resolved {id: locator} map,
                 same untrusted_input/WhatsApp/lease/grant checks as today
                         │
                         ▼
                 app.state.runtimes[id] = AgentRuntime(equipped, model, checkpointer)
                         │
                         ▼
                 served via POST /v1/chat/completions and/or the WhatsApp
                 webhook worker — UNCHANGED from here down
```

## File Changes

| File | Action | Description |
|---|---|---|
| `src/agents_system/harness/loader.py` | Modified | `FolderLocator`/`InlineLocator`/`RoleLocator` (D1); `_extends_target` rewritten (D2); `_load_role_files`, `_resolve_role_chain`, `load_generic`, `resolve()` widened to accept `RoleLocator`; `AgentDefinition` gains `skills_folder`/`inline_skills` (D4) |
| `src/agents_system/harness/factory.py` | Modified | `build_runtime`'s `role_type: str` → `RoleLocator`; `_load_skills` rewritten with the 3-source precedence (D4) |
| `src/agents_system/agent/spec.py` | New | `Agent` class (D3, D6) |
| `src/agents_system/agent/__init__.py` | Unchanged | Stays empty — confirmed by direct read |
| `src/agents_system/__init__.py` | Modified | `_EXPORTS["Agent"]` entry (D6) |
| `src/agents_system/main.py` | Modified | `create_app`'s new `agents`/`grants`/`clients` params; `lifespan()`'s loop rewritten around the resolved `{id: locator}` map; new `_validate_runtime_id`, `_parse_agent_registration` helpers (D5, D6); inline parser at `main.py:280-281` deleted |
| `src/agents_system/config.py` | Modified | New `agent_registrations: dict[str, str]` field (D5); `adapter_runtimes`/`whatsapp_runtime_id` docstrings updated to "opaque id" semantics |
| `src/agents_system/integration/openai_adapter.py` | Modified | `to_model_id`/`parse_model_id` (`:54-74`) deleted, not consolidated |
| `tests/platform_role_contract.py` | Modified | `RoleGovernanceSnapshot`, `EXPECTED_ROLE_SURFACE` (D8); discovery (`discover_platform_roles`, `:40-52`) stays scoped to `platform_root/roles` only — unchanged |
| `tests/test_public_api.py` | Modified | `_EXPECTED_EXPORTS["Agent"]` entry |
| `.github/workflows/*.yml` (exact file TBD in tasks) | Modified | New CI step: conventional-commit breaking-change marker required when `platform/roles/**/manifest.md` changed (D8) |
| `examples/demo/app.py` | New (moved from `src/agents_system/demo.py`) | D7 |
| `docs/platform/demo-entrypoint.md` | Modified | New invocation (D7) |
| `docs/platform/deployment.md` | Modified | Skills contradiction fix (D4) |
| `docs/platform/role.md` | Modified | Skills contradiction fix (D4) |
| `docs/architecture/adr-002-agent-model-and-capabilities.md` | Modified | Pointer amendments at A.1 (`:94`), D (`:1208`), C.15 (`:1131`), E.18 (`:1360`), F (`:1429`) — confirmed current line numbers by direct read, matching the proposal's own citations |
| `docs/architecture/adr-004-library-first-agents.md` | New | This change's ADR |
| `CHANGELOG.md` | Modified (by release-please, not authored directly) | Entries land at the next release, driven by each PR's conventional-commit markers |

## Interfaces / Contracts

```python
# harness/loader.py
RoleLocator = str | FolderLocator | InlineLocator


class FolderLocator:  # frozen dataclass — see D1
    path: pathlib.Path
    root: pathlib.Path
    overrides: Mapping[str, Any]


class InlineLocator:  # frozen dataclass — see D1
    raw: RawDefinition
    parent: RoleLocator | None


class AgentDefinition:  # existing, two new defaulted fields
    ...  # unchanged fields
    skills_folder: pathlib.Path | None = None
    inline_skills: Mapping[str, str] = field(default_factory=dict)


def resolve(
    locator: RoleLocator,
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
) -> AgentDefinition: ...


# client is only valid when isinstance(locator, str) — passing a client with a
# FolderLocator/InlineLocator raises DefinitionError (Q5: deployment overrides
# stay predefined-role-only)


# harness/factory.py
def build_runtime(
    role_type: RoleLocator,
    registry: ToolRegistry,
    granted_permissions: Iterable[str | type[Permission]],
    *,
    client: str | None = None,
    roots: RootConfig | None = None,
    session_provider: async_sessionmaker[AsyncSession] | None = None,
) -> EquippedRuntime: ...


# agent/spec.py
class Agent:  # frozen dataclass — see D3
    name: str
    extends: str | "Agent" = "platform/roles/agent"
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    skill_contents: Mapping[str, str] = {}
    # ... remaining fields mirror RawDefinition's policy/context surface — see D3

    @classmethod
    def from_folder(cls, path: str | pathlib.Path, /, **overrides: Any) -> "Agent": ...


# main.py
def create_app(
    *,
    registry_factory: RegistryFactory,
    agents: Mapping[str, Agent | str] | None = None,
    grants: Mapping[str, Sequence[str]] | None = None,
    clients: Mapping[str, str] | None = None,
    participant_directory: ParticipantDirectory | None = None,
    conversation_recorder: ConversationRecorder | None = None,
    roots: RootConfig | None = None,
    title: str = "agents_system",
) -> FastAPI: ...


# config.py
class Settings(BaseSettings):
    ...
    agent_registrations: dict[str, str] = {}  # id -> "{role}" or "{role}@{client}"
```

## Testing Strategy Per PR

Strict TDD (RED-GREEN-REFACTOR) per `AGENTS.md`'s SDD-flow mapping; `sdd-tasks` writes
the RED tests named below before any production change, same convention the
permission-model change already followed (`openspec/config.yaml`'s
`rules.apply.tdd` stays `false` repo-wide; this change follows Strict TDD by
convention for its own tests regardless).

| PR | Scope | Tests-first | Est. lines | Budget risk |
|---|---|---|---|---|
| PR1 | Locator core (D1, D2): `FolderLocator`/`InlineLocator`, rewritten `_extends_target`, widened `_load_role_files`/`_resolve_role_chain`/`load_generic`/`resolve()` | `test_locator_folder.py` (folder locator success, missing folder, missing file), `test_locator_inline.py` (inline success, cycle via inline→str→folder), `test_extends_fail_loud.py` (all 8 D2 cases: empty, platform-shaped-missing, no-importer-root, `..`/absolute, symlink escape, missing folder/file, cycle, depth), full existing predefined-role/deployment-override suite green unchanged | **~420-480** | **High — likely exceeds 400.** See split below. |
| PR2 | `Agent` Python API (D3, D6): `agent/spec.py`, `__init__.py` export | `test_agent_from_params.py`, `test_agent_from_folder.py`, `test_agent_folder_plus_overrides.py`, `test_agent_extends_agent.py` (Agent-to-Agent, no I/O), `test_agent_extends_predefined_role.py`, `test_public_api.py`'s new `Agent` parametrize case | ~330-370 | Medium — close to budget, no split needed if `skill_contents` validation stays in `__post_init__` only (no `_load_skills` wiring here) |
| PR3 | Skills (D4): `_load_skills` precedence, `AgentDefinition` new fields, doc contradiction fix | `test_skills_precedence.py` (inline > folder > deployment, all 4 orderings + the miss case), `test_factory_error_message.py` (reworded message names all 3 sources) | ~230-260 | Low |
| PR4a | Registration wiring (D5, D6): `create_app`'s new params, `lifespan()` loop rewrite, `_validate_runtime_id` | `test_create_app_agents_param.py` (str-valued unchanged behavior, Agent-valued custom registration, `clients` misuse on an Agent-valued id raises, malformed id raises), `test_main.py`'s WhatsApp/adapter-binding-preserved regression cases | **~380-430** | **Medium-High — borderline.** See split below. |
| PR4b | Env migration (D5): `Settings.agent_registrations`, `_parse_agent_registration`, deletion of `to_model_id`/`parse_model_id`, `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID` opaque-id semantics | `test_agent_registrations_parsing.py` (role-only, role@client, malformed), `test_main.py` env-driven boot migration cases, deletion of `tests/test_openai_adapter.py`'s now-obsolete cases | ~260-300 | Low |
| PR5 | Terminology rename (generic → predefined) + `examples/` relocation (D7) | Mostly docs; `test_demo_entrypoint.md` reference check if one exists, otherwise manual verification only | ~150-200 | Low |
| PR6 | Guard 1 governance (D8): `RoleGovernanceSnapshot`, `EXPECTED_ROLE_SURFACE`, contract test, CI step | `test_guard1_version_bump_required.py` (diff without bump fails, diff with bump + snapshot update passes), CI step tested via a fixture PR diff (or a unit test over the marker-detection function in isolation) | ~300-340 | Low |
| PR7 | ADR-004 + ADR-002 amendments | Docs only, no tests | ~150-200 | Low |

**PR1 split recommendation**: the fail-loud `_extends_target` rewrite (D2) alone —
`_resolve_within_root`, `_importer_root_of`, `_platform_role_name_or_none`, the 8
enumerated fail cases and their tests — is substantial enough to stand alone. Split
into:
- **PR1a — Locator types + disk-reading dispatch.** `FolderLocator`/`InlineLocator`,
  `_load_role_files`'s 3-way branch, `_resolve_role_chain`'s locator-keyed cycle
  detection, `load_generic`/`resolve()` signature widening. `extends:` behavior
  **unchanged** in this slice (still the old last-segment-stripping function, only
  now operating on `RoleLocator` inputs for the platform branch). ~220-260 lines.
- **PR1b — `_extends_target` fail-loud rewrite.** The full D2 algorithm and its 8
  enumerated cases, now that PR1a's locator plumbing exists to test against.
  ~200-230 lines.

**PR4a split recommendation** (the proposal's own Risks table already flags this PR
as "High" likelihood of budget risk): if the combined `create_app`-params +
`lifespan()`-loop-rewrite diff measured during implementation exceeds 400 lines,
split along the same seam the proposal already uses for 4a/4b — **PR4a-i**
(`create_app`'s new params + id validation + tests, no `lifespan()` changes yet, new
params accepted but unused) and **PR4a-ii** (`lifespan()`'s loop rewritten to actually
consume them + the WhatsApp/adapter-preserved-behavior regression suite). Deferred to
`sdd-tasks` to decide based on the actual measured diff, since a design-time estimate
is necessarily approximate for a rewrite this entangled with existing control flow.

**Review Workload Guard** (`sdd-phase-common.md` Section E): `Decision needed before
apply: Yes` (delivery strategy is `ask-on-risk` per this session's cached preflight —
already resolved, not newly flagged here). `Chained PRs recommended: Yes` (Feature
Branch Chain, matching the proposal's PR1→PR7 ordering, now PR1a/PR1b and
PR4a-i/PR4a-ii on top). `400-line budget risk: High` for PR1 and PR4a specifically
(both flagged above with concrete splits); `Medium` overall across the full chain.

## Threat Matrix

Reference matrix (`references/threat-matrix.md`) applicability — none of its rows
apply to this change:

| Boundary | Applicability | Reason |
|---|---|---|
| Documentation-like paths | N/A | No executable-Markdown or build-file classification introduced |
| Git repository selection | N/A | No `git -C`/repo-selection logic touched |
| Commit state | N/A | No commit/staging automation |
| Push state | N/A | No push/ref automation |
| PR commands | N/A | No PR automation (the CI step in D8 is a GitHub Actions workflow check, not a PR-mutating command) |

This change **does** introduce a genuine new trust boundary — an importer-supplied
folder path and its `extends:` chain — not covered by the reference table's rows.
Supplementary matrix, scoped to D1/D2/D4:

| Case | Applicability | Design response | Planned RED test |
|---|---|---|---|
| `extends:` value with a `..` segment, targeting a `FolderLocator`'s importer root | Applicable | `_resolve_within_root` rejects before any filesystem access (D2, case #4) | `test_extends_fail_loud.py::test_dotdot_segment_rejected` |
| `extends:` value with an absolute path | Applicable | Same function, same rejection (case #4) | `test_extends_fail_loud.py::test_absolute_path_rejected` |
| `extends:` value resolving syntactically inside `root`, but the target is a symlink pointing outside it | Applicable | `Path.resolve()` + `is_relative_to` check after syntactic validation (D2, case #5) | `test_extends_fail_loud.py::test_symlink_escape_rejected` |
| Importer folder's `skills/` directory contains a symlink pointing outside the folder | Applicable | Superseded by issue #75: the skill file must resolve inside its own `skills/` folder AND the importer root (`AgentDefinition.importer_root`); PR3 bounded it by `skills/` alone, so a `skills` folder that was itself a symlink out of the root passed. Same check for the deployment source, inside the deployments root | `test_skills_precedence.py::test_skill_symlink_does_not_escape_folder` |
| `role.md`/`manifest.md`/`policy.md` inside an accepted folder is a symlink pointing outside the importer root (issue #75) | Applicable | `_read_contained_md`: each file must resolve inside `FolderLocator.root` after `..` and symlinks, else `DefinitionError` | `test_importer_folder_containment.py::test_role_file_symlinked_outside_the_root_is_rejected` |
| The leaf `FolderLocator.path` itself lies outside its `root` (issue #75) | Applicable | `_contained_folder` checks it with `_resolve_within_root(root, path, ".")`, the same helper as `extends:` | `test_importer_folder_containment.py::test_leaf_path_outside_its_root_is_rejected` |
| A folder that passed the check is swapped for a symlink before it is read (TOCTOU, issue #75) | Applicable | `_read_within_root` walks from the root with `O_NOFOLLOW` directory handles; see the decision below | `test_importer_folder_containment.py::test_folder_swapped_for_a_symlink_after_the_check_is_refused` |
| A hand-built `FolderLocator(overrides=...)` sets `command_tool_declarations`, `deployment` or another field `Agent` cannot override (issue #75) | Applicable | `_FOLDER_OVERRIDE_FIELDS` allowlist, pinned equal to `_AGENT_OVERRIDABLE_FIELDS` | `test_importer_folder_containment.py::test_override_outside_the_allowlist_is_rejected` |
| `InlineLocator(raw=RawDefinition(...))` carries a command tool declaration that never went through the manifest parser (issue #75) | Applicable | `resolve()` runs `_revalidate_command_tools` on every definition: T2 only, `run:`, absolute `argv[0]`, whole-element placeholders, narrow params | `test_importer_folder_containment.py::test_invalid_inline_command_tool_is_rejected_by_resolve` |
| A custom `Agent` extends an `untrusted_input: true` predefined role and tries to widen scope (e.g., declare `exec:*`) | Applicable — but already mitigated by an existing, unchanged invariant | `_validate_untrusted_input_monotonic` and `_validate_untrusted_input_exec` are agent-shape-agnostic (they operate on `RawDefinition`/resolved permissions, not on locator kind) — this is the proposal's own "for free" claim, verified here rather than assumed | `test_untrusted_input_invariant.py`'s existing suite extended with one `FolderLocator`/`InlineLocator`-sourced case each, confirming the SAME rejection fires |
| `Agent(skill_contents={...})` — inline Python-supplied skill text | N/A — not a filesystem boundary | The importer's own process memory; no path resolution involved at all | None needed |
| `platform_role_contract.py`'s `discover_platform_roles()` accidentally walking into an importer folder | Applicable — regression risk, not a new attack surface | `discover_platform_roles()` (`:40-52`) walks `platform_roots_dir()` only; nothing in D1-D4 changes what that function iterates — verified by direct read, not just assumed | Existing `PINNED_ROLES` count assertion stays a regression gate; no new test needed beyond keeping it green |

### Design note — issue #75: TOCTOU decision and threat model

**Decision: implemented, not deferred.** Every importer-folder read goes
through `_read_within_root`: after the resolve-then-check containment
(`_resolve_within_root`), it opens the real importer root and walks the
checked path one directory at a time with `os.open(..., dir_fd=...)` and
`O_NOFOLLOW`, then opens the file `O_NOFOLLOW | O_NONBLOCK` and requires a
regular file. A component swapped for a symlink after the check fails the
read; a FIFO cannot block it.

**Why**: `os.open` supports `dir_fd`, and `O_NOFOLLOW`/`O_DIRECTORY` exist, on
both Linux and macOS in the standard library, so the fix is small and needs
no dependency. Documenting the window instead would leave a real gap for
the one scenario that raises this issue's severity (agent folders from a
less-trusted source).

**Threat model**: the attacker can write anywhere inside an importer root.
The root path and everything above it, `platform/roles/` and the deployments
tree are the deployer's and trusted (their role files are read by path).
Out of scope: hard links (keep untrusted roots on their own filesystem, or
`fs.protected_hardlinks` on), mount points inside the root, and files of one
folder changing between its three reads. Where `os.open` lacks `dir_fd`
(Windows), reads fall back to the path after the same check; the window
stays open there. User-facing statement: `docs/platform/role.md`,
"Importer folders stay inside their root" (and its `platform_es` twin).

## Migration / Rollout

No data migration, no persisted-state schema change. Every break is enumerated in the
proposal's own Backward Compatibility & Migration section and is unaffected by this
design beyond the concrete mechanism chosen for each (D2's fail-loud `extends:`, D5's
`AGENT_REGISTRATIONS` env scheme, D6's `to_model_id`/`parse_model_id` deletion). Each
PR remains independently revertible per the proposal's Rollback Plan; this design adds
no cross-PR coupling beyond what the proposal's own PR-chain dependency already states
(registration redesign depends on the locator core).

## Open Questions

- [ ] Should `Agent` expose `command_tools` (ADR-002 C.12 declarative command tools)?
      Not addressed by the proposal's Scope and not designed here — an importer Agent
      cannot declare a command tool in this change. Flagged for a follow-up change if
      needed, not silently added to this one's surface.
- [ ] Should `Agent.from_folder`'s `abstract: true` manifest declaration be honored
      (today: ignored, since `InlineLocator`/pure-Python agents have no abstract
      concept)? A `FolderLocator`-sourced Agent COULD legitimately declare
      `abstract: true` in its own `manifest.md` and expect `resolve()` to reject
      direct use the same way a platform role does. This design's `_load_role_files`
      FolderLocator branch reads `is_abstract` from the folder's own manifest
      identically to the platform branch (D1's shared read path), so this actually
      already works without extra design — noted here only so `sdd-tasks` writes an
      explicit test for it (`test_locator_folder.py::test_abstract_folder_agent_rejected`)
      rather than discovering it accidentally.
- [ ] The exact CI workflow file/step for D8's conventional-commit marker check is
      named `.github/workflows/*.yml (exact file TBD)` in the File Changes table —
      `sdd-tasks` should confirm which existing workflow (if any) already runs on
      `platform/roles/**` changes, to extend rather than duplicate.
