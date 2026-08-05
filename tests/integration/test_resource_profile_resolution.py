import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from personaforge_control.adapters.outbound.telemetry_store import PostgresTelemetryStore
from personaforge_control.domain.models import CompositeRequirement, ResourceProfileState


@pytest.mark.asyncio
async def test_db_manifest_is_floor_for_compatible_observed_profile() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL telemetry tests")

    suffix = uuid4().hex
    character_id = f"character-{suffix}"
    avatar_model_id = f"avatar-{suffix}"
    config_id = f"config-{suffix}"
    profile_id = f"profile-{suffix}"
    now = datetime.now(timezone.utc)
    store = PostgresTelemetryStore(database_url, compatible_vram_margin_mb=1_024)
    engine = create_async_engine(database_url)
    try:
        await store.upsert_config(
            {
                "config_id": config_id,
                "character_id": character_id,
                "avatar_model_id": avatar_model_id,
                "avatar_revision": "v1",
                "voice_id": "requested-voice",
                "tts_model_id": "tts",
                "tts_revision": "v1",
                "runtime": "pytorch",
                "runtime_version": "2.8",
                "precision": "fp16",
                "resolution": "1280x720",
                "fps": 25.0,
                "isolation_mode": "subprocess",
                "gpu_name": None,
                "minimum_compute_capability": "8.0",
                "manifest_peak_vram_mb": 7_000,
                "manifest_gpu_duty_cycle": 0.35,
                "bootstrap_vram_margin_mb": 500,
                "bootstrap_compute_margin": 0.05,
                "active": True,
            }
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO resource_profiles (
                      profile_id, character_id, avatar_model_id, avatar_revision,
                      voice_id, tts_model_id, tts_revision, gpu_name,
                      compute_capability, runtime_version, precision, resolution,
                      fps, isolation_mode, state, sample_count, clean_session_count,
                      vram_p50_mb, vram_p95_mb, vram_p99_mb,
                      vram_high_watermark_mb, speaking_compute_p95, render_fps_p95,
                      tts_realtime_factor_p95, oom_count, first_seen_at,
                      last_seen_at, last_oom_at, safety_margin_version,
                      policy_version, updated_at
                    ) VALUES (
                      :profile_id, :character_id, :avatar_model_id, 'v1',
                      'another-voice', 'tts', 'v1', 'Test GPU', '8.6', '2.8',
                      'fp16', '1280x720', 25, 'subprocess', 'trusted', 30, 20,
                      6500, 7600, 8000, 8100, 0.4, 25, 0.8, 0, :now, :now,
                      NULL, 'v1', 'v1', :now
                    )
                    """
                ),
                {
                    "profile_id": profile_id,
                    "character_id": character_id,
                    "avatar_model_id": avatar_model_id,
                    "now": now,
                },
            )

        resolution = await store.resolve(
            CompositeRequirement(
                session_id=f"session-{suffix}",
                character_id=character_id,
                avatar_model_id=avatar_model_id,
                avatar_revision="v1",
                voice_id="requested-voice",
                tts_model_id="tts",
                tts_revision="v1",
                runtime="pytorch",
                runtime_version="2.8",
                precision="fp16",
                minimum_compute_capability="7.0",
                resolution="1280x720",
                fps=25,
                peak_vram_mb=1,
                gpu_duty_cycle=0.01,
            ),
            "Test GPU",
            "8.6",
        )

        assert resolution.source == "profile:compatible"
        assert resolution.profile_state is ResourceProfileState.LEARNING
        assert resolution.profile_id == profile_id
        assert resolution.peak_vram_mb == 9_124
        assert resolution.gpu_duty_cycle == 0.4
        assert resolution.minimum_compute_capability == "8.0"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM resource_profiles WHERE profile_id = :profile_id"),
                {"profile_id": profile_id},
            )
            await connection.execute(
                text("DELETE FROM persona_resource_configs WHERE config_id = :config_id"),
                {"config_id": config_id},
            )
        await engine.dispose()
        await store.shutdown()
