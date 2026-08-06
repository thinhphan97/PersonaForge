"""Add the per-composite GPU duty-cycle sample used by profile aggregation."""

import sqlalchemy as sa
from alembic import op

revision = "20260805_06"
down_revision = "20260805_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("composite_resource_samples")
    }
    if "gpu_duty_cycle" not in columns:
        op.add_column(
            "composite_resource_samples",
            sa.Column("gpu_duty_cycle", sa.Float(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("composite_resource_samples")
    }
    if "gpu_duty_cycle" in columns:
        op.drop_column("composite_resource_samples", "gpu_duty_cycle")
