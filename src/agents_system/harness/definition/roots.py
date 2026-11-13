"""Where definitions are read from: the platform and deployments roots."""

from __future__ import annotations

import dataclasses
import pathlib

from agents_system.harness.definition.errors import DefinitionError

# ---------------------------------------------------------------------------
# Repo-level default roots (overridable via RootConfig)
# ---------------------------------------------------------------------------
# Two candidate locations for the platform/ tree that ships the generic
# agent roles:
#   1. packaged      — three hops up from this file, next to the installed
#      `agents_system` package (populated by pyproject.toml's
#      [tool.hatch.build.targets.wheel.force-include], which maps the
#      repo-root `platform/` to `agents_system/platform` inside the wheel).
#   2. dev-checkout   — five hops up from this file, the repo-root sibling
#      of `src/` (this repo, before packaging).
#
# `deployments_root` is deliberately NOT resolved this way: a client's
# deployments are the client's own and are never packaged with the library
# (see docs/platform/library-usage.md). It keeps the dev-checkout default
# only — a consumer must pass its own `deployments_root` explicitly.
_PACKAGED_PLATFORM_ROOT = pathlib.Path(__file__).parent.parent.parent / "platform"
_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent.parent
_CHECKOUT_PLATFORM_ROOT = _REPO_ROOT / "platform"
_DEFAULT_DEPLOYMENTS_ROOT = _REPO_ROOT / "deployments"


def _default_platform_root() -> pathlib.Path:
    """Best guess at ``platform_root``: packaged location, else dev checkout.

    Deliberately does NOT validate. ``default_factory`` is evaluated per FIELD,
    not per object, so raising here would make a bare ``RootConfig()`` — one
    built solely to reach ``deployments_root`` — fail over a directory the
    caller never reads. ``load_override`` is exactly that caller.

    Validation happens at first real use, in ``_require_platform_root``.
    """
    if _PACKAGED_PLATFORM_ROOT.is_dir():
        return _PACKAGED_PLATFORM_ROOT
    return _CHECKOUT_PLATFORM_ROOT


def _require_platform_root(root: pathlib.Path) -> pathlib.Path:
    """Fail loudly, naming what was searched, before reading role files.

    Without this the missing directory surfaces as a per-file "role.md not
    found", which points at the wrong problem.
    """
    if root.is_dir():
        return root
    raise DefinitionError(
        "Could not locate the platform/ directory that ships the predefined "
        f"agent roles. Looked in: {root}\n"
        f"  - packaged location: {_PACKAGED_PLATFORM_ROOT}\n"
        f"  - dev-checkout location: {_CHECKOUT_PLATFORM_ROOT}\n"
        "Pass an explicit RootConfig(platform_root=...) if your platform "
        "roles live elsewhere."
    )


def _require_deployments_root(root: pathlib.Path) -> pathlib.Path:
    """Fail loudly, naming what was searched, before resolving a client override.

    Mirrors ``_require_platform_root``. Without this, a missing
    ``deployments/`` directory (e.g. agents_system installed as a dependency, with
    no co-located ``deployments/``) surfaces as ``load_override``'s "not
    found" warning, and ``resolve`` silently falls back to the generic role.
    That fallback is not neutral: a deployment override may only NARROW the
    generic role, never broaden it, so falling back to the unrestricted
    generic role WIDENS tools/autonomy/permissions past what the requested
    (but unconfigured) override would have restricted — a security
    relaxation disguised as a safe default. Only called when a client
    override is actually requested; ``resolve(role)`` with no ``client``
    never needs a ``deployments_root`` at all.
    """
    if root.is_dir():
        return root
    raise DefinitionError(
        "Could not locate the deployments/ directory required to resolve a "
        f"client override. Looked in: {root}\n"
        "agents_system does not derive `deployments_root` from its own package/"
        "installation location — a consumer must pass its own "
        "RootConfig(deployments_root=...) explicitly "
        "(see docs/platform/library-usage.md)."
    )


@dataclasses.dataclass(frozen=True)
class RootConfig:
    """Injectable path roots so tests can point at fixtures."""

    platform_root: pathlib.Path = dataclasses.field(
        default_factory=_default_platform_root
    )
    deployments_root: pathlib.Path = dataclasses.field(
        default_factory=lambda: _DEFAULT_DEPLOYMENTS_ROOT
    )
