"""Client lookup service — phone normalization and client resolution."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from badie.models.tables import Client

logger = structlog.get_logger()


def normalize_phone(raw: str) -> str:
    """Normalize phone number to E.164 format (prefix with '+' if missing)."""
    if not raw.startswith("+"):
        return f"+{raw}"
    return raw


async def lookup_or_create_client(
    session: AsyncSession, phone: str
) -> Client:
    """Find client by phone or auto-register as inactive.

    If the phone is unknown, creates a new Client with active=False
    so the bot can notify them to register with a human.

    Args:
        session: SQLAlchemy async session.
        phone: Normalized E.164 phone number.

    Returns:
        The existing or newly created Client.
    """
    result = await session.execute(
        select(Client).where(Client.phone_number == phone)
    )
    client = result.scalar_one_or_none()

    if client is not None:
        return client

    client = Client(phone_number=phone, name="Pendiente de alta", active=False)
    session.add(client)
    await session.commit()
    await session.refresh(client)
    return client
