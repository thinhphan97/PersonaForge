"""Add resumable chat message state and cursors."""

import sqlalchemy as sa
from alembic import op

revision = "20260803_02"
down_revision = "20260803_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("sequence", sa.Integer(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "message_status",
            sa.String(16),
            nullable=False,
            server_default="complete",
        ),
    )
    op.add_column(
        "chat_messages",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE chat_messages
        SET sequence = (
            SELECT COUNT(*)
            FROM chat_messages AS earlier
            WHERE earlier.session_id = chat_messages.session_id
              AND (
                earlier.created_at < chat_messages.created_at
                OR (
                  earlier.created_at = chat_messages.created_at
                  AND earlier.message_id <= chat_messages.message_id
                )
              )
        ),
        updated_at = created_at
        """
    )
    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.alter_column(
            "sequence",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
    op.create_index(
        "uq_chat_messages_session_sequence",
        "chat_messages",
        ["session_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "ix_chat_messages_updated_at",
        "chat_messages",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_updated_at", table_name="chat_messages")
    op.drop_index("uq_chat_messages_session_sequence", table_name="chat_messages")
    op.drop_column("chat_messages", "updated_at")
    op.drop_column("chat_messages", "message_status")
    op.drop_column("chat_messages", "revision")
    op.drop_column("chat_messages", "sequence")
