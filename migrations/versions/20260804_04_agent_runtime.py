"""Add Agent Runtime configuration and durable turn tracking."""

import sqlalchemy as sa
from alembic import op

revision = "20260804_04"
down_revision = "20260803_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "avatar_sessions",
        sa.Column(
            "agent_id",
            sa.String(100),
            nullable=False,
            server_default="default",
        ),
    )
    op.create_table(
        "agent_turns",
        sa.Column("turn_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("assistant_message_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_detail", sa.String(1000), nullable=True),
    )
    op.create_index("ix_agent_turns_session_id", "agent_turns", ["session_id"])
    op.create_index("ix_agent_turns_user_id", "agent_turns", ["user_id"])
    op.create_index("ix_agent_turns_agent_id", "agent_turns", ["agent_id"])
    op.create_index("ix_agent_turns_status", "agent_turns", ["status"])
    op.create_index("ix_agent_turns_started_at", "agent_turns", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_turns_started_at", table_name="agent_turns")
    op.drop_index("ix_agent_turns_status", table_name="agent_turns")
    op.drop_index("ix_agent_turns_agent_id", table_name="agent_turns")
    op.drop_index("ix_agent_turns_user_id", table_name="agent_turns")
    op.drop_index("ix_agent_turns_session_id", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_column("avatar_sessions", "agent_id")
