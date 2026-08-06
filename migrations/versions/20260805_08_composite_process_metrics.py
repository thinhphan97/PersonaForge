"""Persist reservation and observed process metrics for every Composite snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "20260805_08"
down_revision = "20260805_07"
branch_labels = None
depends_on = None


_COLUMNS = (
    sa.Column("process_identity_matches", sa.Boolean(), nullable=True),
    sa.Column("process_status", sa.String(length=32), nullable=True),
    sa.Column("process_cpu_utilization", sa.Float(), nullable=True),
    sa.Column("process_host_rss_bytes", sa.BigInteger(), nullable=True),
    sa.Column("process_host_memory_percent", sa.Float(), nullable=True),
    sa.Column("process_gpu_memory_percent", sa.Float(), nullable=True),
    sa.Column("process_gpu_sm_utilization", sa.Float(), nullable=True),
    sa.Column("process_gpu_memory_utilization", sa.Float(), nullable=True),
    sa.Column("process_gpu_encoder_utilization", sa.Float(), nullable=True),
    sa.Column("process_gpu_decoder_utilization", sa.Float(), nullable=True),
    sa.Column("reserved_vram_mb", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("reserved_gpu_duty_cycle", sa.Float(), nullable=False, server_default="0"),
)


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("composite_resource_samples")
    }
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("composite_resource_samples", column)
    with op.batch_alter_table("composite_resource_samples") as batch_op:
        batch_op.alter_column(
            "gpu_duty_cycle",
            existing_type=sa.Float(),
            nullable=True,
            server_default=None,
        )
    # Older rows stored the reservation in this observed-usage column. Keep
    # them unknown instead of training profiles from a configured estimate.
    op.execute("UPDATE composite_resource_samples SET gpu_duty_cycle = NULL")


def downgrade() -> None:
    op.execute(
        "UPDATE composite_resource_samples SET gpu_duty_cycle = 0 WHERE gpu_duty_cycle IS NULL"
    )
    with op.batch_alter_table("composite_resource_samples") as batch_op:
        batch_op.alter_column(
            "gpu_duty_cycle",
            existing_type=sa.Float(),
            nullable=False,
            server_default="0",
        )
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("composite_resource_samples")
    }
    for column in reversed(_COLUMNS):
        if column.name in existing:
            op.drop_column("composite_resource_samples", column.name)
