"""Issue #187 -- the entry points present agents-system as a development kit.

The first lines of `README.md`, `AGENTS.md`, the `pyproject.toml` description
and the architecture overview (EN and its ES twin) used to describe a
"production-grade runtime platform" with a WhatsApp sales bot for one
customer as its first delivery. They steer people, and coding agents, toward a
platform with a bundled bot instead of a kit.

This is a regression gate on the positioning: the first paragraph of each
entry point says "development kit" and links the Quickstart, no entry point
falls back to the single-bot or single-customer framing, and the EN and ES
twins name the same optional pieces.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (path, heading after which the opening paragraph sits, the kit phrase,
#: the Quickstart link the opening must carry)
ENTRY_POINTS = (
    ("README.md", None, "development kit", "library-usage.md#quickstart"),
    ("AGENTS.md", "## Project", "development kit", "library-usage.md#quickstart"),
    (
        "docs/architecture/agent-platform.md",
        None,
        "development kit",
        "library-usage.md#quickstart",
    ),
    (
        "docs/architecture_es/agent-platform.md",
        None,
        "kit de desarrollo",
        "library-usage.md#inicio-rápido",
    ),
)

#: Framings an entry point must never fall back to.
FORBIDDEN_EVERYWHERE = (
    "runtime platform",
    "beverage distributor",
    "sales bot",
    "first delivery",
    "agents-acme",
    "Reusable AI agent platform",
)

#: The architecture overview must not name a customer's systems either.
FORBIDDEN_IN_ARCHITECTURE = (
    "ACME",
    "DeW",
    "App Preventas",
    "App Sergio",
    "Outline",
    "Element",
    "Preventa",
)

OPTIONAL_PIECES = {
    "docs/architecture/agent-platform.md": (
        "server",
        "channels",
        "predefined roles",
        "reference backends",
    ),
    "docs/architecture_es/agent-platform.md": (
        "servidor",
        "canales",
        "roles predefinidos",
        "backends de referencia",
    ),
}


def _opening_paragraph(text: str, after_heading: str | None) -> str:
    """First prose paragraph: not a heading, badge line or blockquote."""
    if after_heading is not None:
        text = text.split(after_heading, 1)[1]
    for block in re.split(r"\n\s*\n", text):
        stripped = block.strip()
        if not stripped or stripped.startswith(("#", "[![", ">", "---")):
            continue
        return stripped
    raise AssertionError("no prose paragraph found")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


@pytest.mark.parametrize(("path", "heading", "phrase", "link"), ENTRY_POINTS)
def test_opening_paragraph_describes_a_kit_and_links_the_quickstart(
    path: str, heading: str | None, phrase: str, link: str
) -> None:
    opening = _opening_paragraph(_read(path), heading)
    assert phrase in opening, f"{path}: the opening must say {phrase!r}"
    assert link in opening, f"{path}: the opening must link the Quickstart ({link})"


def test_pyproject_description_describes_a_kit() -> None:
    data = tomllib.loads(_read("pyproject.toml"))
    description = data["project"]["description"]
    assert "development kit" in description
    for phrase in FORBIDDEN_EVERYWHERE:
        assert phrase.lower() not in description.lower()


@pytest.mark.parametrize(
    "path",
    [entry[0] for entry in ENTRY_POINTS]
    + [
        "docs/platform/library-usage.md",
        "docs/platform_es/library-usage.md",
    ],
)
def test_no_entry_point_describes_a_single_bot_or_customer(path: str) -> None:
    text = _read(path)
    offenders = [p for p in FORBIDDEN_EVERYWHERE if p.lower() in text.lower()]
    assert not offenders, f"{path} still frames the project as: {offenders}"
    assert "ACME runtime" not in text and "runtime de ACME" not in text


@pytest.mark.parametrize("path", sorted(OPTIONAL_PIECES))
def test_architecture_overview_has_no_customer_specific_names(path: str) -> None:
    text = _read(path)
    offenders = [p for p in FORBIDDEN_IN_ARCHITECTURE if p in text]
    assert not offenders, f"{path} names a customer's systems: {offenders}"


@pytest.mark.parametrize("path", sorted(OPTIONAL_PIECES))
def test_architecture_twins_name_the_same_optional_pieces(path: str) -> None:
    opening = _read(path).split("\n## ", 2)[1]  # the executive summary section
    for piece in OPTIONAL_PIECES[path]:
        assert piece in opening.lower(), f"{path}: summary must name {piece!r}"


#: (path, phrase that qualifies "optional" as a runtime choice, the install fact)
RUNTIME_QUALIFIERS = (
    ("README.md", "at runtime", "installing the kit"),
    ("AGENTS.md", "at runtime", "installing the kit"),
    ("docs/architecture/agent-platform.md", "at runtime", "installing the kit"),
    (
        "docs/architecture_es/agent-platform.md",
        "en tiempo de ejecución",
        "instalar el kit",
    ),
)

#: Wording that reads as "you can skip these when you install".
INSTALL_OPTIONAL_WORDING = (
    "leave the rest",
    "dejan las demás",
    "add when you need it",
)


@pytest.mark.parametrize(("path", "runtime", "install"), RUNTIME_QUALIFIERS)
def test_optional_pieces_are_optional_at_runtime_not_at_install(
    path: str, runtime: str, install: str
) -> None:
    """A plain install still pulls fastapi, asyncpg, redis and torch (#188)."""
    text = _read(path)
    assert runtime in text, f"{path}: say the pieces are optional {runtime!r}"
    assert install in text, f"{path}: say what {install!r} still brings"
    offenders = [w for w in INSTALL_OPTIONAL_WORDING if w in text.lower()]
    assert not offenders, f"{path} implies an optional install: {offenders}"


def _section(text: str, heading: str) -> str:
    """Body of the `## heading` section, up to the next `## ` heading."""
    body = text.split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


SHIPPED_SECTIONS = (
    (
        "docs/architecture/agent-platform.md",
        "What the kit ships and what it leaves to you",
    ),
    ("docs/architecture_es/agent-platform.md", "Qué entrega el kit y qué te deja a ti"),
)


def test_shipped_section_is_translated_and_makes_no_first_agent_claim() -> None:
    en, es = (_section(_read(p), h) for p, h in SHIPPED_SECTIONS)
    assert en.count("\n### ") == es.count("\n### ") == 2
    assert en.count("\n- ") == es.count("\n- ")
    for path, heading in SHIPPED_SECTIONS:
        body = _section(_read(path), heading).lower()
        assert "most people" not in body and "la mayoría" not in body
