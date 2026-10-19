"""Name the channel every inbox row arrived on.

Revision ID: 006
Revises: 005
Create Date: 2026-09-27

#22 extracts a channel port: the durable inbox is shared by every messaging
channel (the built-in WhatsApp channel and any channel a deployment
registers), and the deferred worker must hand each row back to the adapter of
the channel it came from -- to find the turn in the stored payload and to send
the reply. This column is that discriminator.

Additive and non-blocking for existing data: ``ADD COLUMN ... NOT NULL
DEFAULT 'whatsapp'`` with a constant default is a catalog-only change in
PostgreSQL 11+ (no table rewrite), and every row accepted before this
migration is a Meta WhatsApp row, which is exactly what the default says.
The WhatsApp write path is unchanged: its ids and conversation keys stay
un-prefixed, and the ``uq_webhook_inbox_meta_message_id`` dedup constraint is
untouched. Other channels store their ids prefixed with their own name
(``services.outbox.channel_scoped_key``), so one global unique constraint
still dedups every channel without two channels ever colliding.

Downgrade refuses while any row names a channel other than WhatsApp: dropping
the column would silently relabel those rows as Meta envelopes, and an
incomplete one would then be processed by the wrong adapter. Once only
WhatsApp rows remain, dropping the column loses nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_NON_WHATSAPP_ROWS_EXIST = """
    SELECT EXISTS (SELECT 1 FROM webhook_inbox WHERE channel <> 'whatsapp')
"""


def upgrade() -> None:
    """Add the channel column; every existing row is a WhatsApp row."""
    op.add_column(
        "webhook_inbox",
        sa.Column(
            "channel",
            sa.Text(),
            server_default=sa.text("'whatsapp'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Refuse while another channel's rows exist; they cannot be relabeled."""
    op.execute("LOCK TABLE webhook_inbox IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(_NON_WHATSAPP_ROWS_EXIST)):
        raise RuntimeError(
            "Refusing to downgrade 006 while webhook_inbox holds rows of a "
            "channel other than 'whatsapp' -- dropping the column would "
            "relabel them as Meta WhatsApp rows"
        )
    op.drop_column("webhook_inbox", "channel")
