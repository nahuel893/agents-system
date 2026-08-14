"""Pins the contract ``tests/conftest.py`` owes the rest of the suite.

D-014 S5 moved the empty-secret check into a ``Settings`` model validator that
raises. ``agentsys.main`` ends with a module-level ``app = create_app()``, so
that validator runs during pytest COLLECTION, before any fixture exists. The
only place that can keep the suite collectable is conftest's import-time env
assignment — and if that assignment is written as ``os.environ.setdefault``, an
inherited ``ALLOW_INSECURE=false`` silently defeats it and every module that
imports ``agentsys.main`` dies as an unattributable collection error.

These tests fail loudly and by name when that regression is reintroduced.
"""

from __future__ import annotations

import os

from agentsys.config import Settings


def test_conftest_forces_insecure_mode_over_inherited_env() -> None:
    """conftest must OVERRIDE an inherited ALLOW_INSECURE, not defer to it."""
    assert os.environ["ALLOW_INSECURE"] == "true"


def test_ambient_environment_can_construct_settings() -> None:
    """``Settings()`` with no kwargs — exactly what ``get_settings()`` does at
    import time — must construct under the suite's environment.

    This is the collection-time invariant stated as a test: if it fails, the
    real symptom is not this assertion but four collection errors.
    """
    settings = Settings()
    assert settings.allow_insecure is True
