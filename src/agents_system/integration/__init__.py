"""Integration package — external service adapters."""

from agents_system.integration.openai_adapter import openai_router
from agents_system.integration.webhook import webhook_router

__all__ = ["openai_router", "webhook_router"]
