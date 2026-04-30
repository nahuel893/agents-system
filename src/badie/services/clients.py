"""Client lookup service — phone normalization and client resolution."""

from __future__ import annotations

import phonenumbers
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from badie.models.tables import Client

logger = structlog.get_logger()

DEFAULT_REGION = "AR"


def normalize_phone(raw: str) -> str:
    """Normalize a phone number to E.164 format (e.g. ``+5491123456789``).

    Uses Google's libphonenumber to parse messy input (dashes, parens,
    spaces, missing country code). Defaults to Argentina (``AR``) when
    no country code is present.

    Args:
        raw: Raw phone string in any format.

    Returns:
        E.164 formatted phone (``+`` followed by digits).

    Raises:
        ValueError: if the input cannot be parsed as a valid number.
    """
    if not raw or not raw.strip():
        raise ValueError("phone is empty")

    try:
        parsed = phonenumbers.parse(raw, DEFAULT_REGION)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"invalid phone: {raw!r}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"invalid phone: {raw!r}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


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
