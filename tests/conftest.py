"""Shared pytest configuration.

D-014 S5 hardening — the Settings security validator fails CLOSED at boot when
adapter runtimes are configured without an adapter API key, or when the Meta
webhook secret is empty (see ``agentsys.config.Settings``). The test suite is
not production: it opts into the insecure/dev mode by default so unrelated
tests (embeddings, RAG, health, etc.) that build ``Settings`` with empty
secrets keep booting. Tests that exercise the security boundary construct
``Settings(..., allow_insecure=False)`` explicitly, which overrides this env
default (init kwargs win over environment variables in pydantic-settings).

This is set at conftest import time — before any test module imports
``agentsys.main`` (whose module-level ``app = create_app()`` would otherwise
trip the fail-closed validator during collection). It CANNOT be an autouse
fixture: fixtures run after collection, and collection is what explodes.

The assignment is unconditional, NOT ``setdefault``. ``setdefault`` is a no-op
against an inherited value, so any environment that exports ALLOW_INSECURE=false
(hardened CI, or a developer checking a production-like config) turned the whole
suite into four collection-time ValidationErrors with nothing pointing at the
cause. The suite's need for the dev mode is not negotiable by the ambient
environment; the security boundary is still tested honestly because those tests
pass ``allow_insecure=False`` as an init kwarg, which outranks the env var.
``tests/test_conftest_contract.py`` pins this.
"""

import os

os.environ["ALLOW_INSECURE"] = "true"
