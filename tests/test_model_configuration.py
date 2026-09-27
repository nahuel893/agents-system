"""Issue #13 / D-037 -- provider + model configuration per agent, no code
edits.

An agent (a predefined role's `policy.md`, a deployment override's
`policy.md`, or the `Agent`/`Agent.from_folder` Python API) may declare its
own `model:` block naming which already-configured provider + model id to
use, plus optional `temperature`/`max_tokens`. Declaring nothing keeps the
existing global `ADAPTER_PROVIDER`-driven default (see `test_main.py`'s
`_build_chat_model` tests) -- these tests cover only the NEW per-agent
surface: parsing, inheritance (role-to-role folds per key, a deployment
replaces wholesale), and validation (`_validate_model_config`), all at the
`harness.loader` level, plus the `Agent` Python API's own `model=` parameter.

Unlike `autonomy`/`execution_limits`/`audit_policy`, model/provider selection
is not a capability or safety ceiling -- it never widens what a role can do
-- so there is deliberately no ceiling check here, only shape/value
validation and a closed key set that structurally excludes any credential
field.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.agent.spec import Agent
from agents_system.harness.loader import (
    _SUPPORTED_MODEL_PROVIDERS,
    DefinitionError,
    RootConfig,
    _validate_model_config,
    resolve,
)

ROOTS = RootConfig()


def _write(folder: pathlib.Path, name: str, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


def _write_platform_role(
    platform_root: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    manifest_extra: str = "permissions: [read:session]\n",
    policy_extra: str = "",
) -> None:
    """A predefined role under `platform_root/roles/` (same shape every
    other loader test file writes)."""
    folder = platform_root / "roles" / name
    _write(folder, "role.md", f"---\nname: {name}\n---\n\nbody\n")
    extends_line = f"extends: {extends}\n" if extends else ""
    _write(
        folder,
        "manifest.md",
        f"---\nrole: {name}\n{extends_line}tools: []\nskills: []\ncontext: {{}}\n"
        f"{manifest_extra}---\n\nm\n",
    )
    _write(
        folder,
        "policy.md",
        f"---\nrole: {name}\nautonomy: supervised\nuntrusted_input: false\n"
        f"{policy_extra}---\n\np\n",
    )


def _write_deployment(
    deployments_root: pathlib.Path,
    client: str,
    role: str,
    *,
    tools: str = "[]",
    manifest_extra: str = "permissions: inherit\n",
    policy_extra: str = "",
) -> None:
    folder = deployments_root / client / role
    _write(folder, "role.md", f'---\nname: {role}\nversion: "1.0"\n---\n\nclient.\n')
    _write(
        folder,
        "manifest.md",
        f'---\nrole: {role}\nversion: "1.0"\ndeployment: {client}\ntools: {tools}\n'
        f"skills: []\ncontext: {{}}\n{manifest_extra}---\n",
    )
    _write(
        folder,
        "policy.md",
        f'---\nrole: {role}\nversion: "1.0"\n{policy_extra}---\n',
    )


def _deployment_roots(tmp_path: pathlib.Path) -> RootConfig:
    # Fully isolated -- an EARLIER version of this fixture pointed
    # `platform_root` at the real repo (`ROOTS.platform_root`), which leaked
    # `_write_platform_role`'s throwaway folders into the actual
    # `platform/roles/` tree. Never write a test-only role under the real
    # platform root; a temp one keeps every write inside `tmp_path`.
    return RootConfig(
        platform_root=tmp_path / "platform", deployments_root=tmp_path / "deployments"
    )


def _platform_roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(platform_root=tmp_path / "platform")


_MODEL_BLOCK = "model:\n  provider: anthropic\n  model: claude-3-5-haiku-latest\n"


# ---------------------------------------------------------------------------
# 1. No declaration anywhere -- behavior is unchanged (the global default
#    settings-driven provider applies, per main._build_chat_model).
# ---------------------------------------------------------------------------


def test_real_platform_role_with_no_model_resolves_to_none() -> None:
    definition = resolve("sales-agent")
    assert definition.model is None


# ---------------------------------------------------------------------------
# 2. A platform role's own policy.md declares `model:` -- resolved with no
#    deployment override (the `resolve()` no-override terminal branch).
# ---------------------------------------------------------------------------


def test_role_model_declaration_resolves_with_no_override(
    tmp_path: pathlib.Path,
) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(roots.platform_root, "solo-agent", policy_extra=_MODEL_BLOCK)

    definition = resolve("solo-agent", roots=roots)

    assert definition.model == {
        "provider": "anthropic",
        "model": "claude-3-5-haiku-latest",
    }


# ---------------------------------------------------------------------------
# 3. A deployment override REPLACES the role's model wholesale.
# ---------------------------------------------------------------------------


def test_deployment_override_replaces_model_wholesale(
    tmp_path: pathlib.Path,
) -> None:
    roots = _deployment_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "sales-like",
        policy_extra=(
            "model:\n"
            "  provider: anthropic\n"
            "  model: claude-3-5-haiku-latest\n"
            "  temperature: 0.1\n"
        ),
    )
    _write_deployment(
        roots.deployments_root,
        "acme",
        "sales-like",
        policy_extra=("model:\n  provider: groq\n  model: llama-3.3-70b-versatile\n"),
    )

    definition = resolve("sales-like", client="acme", roots=roots)

    # Wholesale replace: the role's temperature does NOT survive.
    assert definition.model == {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    }


def test_deployment_with_no_model_inherits_role_model(
    tmp_path: pathlib.Path,
) -> None:
    roots = _deployment_roots(tmp_path)
    _write_platform_role(roots.platform_root, "sales-like2", policy_extra=_MODEL_BLOCK)
    _write_deployment(roots.deployments_root, "acme", "sales-like2")

    definition = resolve("sales-like2", client="acme", roots=roots)

    assert definition.model == {
        "provider": "anthropic",
        "model": "claude-3-5-haiku-latest",
    }


# ---------------------------------------------------------------------------
# 4. Role-to-role `extends:` folds `model` PER KEY (same shape as
#    `execution_limits`): a child may override just `temperature` while
#    inheriting `provider`/`model` from its parent.
# ---------------------------------------------------------------------------


def test_role_extends_folds_model_per_key(tmp_path: pathlib.Path) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "model-parent",
        policy_extra=(
            "model:\n"
            "  provider: anthropic\n"
            "  model: claude-3-5-haiku-latest\n"
            "  temperature: 0.1\n"
        ),
    )
    _write_platform_role(
        roots.platform_root,
        "model-child",
        extends="model-parent",
        policy_extra="model:\n  temperature: 0.7\n",
    )

    definition = resolve("model-child", roots=roots)

    assert definition.model == {
        "provider": "anthropic",
        "model": "claude-3-5-haiku-latest",
        "temperature": 0.7,
    }


# ---------------------------------------------------------------------------
# 5. Validation -- shape/value only, never a ceiling.
# ---------------------------------------------------------------------------


def test_unknown_provider_rejected(tmp_path: pathlib.Path) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "bad-provider",
        policy_extra="model:\n  provider: made-up-vendor\n  model: x\n",
    )

    with pytest.raises(DefinitionError) as exc_info:
        resolve("bad-provider", roots=roots)

    message = str(exc_info.value)
    assert "made-up-vendor" in message
    assert "provider" in message


def test_missing_model_id_rejected(tmp_path: pathlib.Path) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "missing-model-id",
        policy_extra="model:\n  provider: anthropic\n",
    )

    with pytest.raises(DefinitionError, match="model"):
        resolve("missing-model-id", roots=roots)


def test_non_numeric_temperature_rejected(tmp_path: pathlib.Path) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "bad-temp",
        policy_extra=(
            "model:\n  provider: anthropic\n  model: x\n  temperature: hot\n"
        ),
    )

    with pytest.raises(DefinitionError, match="temperature"):
        resolve("bad-temp", roots=roots)


def test_non_positive_max_tokens_rejected(tmp_path: pathlib.Path) -> None:
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "bad-max-tokens",
        policy_extra=("model:\n  provider: anthropic\n  model: x\n  max_tokens: 0\n"),
    )

    with pytest.raises(DefinitionError, match="max_tokens"):
        resolve("bad-max-tokens", roots=roots)


def test_max_tokens_has_an_upper_bound() -> None:
    """PR #141 review: `temperature` has an explicit upper bound to catch a
    typo (`_MAX_MODEL_TEMPERATURE`), but `max_tokens` had none at all, so an
    absurd value (a misplaced decimal, a unit mixup) passed silently."""
    with pytest.raises(DefinitionError, match="max_tokens"):
        _validate_model_config(
            {"provider": "ollama", "model": "x", "max_tokens": 10**9}, who="probe"
        )


def test_credential_like_key_rejected(tmp_path: pathlib.Path) -> None:
    """Credentials never come from a versioned manifest -- an attempted
    `api_key` (or any key outside the closed set) is rejected structurally,
    not merely discouraged by convention."""
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "smuggled-key",
        policy_extra=(
            "model:\n"
            "  provider: anthropic\n"
            "  model: x\n"
            "  api_key: sk-should-never-be-here\n"
        ),
    )

    with pytest.raises(DefinitionError) as exc_info:
        resolve("smuggled-key", roots=roots)

    message = str(exc_info.value)
    assert "api_key" in message
    assert "sk-should-never-be-here" not in message


def test_validate_model_config_accepts_minimal_valid_shape() -> None:
    # Does not raise.
    _validate_model_config(
        {"provider": "ollama", "model": "qwen2.5:3b"}, who="unit test"
    )


def test_malformed_role_model_still_validated_through_a_deployment_with_no_model(
    tmp_path: pathlib.Path,
) -> None:
    """PR #141 review: `_merge_validated`'s "override declares no model:"
    branch took `generic.model` straight from the role without ever calling
    `_validate_model_config` on it -- so a malformed role `model:` was only
    ever checked on the no-deployment path. A deployment override that does
    not itself touch `model:` (the common case) must not bypass validation
    of the role's own declaration."""
    roots = _deployment_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "bad-role-with-deployment",
        policy_extra="model:\n  provider: made-up-vendor\n  model: x\n",
    )
    _write_deployment(roots.deployments_root, "acme", "bad-role-with-deployment")

    # No client: already correctly raises -- confirms the role model really
    # is malformed, isolating the deployment path as what is under test.
    with pytest.raises(DefinitionError):
        resolve("bad-role-with-deployment", roots=roots)

    with pytest.raises(DefinitionError) as exc_info:
        resolve("bad-role-with-deployment", client="acme", roots=roots)

    assert "made-up-vendor" in str(exc_info.value)


