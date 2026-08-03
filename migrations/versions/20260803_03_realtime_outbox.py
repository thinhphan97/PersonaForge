"""Add durable realtime event outbox."""

import sqlalchemy as sa
from alembic import op

revision = "20260803_03"
down_revision = "20260803_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "realtime_outbox",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_realtime_outbox_session_id", "realtime_outbox", ["session_id"])
    op.create_index("ix_realtime_outbox_created_at", "realtime_outbox", ["created_at"])
    op.create_index("ix_realtime_outbox_published_at", "realtime_outbox", ["published_at"])


def downgrade() -> None:
    op.drop_index("ix_realtime_outbox_published_at", table_name="realtime_outbox")
    op.drop_index("ix_realtime_outbox_created_at", table_name="realtime_outbox")
    op.drop_index("ix_realtime_outbox_session_id", table_name="realtime_outbox")
    op.drop_table("realtime_outbox")
