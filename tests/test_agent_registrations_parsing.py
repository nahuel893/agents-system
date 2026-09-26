"""`AGENT_REGISTRATIONS` and its value parser (ADR-004 PR4b-T1, design.md D5/D6).

The Settings-driven boot path no longer reads a role and a deployment out of
a `{deployment}__{role}` runtime id. The operator names each runtime id in
`AGENT_REGISTRATIONS` and maps it to `"{role}"` or `"{role}@{client}"`;
`main._parse_agent_registration` reads that one value shape, strictly.

What the lifespan builds from these entries is covered by `tests/test_main.py`
(the "env_registration" tests).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents_system.config import Settings
from agents_system.harness.loader import DefinitionError
from agents_system.main import _parse_agent_registration

# ---------------------------------------------------------------------------
# Settings.agent_registrations
# ---------------------------------------------------------------------------


def test_agent_registrations_accepts_a_mapping_of_id_to_role_value() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        agent_registrations={
            "my-sales-bot": "sales-agent@acme",
            "support": "support-agent",
        },
    )

    assert settings.agent_registrations == {
        "my-sales-bot": "sales-agent@acme",
        "support": "support-agent",
    }


def test_agent_registrations_is_decoded_from_json_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AGENT_REGISTRATIONS",
        '{"my-sales-bot": "sales-agent@acme", "support": "support-agent"}',
    )

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.agent_registrations == {
        "my-sales-bot": "sales-agent@acme",
        "support": "support-agent",
    }


def test_agent_registrations_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset registers nothing, and constructing Settings does not raise."""
    monkeypatch.delenv("AGENT_REGISTRATIONS", raising=False)

    assert Settings(_env_file=None).agent_registrations == {}  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "raw",
    [
        '["sales-agent"]',  # a list, not an id -> value mapping
        '{"support": 1}',  # a value that is not a string
        '{"support": null}',
        '{"support": ["sales-agent"]}',
    ],
)
def test_agent_registrations_rejects_a_value_that_is_not_a_string(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("AGENT_REGISTRATIONS", raw)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _parse_agent_registration
# ---------------------------------------------------------------------------


def test_parse_a_role_with_a_client() -> None:
    assert _parse_agent_registration("sales-agent@acme") == ("sales-agent", "acme")


def test_parse_a_role_without_a_client() -> None:
    assert _parse_agent_registration("support-agent") == ("support-agent", None)


def test_parse_takes_a_legacy_shaped_value_whole() -> None:
    """spec: 'A legacy-shaped id string is treated as opaque, not parsed' --
    `__` has no meaning in a registration value either: nothing splits
    `acme__sales-agent` into a deployment and a role."""
    assert _parse_agent_registration("acme__sales-agent") == (
        "acme__sales-agent",
        None,
    )


@pytest.mark.parametrize(
    "value",
    [
        "bad@extra@value",  # more than one separator
        "",
        "@acme",  # no role
        "sales-agent@",  # an empty client is not "no client"
        "sales agent",
        " sales-agent",
        "sales-agent@acme ",
        "../sales-agent",
        "sales-agent@../acme",
        "_generic__sales-agent",  # the removed sentinel is not a role name
        "-sales-agent",
        "sales-agent@_generic",
    ],
)
def test_parse_rejects_a_malformed_value_naming_it(value: str) -> None:
    with pytest.raises(DefinitionError) as exc_info:
        _parse_agent_registration(value)

    assert repr(value) in str(exc_info.value)


@pytest.mark.parametrize("value", [None, 42, ["sales-agent"]])
def test_parse_rejects_a_value_that_is_not_a_string(value: object) -> None:
    with pytest.raises(DefinitionError, match=type(value).__name__):
        _parse_agent_registration(value)  # type: ignore[arg-type]


def test_parse_does_not_echo_an_oversized_value() -> None:
    value = "sales-agent" + "@acme" * 1000

    with pytest.raises(DefinitionError) as exc_info:
        _parse_agent_registration(value)

    assert len(str(exc_info.value)) < 500
