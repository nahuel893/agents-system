"""What happens when a tool raises (#289).

A tool that raises an ``Exception`` does not abort the turn: the model gets an
error ``ToolMessage`` it can react to (shorten an argument, change approach).
Two things stay out of that path, and both are defined here, in one place:

* ``BaseException`` (``asyncio.CancelledError``, ``KeyboardInterrupt``,
  ``SystemExit``) is never caught. The turn's own total timeout works by
  cancelling the task, so swallowing it would defeat the limit.
* ``aborting_exceptions()`` -- the exceptions that carry a security or
  permission decision. They propagate and abort the turn: showing them to the
  model as a retryable error would invite it to try around the decision.

The interceptor's own denials (``PolicyViolation``: not in the surface,
permission revoked, revalidation required, memory policy, missing subject)
are not in this set because they were never raised out of the turn: they are
already returned to the model as a "blocked" error result, and they do not
count toward ``max_tool_errors``.
"""

from __future__ import annotations

import functools
import importlib
import re

#: The most characters of an exception's message the model is shown.
MAX_ERROR_MESSAGE_CHARS = 300

#: (module, class, why it aborts the turn). A module whose optional extra is
#: not installed cannot raise its exceptions, so it is skipped.
_ABORTING = (
    (
        "agents_system.permissions.errors",
        "AgentPermissionError",
        "the permission model failed: a decision about what may run",
    ),
    (
        "agents_system.proactive",
        "ProactivePermissionError",
        "the caller lacks send:proactive within the deploy grants",
    ),
    (
        "agents_system.proactive",
        "RecipientNotAuthorizedError",
        "the deployment's directory does not authorize the recipient",
    ),
    (
        "agents_system.services.sql_guard",
        "QueryRejectedError",
        "the SQL guard refused a query that the connector did not answer",
    ),
    (
        "agents_system.services.sql_query",
        "UnsafeQueryRoleError",
        "the database role is unsafe for model-authored SQL",
    ),
    (
        "agents_system.services.scope_postgres",
        "UnfencedRoleError",
        "the database role bypasses row-level security: user data is not fenced",
    ),
)


@functools.cache
def _resolved() -> tuple[type[Exception], ...]:
    found: list[type[Exception]] = []
    for module_name, class_name, _why in _ABORTING:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError):
            continue
        found.append(cls)
    return tuple(found)


def aborting_exceptions() -> tuple[type[Exception], ...]:
    """The exception classes that abort the turn instead of reaching the model."""
    return _resolved()


def tool_error_text(tool_name: str, error: Exception) -> str:
    """What the model is told when *tool_name* raised *error*.

    Short and on one line: the tool name, the exception class and its message
    truncated to ``MAX_ERROR_MESSAGE_CHARS``. Never a traceback. The message
    is truncated and flattened, not scrubbed: no secret redaction helper
    exists in the platform, so a tool must not put a secret in its exception.
    """
    detail = re.sub(r"\s+", " ", str(error)).strip()
    if len(detail) > MAX_ERROR_MESSAGE_CHARS:
        detail = detail[:MAX_ERROR_MESSAGE_CHARS].rstrip() + "..."
    head = f"Tool {tool_name!r} failed: {type(error).__name__}"
    return f"{head}: {detail}" if detail else head
