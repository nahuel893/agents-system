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
from typing import Any

os.environ["ALLOW_INSECURE"] = "true"


def create_test_app(**overrides: Any) -> Any:
    """`create_app` with this deployment's composition filled in.

    `create_app` has no default registry factory on purpose — the platform
    owns no connectors, so an application must say what it boots with. Most
    tests here do not care which registry that is (they set
    `app.state.runtimes` directly, or never reach a tool at all), so this
    supplies the same wiring `main.py` uses at module scope.

    A test that IS about composition passes its own arguments instead.
    """
    from agentsys.connectors.rag_connector import build_acme_rag_registry
    from agentsys.main import create_app
    from agentsys.services.clients import ClientDirectory
    from agentsys.services.conversation_log import ConversationLogRecorder

    kwargs: dict[str, Any] = {
        "registry_factory": build_acme_rag_registry,
        "participant_directory": ClientDirectory(),
        "conversation_recorder": ConversationLogRecorder(),
    }
    kwargs.update(overrides)
    return create_app(**kwargs)
