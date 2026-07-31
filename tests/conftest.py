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
trip the fail-closed validator during collection).
"""

import os

os.environ.setdefault("ALLOW_INSECURE", "true")