def test_role_own_non_mapping_model_rejected(tmp_path: pathlib.Path) -> None:
    """PR #141 review: a non-mapping, non-None, non-'inherit' `model:` value
    (a bare scalar -- the single most likely authoring typo, writing
    `model: claude-3-5-sonnet-latest` instead of the documented
    `provider:`/`model:` mapping) silently resolved to `None` instead of
    raising, discarding the author's intent with no error at all."""
    roots = _platform_roots(tmp_path)
    _write_platform_role(
        roots.platform_root,
        "typo-model",
        policy_extra="model: not-a-real-declaration\n",
    )

    with pytest.raises(DefinitionError, match="model"):
        resolve("typo-model", roots=roots)


def test_deployment_non_mapping_model_rejected(tmp_path: pathlib.Path) -> None:
    """Same defect, at the deployment-override boundary: a bare scalar
    `model:` on the override's own policy.md silently resolved to `None`
    (discarding the role's valid model) instead of raising."""
    roots = _deployment_roots(tmp_path)
    _write_platform_role(roots.platform_root, "sales-like3", policy_extra=_MODEL_BLOCK)
    _write_deployment(
        roots.deployments_root,
        "acme",
        "sales-like3",
        policy_extra="model: 5\n",
    )

    with pytest.raises(DefinitionError, match="model"):
        resolve("sales-like3", client="acme", roots=roots)


