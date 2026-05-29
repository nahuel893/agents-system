"""Tests for phone normalization using Google's phonenumbers library."""

from __future__ import annotations

import pytest

from agentsys.services.clients import normalize_argentine_mobile, normalize_phone


# ---------------------------------------------------------------------------
# Valid Argentine mobile inputs (must all normalize to +5491123456789)
# ---------------------------------------------------------------------------


def test_normalize_already_e164() -> None:
    """E.164 input passes through unchanged."""
    assert normalize_phone("+5491123456789") == "+5491123456789"


def test_normalize_no_plus_with_country_code() -> None:
    """Numbers with 549 prefix but no '+' get normalized."""
    assert normalize_phone("5491123456789") == "+5491123456789"


def test_normalize_argentine_landline_with_dashes() -> None:
    """Argentine local format parses as landline (no mobile '9' marker)."""
    # "11-2345-6789" is ambiguous in AR — without context it parses as landline.
    # Mobile-aware normalization happens in the medallion sync (1A.4d).
    assert normalize_phone("11-2345-6789") == "+541123456789"


def test_normalize_argentine_landline_with_parens() -> None:
    """Argentine format with parens around area code."""
    assert normalize_phone("(011) 2345-6789") == "+541123456789"


def test_normalize_argentine_mobile_with_spaces() -> None:
    """Spaces don't break parsing when '9' marker is present."""
    assert normalize_phone("54 9 11 2345 6789") == "+5491123456789"


# ---------------------------------------------------------------------------
# Invalid inputs (must raise ValueError)
# ---------------------------------------------------------------------------


def test_normalize_empty_raises() -> None:
    with pytest.raises(ValueError):
        normalize_phone("")


def test_normalize_garbage_raises() -> None:
    with pytest.raises(ValueError):
        normalize_phone("abc")


def test_normalize_too_short_raises() -> None:
    with pytest.raises(ValueError):
        normalize_phone("123")


# ---------------------------------------------------------------------------
# Argentine mobile-aware normalization (used by medallion sync)
# ---------------------------------------------------------------------------


def test_normalize_argentine_mobile_local_dashes() -> None:
    """Local format with dashes — known to be mobile, force '9' prefix."""
    assert normalize_argentine_mobile("11-2345-6789") == "+5491123456789"


def test_normalize_argentine_mobile_local_parens() -> None:
    """Local format with parens — known to be mobile."""
    assert normalize_argentine_mobile("(011) 2345-6789") == "+5491123456789"


def test_normalize_argentine_mobile_already_correct() -> None:
    """Already-normalized mobile passes through."""
    assert normalize_argentine_mobile("+5491123456789") == "+5491123456789"


def test_normalize_argentine_mobile_with_country_code_no_nine() -> None:
    """+54 11... gets the '9' inserted."""
    assert normalize_argentine_mobile("+541123456789") == "+5491123456789"


def test_normalize_argentine_mobile_invalid_raises() -> None:
    with pytest.raises(ValueError):
        normalize_argentine_mobile("abc")


def test_normalize_argentine_mobile_empty_raises() -> None:
    with pytest.raises(ValueError):
        normalize_argentine_mobile("")
