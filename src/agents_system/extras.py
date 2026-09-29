"""Optional features, the extras that install them, and the error when one is missing.

A plain install of ``agents-system`` is the core a minimal agent needs (issue
#188): the agent API, the harness, the in-process runtime, and the audit and
metrics interfaces. Everything else is an extra of the distribution, declared
in ``pyproject.toml``::

    pip install "agents-system[server]"

Every module that imports an optional package at module level does it inside a
guard, so using a feature whose extra is not installed fails with a message
that names the extra instead of a bare ``No module named 'fastapi'``::

    try:
        from fastapi import FastAPI
    except ImportError as exc:
        raise_missing_extra("server", "The served app", exc)

``tests/test_core_install.py`` holds every module-level optional import to that
pattern, and ``EXTRA_MODULES`` to ``pyproject.toml``.
"""

from __future__ import annotations

from typing import NoReturn

__all__ = ["EXTRA_MODULES", "MissingExtraError", "raise_missing_extra"]

_DISTRIBUTION = "agents-system"

#: The database, and the durable conversation checkpointer on it (#24).
_POSTGRES = frozenset(
    {
        "sqlalchemy",
        "asyncpg",
        "alembic",
        "psycopg",
        "psycopg_pool",
        "langgraph.checkpoint.postgres",
    }
)

#: Each extra, and the top-level modules it provides. A missing module that is
#: not listed under the extra a guard names is re-raised unchanged: it is a
#: broken install, not a missing extra, and must not be reported as one.
#:
#: ``server`` and ``sql`` include the ``postgres`` extra, because the served
#: app and the SQL query tool both run on a database.
EXTRA_MODULES: dict[str, frozenset[str]] = {
    "server": frozenset({"fastapi", "starlette", "uvicorn", "redis"}) | _POSTGRES,
    "postgres": _POSTGRES,
    "sql": frozenset({"sqlglot"}) | _POSTGRES,
    "embeddings": frozenset(
        {"torch", "sentence_transformers", "transformers", "openai"}
    ),
    "openai": frozenset({"langchain_openai", "openai", "tiktoken"}),
    "anthropic": frozenset({"langchain_anthropic", "anthropic"}),
    "groq": frozenset({"langchain_groq", "groq"}),
    "ollama": frozenset({"langchain_ollama", "ollama"}),
}


class MissingExtraError(ModuleNotFoundError):
    """A feature was used, and the extra that installs it is not installed.

    A ``ModuleNotFoundError`` (so an ``ImportError``) whose ``name`` is the
    missing module, and whose ``extra`` is the extra to install.
    """

    def __init__(self, extra: str, feature: str, missing: str) -> None:
        super().__init__(
            f"{feature} needs the '{extra}' extra, which is not installed "
            f"(no module named {missing!r}). Install the package with it: "
            f"{_DISTRIBUTION}[{extra}]. The install section of "
            "docs/platform/library-usage.md lists every extra.",
            name=missing,
        )
        self.extra = extra
        self.feature = feature


def raise_missing_extra(extra: str, feature: str, error: ImportError) -> NoReturn:
    """Raise ``MissingExtraError`` when ``error`` is ``extra`` not being installed.

    Called from the ``except ImportError`` of a guarded import. Any other
    import error -- a module the extra does not provide, or a name missing
    from an installed package -- is re-raised as it is.
    """
    if isinstance(error, MissingExtraError):
        raise error
    missing = error.name or ""
    provided = EXTRA_MODULES[extra]
    if isinstance(error, ModuleNotFoundError) and any(
        missing == module or missing.startswith(module + ".") for module in provided
    ):
        raise MissingExtraError(extra, feature, missing) from error
    raise error