def test_supported_providers_are_exactly_the_adapter_provider_literal() -> None:
    """Guards against the loader's own provider set drifting from
    `Settings.adapter_provider`'s `Literal` (config.py) -- the loader cannot
    import `main`/`config` provider dispatch without a cycle risk, so this
    is the seam that would catch a silent drift."""
    from typing import get_args

    from agents_system.config import Settings

    annotation = Settings.model_fields["adapter_provider"].annotation
    assert set(get_args(annotation)) == set(_SUPPORTED_MODEL_PROVIDERS)


# ---------------------------------------------------------------------------
# 6. The `Agent` Python API (ADR-004 D3) -- `Agent(model=...)` and
#    `Agent.from_folder(path, model=...)` go through the identical loader
#    resolution path, no ceiling either.
# ---------------------------------------------------------------------------


def test_agent_python_api_accepts_model_param() -> None:
    agent = Agent(
        name="inline-model-agent",
        extends="platform/roles/agent",
        model={"provider": "groq", "model": "llama-3.3-70b-versatile"},
    )

    definition = resolve(agent._to_locator())

    assert definition.model == {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    }


def test_agent_from_folder_model_override(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "importer-agent"
    _write(folder, "role.md", "---\nname: importer-agent\n---\n\nbody\n")
    _write(
        folder,
        "manifest.md",
        "---\nrole: importer-agent\nextends: platform/roles/agent\ntools: []\n"
        "skills: []\ncontext: {}\npermissions: []\n---\n\nm\n",
    )
    _write(
        folder,
        "policy.md",
        "---\nrole: importer-agent\nautonomy: supervised\n"
        "untrusted_input: false\n---\n\np\n",
    )

    agent = Agent.from_folder(folder, model={"provider": "ollama", "model": "qwen3:8b"})

    definition = resolve(agent._to_locator())

    assert definition.model == {"provider": "ollama", "model": "qwen3:8b"}
