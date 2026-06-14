"""Integration package — external service adapters."""

from agentsys.integration.openai_adapter import openai_router
from agentsys.integration.webhook import webhook_router

__all__ = ["openai_router", "webhook_router"]
