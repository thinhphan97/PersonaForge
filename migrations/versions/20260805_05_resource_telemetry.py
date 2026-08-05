"""Add GPU telemetry, composite samples and versioned resource profiles."""

import sqlalchemy as sa
from alembic import op

revision = "20260805_05"
down_revision = "20260804_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persona_resource_configs",
        sa.Column("config_id", sa.String(64), primary_key=True),
        sa.Column("character_id", sa.String(100), nullable=False),
        sa.Column("avatar_model_id", sa.String(180), nullable=False),
        sa.Column("avatar_revision", sa.String(180), nullable=False),
        sa.Column("voice_id", sa.String(180), nullable=False),
        sa.Column("tts_model_id", sa.String(180), nullable=False),
        sa.Column("tts_revision", sa.String(180), nullable=False),
        sa.Column("runtime", sa.String(100), nullable=False),
        sa.Column("runtime_version", sa.String(100), nullable=False),
        sa.Column("precision", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("isolation_mode", sa.String(32), nullable=False),
        sa.Column("gpu_name", sa.String(180), nullable=False, server_default="*"),
        sa.Column("minimum_compute_capability", sa.String(16), nullable=False),
        sa.Column("manifest_peak_vram_mb", sa.Integer(), nullable=False),
        sa.Column("manifest_gpu_duty_cycle", sa.Float(), nullable=False),
        sa.Column("bootstrap_vram_margin_mb", sa.Integer(), nullable=False),
        sa.Column("bootstrap_compute_margin", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id", "avatar_model_id", "avatar_revision", "voice_id",
            "tts_model_id", "tts_revision", "runtime", "runtime_version", "precision",
            "resolution", "fps", "isolation_mode", "gpu_name",
            name="uq_persona_resource_config_key",
        ),
    )
    op.create_index(
        "ix_persona_resource_configs_lookup",
        "persona_resource_configs",
        ["avatar_model_id", "tts_model_id", "voice_id", "resolution", "fps"],
    )

    op.create_table(
        "gpu_heartbeat_samples",
        sa.Column("sample_id", sa.String(64), primary_key=True),
        sa.Column("worker_id", sa.String(180), nullable=False),
        sa.Column("boot_id", sa.String(64), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("gpu_uuid", sa.String(100), nullable=False),
        sa.Column("gpu_name", sa.String(180), nullable=False),
        sa.Column("compute_capability", sa.String(16), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_vram_mb", sa.Integer(), nullable=False),
        sa.Column("used_vram_mb", sa.Integer(), nullable=False),
        sa.Column("free_vram_mb", sa.Integer(), nullable=False),
        sa.Column("gpu_utilization", sa.Float(), nullable=False),
        sa.Column("memory_utilization", sa.Float(), nullable=False),
        sa.Column("encoder_utilization", sa.Float(), nullable=True),
        sa.Column("decoder_utilization", sa.Float(), nullable=True),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("power_watts", sa.Float(), nullable=True),
        sa.Column("active_children", sa.Integer(), nullable=False),
        sa.Column("reserved_vram_mb", sa.Integer(), nullable=False),
        sa.Column("reserved_compute", sa.Float(), nullable=False),
        sa.UniqueConstraint(
            "worker_id", "boot_id", "gpu_uuid", "sampled_at",
            name="uq_gpu_heartbeat_sample",
        ),
    )
    op.create_index("ix_gpu_samples_sampled_at", "gpu_heartbeat_samples", ["sampled_at"])
    op.create_index(
        "ix_gpu_samples_worker_gpu_time",
        "gpu_heartbeat_samples",
        ["worker_id", "gpu_uuid", "sampled_at"],
    )

    op.create_table(
        "composite_resource_samples",
        sa.Column("sample_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("child_id", sa.String(100), nullable=False),
        sa.Column("worker_id", sa.String(180), nullable=False),
        sa.Column("boot_id", sa.String(64), nullable=False),
        sa.Column("gpu_uuid", sa.String(100), nullable=False),
        sa.Column("gpu_name", sa.String(180), nullable=False),
        sa.Column("compute_capability", sa.String(16), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("character_id", sa.String(100), nullable=False),
        sa.Column("avatar_model_id", sa.String(180), nullable=False),
        sa.Column("avatar_revision", sa.String(180), nullable=False),
        sa.Column("voice_id", sa.String(180), nullable=False),
        sa.Column("tts_model_id", sa.String(180), nullable=False),
        sa.Column("tts_revision", sa.String(180), nullable=False),
        sa.Column("runtime_version", sa.String(100), nullable=False),
        sa.Column("precision", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("isolation_mode", sa.String(32), nullable=False),
        sa.Column("process_vram_mb", sa.Float(), nullable=True),
        sa.Column("composite_baseline_vram_mb", sa.Float(), nullable=True),
        sa.Column("attributed_vram_mb", sa.Float(), nullable=True),
        sa.Column("gpu_execution_ms", sa.Float(), nullable=True),
        sa.Column("tts_realtime_factor", sa.Float(), nullable=True),
        sa.Column("render_fps", sa.Float(), nullable=True),
        sa.Column("av_drift_ms", sa.Float(), nullable=True),
        sa.Column("oom", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint(
            "worker_id", "boot_id", "child_id", "sampled_at",
            name="uq_composite_resource_sample",
        ),
    )
    op.create_index(
        "ix_composite_samples_profile",
        "composite_resource_samples",
        ["avatar_model_id", "tts_model_id", "voice_id", "sampled_at"],
    )
    op.create_index(
        "ix_composite_samples_session_time",
        "composite_resource_samples",
        ["session_id", "sampled_at"],
    )

    op.create_table(
        "resource_profiles",
        sa.Column("profile_id", sa.String(64), primary_key=True),
        sa.Column("character_id", sa.String(100), nullable=False),
        sa.Column("avatar_model_id", sa.String(180), nullable=False),
        sa.Column("avatar_revision", sa.String(180), nullable=False),
        sa.Column("voice_id", sa.String(180), nullable=False),
        sa.Column("tts_model_id", sa.String(180), nullable=False),
        sa.Column("tts_revision", sa.String(180), nullable=False),
        sa.Column("gpu_name", sa.String(180), nullable=False),
        sa.Column("compute_capability", sa.String(16), nullable=False),
        sa.Column("runtime_version", sa.String(100), nullable=False),
        sa.Column("precision", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("isolation_mode", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("clean_session_count", sa.Integer(), nullable=False),
        sa.Column("vram_p50_mb", sa.Float(), nullable=False),
        sa.Column("vram_p95_mb", sa.Float(), nullable=False),
        sa.Column("vram_p99_mb", sa.Float(), nullable=False),
        sa.Column("vram_high_watermark_mb", sa.Float(), nullable=False),
        sa.Column("speaking_compute_p95", sa.Float(), nullable=False),
        sa.Column("render_fps_p95", sa.Float(), nullable=True),
        sa.Column("tts_realtime_factor_p95", sa.Float(), nullable=True),
        sa.Column("oom_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_oom_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safety_margin_version", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id", "avatar_model_id", "avatar_revision", "voice_id",
            "tts_model_id", "tts_revision", "gpu_name", "compute_capability",
            "runtime_version", "precision", "resolution", "fps", "isolation_mode",
            name="uq_resource_profile_key",
        ),
    )
    op.create_index(
        "ix_resource_profiles_lookup",
        "resource_profiles",
        ["avatar_model_id", "tts_model_id", "voice_id", "resolution", "fps", "state"],
    )


def downgrade() -> None:
    op.drop_table("resource_profiles")
    op.drop_table("composite_resource_samples")
    op.drop_table("gpu_heartbeat_samples")
    op.drop_table("persona_resource_configs")
