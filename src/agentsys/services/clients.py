"""Client lookup service — phone normalization and client resolution."""

from __future__ import annotations

import phonenumbers
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.tables import Client

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


def normalize_argentine_mobile(raw: str) -> str:
    """Normalize a phone known to be an Argentine mobile to E.164 (+549...).

    Argentina's quirk: in international format, mobiles get a ``9`` after the
    country code (``+5491123456789``), but locals dial without it
    (``11-2345-6789``). phonenumbers can't infer mobile from ambiguous input,
    so this helper forces the ``9`` prefix when missing.

    Args:
        raw: Raw mobile phone string in any format.

    Returns:
        E.164 mobile phone with ``+549`` prefix.

    Raises:
        ValueError: if the input cannot be parsed.
    """
    e164 = normalize_phone(raw)
    if e164.startswith("+549"):
        return e164
    if e164.startswith("+54") and not e164.startswith("+549"):
        # Insert "9" right after country code
        return "+549" + e164[len("+54"):]
    return e164


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
