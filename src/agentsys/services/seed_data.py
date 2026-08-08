"""Deterministic demo-data generator for Distribuidora BADIE.

Populates ``clients``, ``orders``, and ``order_items`` with realistic,
internally-consistent data so a BI/analytics agent has something to query —
right now every analytics table is empty.

The module is split in two halves on purpose:

- Generation (``generate_clients`` / ``generate_orders`` /
  ``generate_demo_dataset``) is pure: no I/O, no SQLAlchemy session, no
  ``datetime.now()``. Given the same seed it returns byte-identical data,
  which is what makes it unit-testable without a database.
- Persistence (``seed_database``) is the only place that touches a session.
  It UPSERTs by ``external_id`` (clients, orders) so re-running the script is
  idempotent — never duplicates rows.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.tables import Client, Order, OrderItem

# ---------------------------------------------------------------------------
# Constants — fixed so every run (any machine, any day) is byte-identical.
# Do NOT derive any of these from wall-clock time; determinism is a hard
# requirement (see AGENTS.md task: repeated runs must be idempotent).
# ---------------------------------------------------------------------------

DEFAULT_SEED = 20260101
"""Fixed RNG seed for the demo dataset."""

ANCHOR_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
"""Fixed reference point for "now" when spreading orders across the trailing
12 months. Using real wall-clock time here would make the dataset different
on every run, which would break both determinism and idempotency (the
"same" order would get a new created_at on every re-run)."""

DEFAULT_NUM_CLIENTS = 32
DEFAULT_NUM_ORDERS = 320

EXTERNAL_ID_NAMESPACE = -900_000
"""Demo client external_ids are negative and start here so they can never
collide with real ``id_cliente`` values synced from the medallion warehouse
(always positive there) — see services/sync_clients.py."""

ORDER_EXTERNAL_ID_PREFIX = "DEMO-ORD"

ZONES: tuple[str, ...] = (
    "CABA - Palermo",
    "CABA - Once",
    "CABA - Flores",
    "CABA - Caballito",
    "Avellaneda",
    "Quilmes",
    "Lomas de Zamora",
    "La Matanza - San Justo",
    "Morón",
    "Tigre",
    "San Isidro",
    "Vicente López",
    "Lanús",
    "Berazategui",
)

BUSINESS_TYPES: tuple[str, ...] = (
    "kiosco",
    "almacén",
    "bar",
    "restaurante",
    "supermercado",
    "verdulería",
    "rotisería",
    "pizzería",
)

_NAME_POOL: tuple[str, ...] = (
    "Don Pedro", "La Esquina", "El Progreso", "Doña María", "San Martín",
    "Los Amigos", "La Fortuna", "El Sol", "Central", "Del Barrio",
    "La Reina", "El Trébol", "Santa Rosa", "La Familia", "El Faro",
    "Nueva Era", "La Paz", "El Puente", "Don José", "La Victoria",
    "El Ceibo", "Los Pinos", "La Perla", "El Águila", "Santa Fe",
    "La Estrella", "El Rincón", "Don Carlos", "La Unión", "El Norte",
    "Los Cedros", "La Merced",
)

# (sku, description, unit_price) — plausible ARS prices for a Buenos Aires
# beverage distributor, mid-2026.
CATALOG: tuple[tuple[str, str, Decimal], ...] = (
    ("BEB-001", "Coca-Cola 2.25L", Decimal("2200.00")),
    ("BEB-002", "Coca-Cola Zero 2.25L", Decimal("2200.00")),
    ("BEB-003", "Sprite 2.25L", Decimal("2100.00")),
    ("BEB-004", "Fanta Naranja 2.25L", Decimal("2100.00")),
    ("BEB-005", "Quilmes Cristal 1L retornable", Decimal("1800.00")),
    ("BEB-006", "Quilmes Cristal lata 473ml", Decimal("1200.00")),
    ("BEB-007", "Stella Artois botella 473ml", Decimal("1600.00")),
    ("BEB-008", "Brahma lata 473ml", Decimal("1100.00")),
    ("BEB-009", "Fernet Branca 750ml", Decimal("9500.00")),
    ("BEB-010", "Aperol 750ml", Decimal("11500.00")),
    ("BEB-011", "Gancia 950ml", Decimal("4200.00")),
    ("BEB-012", "Agua Villavicencio sin gas 2L", Decimal("900.00")),
    ("BEB-013", "Agua Villavicencio con gas 2L", Decimal("950.00")),
    ("BEB-014", "Gatorade Naranja 500ml", Decimal("1500.00")),
    ("BEB-015", "Powerade Mora 500ml", Decimal("1450.00")),
    ("BEB-016", "Jugo Cepita Naranja 1.5L", Decimal("1700.00")),
    ("BEB-017", "Paso de los Toros Pomelo 2.25L", Decimal("2050.00")),
    ("BEB-018", "Sidra Real 720ml", Decimal("3200.00")),
    ("BEB-019", "Vino Toro Tinto 1L", Decimal("2600.00")),
    ("BEB-020", "Speed 269ml", Decimal("1300.00")),
)

ORDER_STATUSES: tuple[str, ...] = ("confirmed", "pending", "cancelled")
_STATUS_WEIGHTS: tuple[int, ...] = (70, 15, 15)


# ---------------------------------------------------------------------------
# Pure data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClientSeed:
    external_id: int
    phone_number: str
    name: str
    business_type: str
    zone: str
    price_list_id: int
    active: bool


@dataclass(frozen=True, slots=True)
class OrderItemSeed:
    sku: str
    description: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal


@dataclass(frozen=True, slots=True)
class OrderSeed:
    external_id: str
    client_external_id: int
    status: str
    created_at: datetime
    confirmed_at: datetime | None
    cutoff_at: datetime | None
    notes: str | None
    items: tuple[OrderItemSeed, ...]
    total_amount: Decimal


@dataclass(frozen=True, slots=True)
class DemoDataset:
    clients: tuple[ClientSeed, ...]
    orders: tuple[OrderSeed, ...]


# ---------------------------------------------------------------------------
# Pure generation — no I/O, fully deterministic for a given seed.
# ---------------------------------------------------------------------------


def _make_phone_number(index: int) -> str:
    """Deterministic E.164-ish AR mobile, e.g. '5491112345678'.

    ``549`` (country 54 + mobile flag 9) + ``11`` (CABA/GBA area code) + an
    8-digit subscriber number derived from *index*.
    """
    return f"54911{20_000_000 + index:08d}"


def generate_clients(
    rng: random.Random, count: int = DEFAULT_NUM_CLIENTS
) -> tuple[ClientSeed, ...]:
    """Generate *count* deterministic demo clients.

    ``external_id`` is a stable natural key derived from the client's index
    (``EXTERNAL_ID_NAMESPACE - index``), which is what makes ``seed_database``
    idempotent across re-runs with the same *count*.
    """
    clients = []
    for i in range(count):
        business_type = rng.choice(BUSINESS_TYPES)
        zone = rng.choice(ZONES)
        name = f"{business_type.capitalize()} {_NAME_POOL[i % len(_NAME_POOL)]}"
        clients.append(
            ClientSeed(
                external_id=EXTERNAL_ID_NAMESPACE - i,
                phone_number=_make_phone_number(i),
                name=name,
                business_type=business_type,
                zone=zone,
                price_list_id=rng.randint(1, 4),
                active=rng.random() > 0.08,
            )
        )
    return tuple(clients)


def _generate_order_items(rng: random.Random) -> tuple[OrderItemSeed, ...]:
    n_items = rng.randint(1, 6)
    picks = rng.sample(CATALOG, k=min(n_items, len(CATALOG)))
    items = []
    for sku, description, unit_price in picks:
        quantity = rng.randint(1, 24)
        # unit_price already has exactly 2 decimal digits, so this product
        # never needs rounding — quantize only normalizes the exponent.
        subtotal = (unit_price * quantity).quantize(Decimal("0.01"))
        items.append(
            OrderItemSeed(
                sku=sku,
                description=description,
                quantity=quantity,
                unit_price=unit_price,
                subtotal=subtotal,
            )
        )
    return tuple(items)


def _pick_status(rng: random.Random) -> str:
    return rng.choices(ORDER_STATUSES, weights=_STATUS_WEIGHTS, k=1)[0]


def generate_orders(
    rng: random.Random,
    clients: tuple[ClientSeed, ...],
    count: int = DEFAULT_NUM_ORDERS,
    anchor_now: datetime = ANCHOR_NOW,
) -> tuple[OrderSeed, ...]:
    """Generate *count* deterministic orders spread over the trailing 12 months.

    Each order's ``total_amount`` is the exact ``Decimal`` sum of its items'
    subtotals — computed here, not left for the caller to derive, so the
    invariant ``orders.total_amount == SUM(order_items.subtotal)`` holds by
    construction.
    """
    if not clients:
        return ()

    orders = []
    for i in range(count):
        client = rng.choice(clients)
        items = _generate_order_items(rng)
        total_amount = sum((item.subtotal for item in items), start=Decimal("0.00"))

        days_ago = rng.uniform(0, 365)
        created_at = anchor_now - timedelta(days=days_ago)
        status = _pick_status(rng)

        confirmed_at: datetime | None = None
        cutoff_at: datetime | None = None
        if status == "confirmed":
            confirmed_at = created_at + timedelta(hours=rng.uniform(0.5, 48))
            cutoff_at = created_at + timedelta(hours=rng.uniform(1, 6))
        elif status == "pending":
            cutoff_at = created_at + timedelta(hours=rng.uniform(1, 6))

        orders.append(
            OrderSeed(
                external_id=f"{ORDER_EXTERNAL_ID_PREFIX}-{i:06d}",
                client_external_id=client.external_id,
                status=status,
                created_at=created_at,
                confirmed_at=confirmed_at,
                cutoff_at=cutoff_at,
                notes=None,
                items=items,
                total_amount=total_amount,
            )
        )
    return tuple(orders)


def generate_demo_dataset(
    seed: int = DEFAULT_SEED,
    num_clients: int = DEFAULT_NUM_CLIENTS,
    num_orders: int = DEFAULT_NUM_ORDERS,
) -> DemoDataset:
    """Generate the full demo dataset deterministically from *seed*.

    Calling this twice with the same arguments returns byte-for-byte
    identical data — required so ``scripts/seed_demo_data.py`` is idempotent
    and safe to re-run.
    """
    rng = random.Random(seed)
    clients = generate_clients(rng, num_clients)
    orders = generate_orders(rng, clients, num_orders)
    return DemoDataset(clients=clients, orders=orders)


# ---------------------------------------------------------------------------
# I/O — the only part of this module that touches a database session.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedResult:
    clients_inserted: int
    clients_updated: int
    orders_inserted: int
    orders_updated: int


async def seed_database(session: AsyncSession, dataset: DemoDataset) -> SeedResult:
    """UPSERT *dataset* into ``clients`` / ``orders`` / ``order_items``.

    Idempotent: matches existing rows by ``external_id``. Re-running with the
    same dataset never creates duplicate clients or orders — an existing
    order's items are replaced wholesale so they stay consistent with the
    freshly generated set.

    Money fields are converted to ``float`` at this boundary to match the
    ORM's declared ``Mapped[float | None]`` columns; all arithmetic upstream
    (in ``generate_orders`` / ``_generate_order_items``) is done in exact
    ``Decimal``, so the conversion happens exactly once, per value, at the
    edge — never mid-computation.
    """
    clients_inserted = 0
    clients_updated = 0
    client_id_by_external: dict[int, int] = {}

    for c in dataset.clients:
        existing_client = (
            await session.execute(
                select(Client).where(Client.external_id == c.external_id)
            )
        ).scalar_one_or_none()
        if existing_client is None:
            row = Client(
                external_id=c.external_id,
                phone_number=c.phone_number,
                name=c.name,
                business_type=c.business_type,
                zone=c.zone,
                price_list_id=c.price_list_id,
                active=c.active,
            )
            session.add(row)
            await session.flush()
            client_id_by_external[c.external_id] = row.id
            clients_inserted += 1
        else:
            existing_client.phone_number = c.phone_number
            existing_client.name = c.name
            existing_client.business_type = c.business_type
            existing_client.zone = c.zone
            existing_client.price_list_id = c.price_list_id
            existing_client.active = c.active
            client_id_by_external[c.external_id] = existing_client.id
            clients_updated += 1

    orders_inserted = 0
    orders_updated = 0

    for o in dataset.orders:
        client_id = client_id_by_external.get(o.client_external_id)
        existing_order = (
            await session.execute(
                select(Order).where(Order.external_id == o.external_id)
            )
        ).scalar_one_or_none()

        if existing_order is None:
            order_row = Order(
                external_id=o.external_id,
                client_id=client_id,
                status=o.status,
                created_at=o.created_at,
                confirmed_at=o.confirmed_at,
                cutoff_at=o.cutoff_at,
                total_amount=float(o.total_amount),
                notes=o.notes,
            )
            session.add(order_row)
            await session.flush()
            orders_inserted += 1
            order_id = order_row.id
        else:
            existing_order.client_id = client_id
            existing_order.status = o.status
            existing_order.created_at = o.created_at
            existing_order.confirmed_at = o.confirmed_at
            existing_order.cutoff_at = o.cutoff_at
            existing_order.total_amount = float(o.total_amount)
            existing_order.notes = o.notes
            orders_updated += 1
            order_id = existing_order.id
            await session.execute(
                delete(OrderItem).where(OrderItem.order_id == order_id)
            )

        for item in o.items:
            session.add(
                OrderItem(
                    order_id=order_id,
                    sku=item.sku,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=float(item.unit_price),
                    subtotal=float(item.subtotal),
                )
            )

    await session.commit()
    return SeedResult(
        clients_inserted=clients_inserted,
        clients_updated=clients_updated,
        orders_inserted=orders_inserted,
        orders_updated=orders_updated,
    )
