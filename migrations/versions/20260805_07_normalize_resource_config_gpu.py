"""Normalize generic resource-config GPU keys for deterministic upserts."""

import sqlalchemy as sa
from alembic import op

revision = "20260805_07"
down_revision = "20260805_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE persona_resource_configs SET gpu_name = '*' WHERE gpu_name IS NULL")
    with op.batch_alter_table("persona_resource_configs") as batch_op:
        batch_op.alter_column(
            "gpu_name",
            existing_type=sa.String(length=180),
            nullable=False,
            server_default="*",
        )


def downgrade() -> None:
    with op.batch_alter_table("persona_resource_configs") as batch_op:
        batch_op.alter_column(
            "gpu_name",
            existing_type=sa.String(length=180),
            nullable=True,
            server_default=None,
        )
