"""Add identity, ownership and session access tables."""

import sqlalchemy as sa
from alembic import op

revision = "20260803_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("normalized_email", sa.String(320), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("normalized_username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("auth_session_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_session_id", sa.String(64), nullable=True),
    )
    op.create_table(
        "avatar_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("character_id", sa.String(100), nullable=False),
        sa.Column("owner_user_id", sa.String(64), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="private"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("avatar_state", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "chat_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.user_id"]),
    )
    op.create_table(
        "session_shares",
        sa.Column("share_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("avatar_sessions.session_id"),
            nullable=False,
        ),
        sa.Column(
            "grantee_user_id",
            sa.String(64),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(16), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.String(64),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "grantee_user_id"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.String(64),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("session_shares")
    op.drop_table("chat_messages")
    op.drop_table("avatar_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
