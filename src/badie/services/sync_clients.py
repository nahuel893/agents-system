"""Sync pipeline: medallion ``gold.dim_cliente`` → bot ``clients``.

Reads clients from the warehouse, normalizes mobile phones to E.164, and
UPSERTs into the local clients table by ``external_id``. Idempotent.

Skips:
  - Rows with ``anulado = True``
  - Rows without a parseable ``telefono_movil``
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from badie.models.tables import Client
from badie.services.clients import normalize_argentine_mobile

logger = structlog.get_logger()


@dataclass(frozen=True)
class SyncResult:
    processed: int
    errors: int


_SQL = (
    "SELECT id_cliente, razon_social, fantasia, telefono_movil, "
    "id_lista_precio, des_canal_mkt, des_localidad, anulado "
    "FROM {table}"
)


async def sync_clients(
    medallion_session: AsyncSession,
    bot_session: AsyncSession,
) -> SyncResult:
    """Sync ``gold.dim_cliente`` rows into ``clients`` (UPSERT by external_id).

    Per row:
      - Skip if ``anulado`` is true
      - Skip if telefono_movil is empty / unparseable (counted as error)
      - Normalize phone via ``normalize_argentine_mobile``
      - Use ``fantasia`` or fall back to ``razon_social`` for ``name``
      - Set ``active = True`` (registered client, not auto-registered)

    Returns ``SyncResult`` with processed and skipped counts.
    """
    try:
        result = await medallion_session.execute(text(_SQL.format(table="gold.dim_cliente")))
    except Exception:
        result = await medallion_session.execute(text(_SQL.format(table="dim_cliente")))
    rows = list(result.mappings().all())

    processed = 0
    errors = 0

    for row in rows:
        if row.get("anulado"):
            continue

        raw_phone = row.get("telefono_movil") or ""
        if not raw_phone.strip():
            errors += 1
            logger.warning("sync_clients.empty_phone", id_cliente=row.get("id_cliente"))
            continue

        try:
            phone = normalize_argentine_mobile(raw_phone)
        except ValueError:
            errors += 1
            logger.warning(
                "sync_clients.invalid_phone",
                id_cliente=row.get("id_cliente"),
                raw=raw_phone,
            )
            continue

        external_id = int(row["id_cliente"])
        name = (row.get("fantasia") or row.get("razon_social") or "").strip()

        existing = await bot_session.execute(
            select(Client).where(Client.external_id == external_id)
        )
        current = existing.scalar_one_or_none()
        if current is None:
            bot_session.add(
                Client(
                    external_id=external_id,
                    phone_number=phone,
                    name=name,
                    business_type=row.get("des_canal_mkt"),
                    zone=row.get("des_localidad"),
                    price_list_id=row.get("id_lista_precio"),
                    active=True,
                )
            )
        else:
            current.phone_number = phone
            current.name = name
            current.business_type = row.get("des_canal_mkt")
            current.zone = row.get("des_localidad")
            current.price_list_id = row.get("id_lista_precio")
            current.active = True

        processed += 1

    await bot_session.commit()
    logger.info("sync_clients.complete", processed=processed, errors=errors)
    return SyncResult(processed=processed, errors=errors)
