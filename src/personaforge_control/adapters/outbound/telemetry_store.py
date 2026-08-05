import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from personaforge_control.domain.models import (
    CompositeRequirement,
    ResourceProfileState,
    ResourceResolution,
)


class PostgresTelemetryStore:
    """Durable telemetry sink and resource-profile resolver.

    Worker processes never receive this database credential. The Control ingestor owns writes,
    while Scheduler reads compact profiles/configs through this adapter.
    """

    def __init__(
        self,
        database_url: str,
        bootstrap_peak_vram_mb: int = 4096,
        bootstrap_gpu_duty_cycle: float = 0.25,
        learning_vram_margin_mb: int = 512,
        trusted_vram_margin_mb: int = 256,
        compatible_vram_margin_mb: int = 1024,
    ) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._bootstrap_peak_vram_mb = bootstrap_peak_vram_mb
        self._bootstrap_gpu_duty_cycle = bootstrap_gpu_duty_cycle
        self._learning_vram_margin_mb = learning_vram_margin_mb
        self._trusted_vram_margin_mb = trusted_vram_margin_mb
        self._compatible_vram_margin_mb = compatible_vram_margin_mb

    async def startup(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def shutdown(self) -> None:
        await self._engine.dispose()

    async def ingest(self, payload: dict[str, Any], received_at: datetime) -> None:
        sampled_at = datetime.fromtimestamp(
            float(payload.get("hardware_sampled_at", received_at.timestamp())),
            timezone.utc,
        )
        worker_id = str(payload["worker_id"])
        boot_id = str(payload["boot_id"])
        generation = int(payload.get("identity_generation", 0))
        children = [item for item in payload.get("child_resources", ()) if isinstance(item, dict)]
        child_count_by_gpu: dict[str, int] = {}
        reserved_vram_by_gpu: dict[str, int] = {}
        reserved_compute_by_gpu: dict[str, float] = {}
        for child in children:
            gpu_uuid = str(child.get("assigned_gpu_uuid") or child.get("gpu_uuid") or "")
            if not gpu_uuid:
                continue
            child_count_by_gpu[gpu_uuid] = child_count_by_gpu.get(gpu_uuid, 0) + 1
            reserved_vram_by_gpu[gpu_uuid] = reserved_vram_by_gpu.get(gpu_uuid, 0) + int(
                child.get("reserved_vram_mb", 0)
            )
            reserved_compute_by_gpu[gpu_uuid] = reserved_compute_by_gpu.get(gpu_uuid, 0.0) + float(
                child.get("reserved_gpu_duty_cycle", 0)
            )

        gpu_by_uuid = {
            str(item["gpu_uuid"]): item
            for item in payload.get("gpu_resources", ())
            if isinstance(item, dict) and item.get("gpu_uuid")
        }
        async with self._engine.begin() as connection:
            for gpu_uuid, gpu in gpu_by_uuid.items():
                await connection.execute(
                    text(_INSERT_GPU_SAMPLE),
                    {
                        "sample_id": _sample_id(worker_id, boot_id, gpu_uuid, sampled_at),
                        "worker_id": worker_id,
                        "boot_id": boot_id,
                        "identity_generation": generation,
                        "gpu_uuid": gpu_uuid,
                        "gpu_name": str(gpu.get("gpu_name", "unknown")),
                        "compute_capability": str(gpu.get("compute_capability", "0.0")),
                        "sampled_at": sampled_at,
                        "received_at": received_at,
                        "total_vram_mb": int(gpu.get("total_vram_mb", 0)),
                        "used_vram_mb": int(gpu.get("used_vram_mb", 0)),
                        "free_vram_mb": int(gpu.get("free_vram_mb", 0)),
                        "gpu_utilization": float(gpu.get("gpu_utilization", 0)),
                        "memory_utilization": float(gpu.get("memory_utilization", 0)),
                        "encoder_utilization": _float_or_none(gpu.get("encoder_utilization")),
                        "decoder_utilization": _float_or_none(gpu.get("decoder_utilization")),
                        "temperature_c": _float_or_none(gpu.get("temperature_c")),
                        "power_watts": _float_or_none(gpu.get("power_watts")),
                        "active_children": child_count_by_gpu.get(gpu_uuid, 0),
                        "reserved_vram_mb": reserved_vram_by_gpu.get(gpu_uuid, 0),
                        "reserved_compute": reserved_compute_by_gpu.get(gpu_uuid, 0),
                    },
                )
            for child in children:
                gpu_uuid = str(child.get("assigned_gpu_uuid") or child.get("gpu_uuid") or "")
                child_gpu = gpu_by_uuid.get(gpu_uuid)
                if not gpu_uuid or child_gpu is None or not child.get("child_id"):
                    continue
                gpu_memory = _float_or_none(child.get("gpu_memory_bytes"))
                process_vram_mb = None if gpu_memory is None else gpu_memory / 1024 / 1024
                baseline_vram_mb = _float_or_none(child.get("composite_baseline_vram_mb"))
                attributed_vram_mb = _float_or_none(child.get("attributed_vram_mb"))
                if attributed_vram_mb is None and process_vram_mb is not None:
                    attributed_vram_mb = max(
                        0.0,
                        process_vram_mb - (baseline_vram_mb or 0.0),
                    )
                gpu_sm_utilization = _float_or_none(child.get("gpu_sm_utilization"))
                observed_gpu_duty_cycle = _float_or_none(child.get("gpu_duty_cycle"))
                if observed_gpu_duty_cycle is None and gpu_sm_utilization is not None:
                    observed_gpu_duty_cycle = max(0.0, min(1.0, gpu_sm_utilization / 100.0))
                await connection.execute(
                    text(_INSERT_COMPOSITE_SAMPLE),
                    {
                        "sample_id": _sample_id(
                            worker_id, boot_id, str(child["child_id"]), sampled_at
                        ),
                        "session_id": str(child.get("session_id", "unknown")),
                        "generation": int(child.get("generation", 0)),
                        "child_id": str(child["child_id"]),
                        "worker_id": worker_id,
                        "boot_id": boot_id,
                        "gpu_uuid": gpu_uuid,
                        "gpu_name": str(child_gpu.get("gpu_name", "unknown")),
                        "compute_capability": str(child_gpu.get("compute_capability", "0.0")),
                        "process_id": int(child.get("pid", 0)),
                        "sampled_at": sampled_at,
                        "received_at": received_at,
                        "phase": str(child.get("phase", "unknown")),
                        "character_id": str(child.get("character_id", "default")),
                        "avatar_model_id": str(child.get("avatar_model_id", "recorded-avatar")),
                        "avatar_revision": str(child.get("avatar_revision", "default")),
                        "voice_id": str(child.get("voice_id", "default")),
                        "tts_model_id": str(child.get("tts_model_id", "scripted-tone")),
                        "tts_revision": str(child.get("tts_revision", "default")),
                        "runtime_version": str(child.get("runtime_version", "default")),
                        "precision": str(child.get("precision", "fp32")),
                        "resolution": str(child.get("resolution", "640x360")),
                        "fps": float(child.get("fps", 25)),
                        "isolation_mode": str(child.get("isolation_mode", "subprocess")),
                        "process_identity_matches": _bool_or_none(
                            child.get("process_identity_matches")
                        ),
                        "process_status": _string_or_none(child.get("status")),
                        "process_cpu_utilization": _float_or_none(child.get("cpu_utilization")),
                        "process_host_rss_bytes": _int_or_none(child.get("host_rss_bytes")),
                        "process_host_memory_percent": _float_or_none(
                            child.get("host_memory_percent")
                        ),
                        "process_vram_mb": process_vram_mb,
                        "process_gpu_memory_percent": _float_or_none(
                            child.get("gpu_memory_percent")
                        ),
                        "process_gpu_sm_utilization": gpu_sm_utilization,
                        "process_gpu_memory_utilization": _float_or_none(
                            child.get("gpu_memory_utilization")
                        ),
                        "process_gpu_encoder_utilization": _float_or_none(
                            child.get("gpu_encoder_utilization")
                        ),
                        "process_gpu_decoder_utilization": _float_or_none(
                            child.get("gpu_decoder_utilization")
                        ),
                        "reserved_vram_mb": int(child.get("reserved_vram_mb", 0)),
                        "reserved_gpu_duty_cycle": float(child.get("reserved_gpu_duty_cycle", 0)),
                        "composite_baseline_vram_mb": baseline_vram_mb,
                        "attributed_vram_mb": attributed_vram_mb,
                        "gpu_execution_ms": _float_or_none(child.get("gpu_execution_ms")),
                        "gpu_duty_cycle": observed_gpu_duty_cycle,
                        "tts_realtime_factor": _float_or_none(child.get("tts_realtime_factor")),
                        "render_fps": _float_or_none(child.get("render_fps")),
                        "av_drift_ms": _float_or_none(child.get("av_drift_ms")),
                        "oom": bool(child.get("oom", False)),
                    },
                )

    async def aggregate_profiles(self) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(text(_AGGREGATE_PROFILES), {"now": _utc_now()})

    async def delete_expired_samples(self, retention_days: int) -> tuple[int, int]:
        cutoff = _utc_now() - timedelta(days=retention_days)
        async with self._engine.begin() as connection:
            composite = await connection.execute(
                text("DELETE FROM composite_resource_samples WHERE sampled_at < :cutoff"),
                {"cutoff": cutoff},
            )
            gpu = await connection.execute(
                text("DELETE FROM gpu_heartbeat_samples WHERE sampled_at < :cutoff"),
                {"cutoff": cutoff},
            )
        return composite.rowcount or 0, gpu.rowcount or 0

    async def resolve(
        self,
        requirement: CompositeRequirement,
        gpu_name: str,
        compute_capability: str,
    ) -> ResourceResolution:
        async with self._engine.connect() as connection:
            config = (
                (
                    await connection.execute(
                        text(_SELECT_CONFIG),
                        _requirement_params(requirement, gpu_name, compute_capability),
                    )
                )
                .mappings()
                .first()
            )
            profile = (
                (
                    await connection.execute(
                        text(_SELECT_PROFILE),
                        _requirement_params(requirement, gpu_name, compute_capability),
                    )
                )
                .mappings()
                .first()
            )
            compatible_profiles = []
            if profile is None:
                compatible_profiles = list(
                    (
                        await connection.execute(
                            text(_SELECT_COMPATIBLE_PROFILES),
                            _requirement_params(requirement, gpu_name, compute_capability),
                        )
                    ).mappings()
                )

        config_peak = int(config["manifest_peak_vram_mb"]) if config else 0
        config_compute = float(config["manifest_gpu_duty_cycle"]) if config else 0
        bootstrap_margin = int(config["bootstrap_vram_margin_mb"]) if config else 0
        compute_margin = float(config["bootstrap_compute_margin"]) if config else 0
        minimum_compute = (
            str(config["minimum_compute_capability"])
            if config
            else requirement.minimum_compute_capability
        )
        floor_peak = max(requirement.peak_vram_mb, config_peak)
        floor_compute = max(requirement.gpu_duty_cycle, config_compute)

        if profile is not None:
            state = ResourceProfileState(str(profile["state"]))
            observed_peak = max(
                float(profile["vram_p99_mb"]),
                float(profile["vram_high_watermark_mb"]),
            )
            margin = (
                self._trusted_vram_margin_mb
                if state is ResourceProfileState.TRUSTED
                else self._learning_vram_margin_mb
            )
            if state is ResourceProfileState.DEGRADED:
                margin = max(margin, bootstrap_margin, self._learning_vram_margin_mb * 2)
            return ResourceResolution(
                peak_vram_mb=max(floor_peak, int(observed_peak)) + margin,
                gpu_duty_cycle=min(
                    1.0,
                    max(floor_compute, float(profile["speaking_compute_p95"])),
                ),
                source=f"profile:{state.value}",
                profile_state=state,
                profile_id=str(profile["profile_id"]),
                minimum_compute_capability=minimum_compute,
            )
        if compatible_profiles:
            # A compatible profile shares either the avatar model or the voice/TTS
            # model on the same GPU/runtime/output shape. It is useful cold-start
            # evidence, but never as trustworthy as the exact composite pair.
            peak_profile = max(
                compatible_profiles,
                key=lambda item: max(
                    float(item["vram_p99_mb"]),
                    float(item["vram_high_watermark_mb"]),
                ),
            )
            observed_peak = max(
                max(
                    float(item["vram_p99_mb"]),
                    float(item["vram_high_watermark_mb"]),
                )
                for item in compatible_profiles
            )
            observed_compute = max(
                float(item["speaking_compute_p95"]) for item in compatible_profiles
            )
            has_degraded_profile = any(
                str(item["state"]) == ResourceProfileState.DEGRADED.value
                for item in compatible_profiles
            )
            compatibility_margin = max(
                self._compatible_vram_margin_mb,
                bootstrap_margin,
            )
            if has_degraded_profile:
                compatibility_margin = max(
                    compatibility_margin,
                    self._learning_vram_margin_mb * 2,
                )
            return ResourceResolution(
                peak_vram_mb=max(floor_peak, int(observed_peak)) + compatibility_margin,
                gpu_duty_cycle=min(1.0, max(floor_compute, observed_compute)),
                source="profile:compatible",
                profile_state=(
                    ResourceProfileState.DEGRADED
                    if has_degraded_profile
                    else ResourceProfileState.LEARNING
                ),
                profile_id=str(peak_profile["profile_id"]),
                minimum_compute_capability=minimum_compute,
            )
        if config is not None:
            return ResourceResolution(
                peak_vram_mb=floor_peak + bootstrap_margin,
                gpu_duty_cycle=min(1.0, floor_compute + compute_margin),
                source="database-config",
                profile_state=ResourceProfileState.BOOTSTRAP,
                profile_id=str(config["config_id"]),
                minimum_compute_capability=minimum_compute,
            )
        return ResourceResolution(
            peak_vram_mb=max(floor_peak, self._bootstrap_peak_vram_mb),
            gpu_duty_cycle=max(floor_compute, self._bootstrap_gpu_duty_cycle),
            source="platform-default",
            profile_state=ResourceProfileState.BOOTSTRAP,
            minimum_compute_capability=requirement.minimum_compute_capability,
        )

    async def upsert_config(self, values: dict[str, Any]) -> str:
        now = _utc_now()
        config_id = str(values.get("config_id") or uuid4().hex)
        parameters = {
            **values,
            "gpu_name": values.get("gpu_name") or "*",
            "config_id": config_id,
            "created_at": now,
            "updated_at": now,
        }
        async with self._engine.begin() as connection:
            result = await connection.execute(text(_UPSERT_CONFIG), parameters)
            stored_id = result.scalar_one()
        return str(stored_id)

    async def list_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT * FROM resource_profiles ORDER BY updated_at DESC LIMIT :limit"),
                {"limit": limit},
            )
            return [dict(row) for row in rows.mappings()]

    async def list_configs(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT * FROM persona_resource_configs ORDER BY updated_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [dict(row) for row in rows.mappings()]

    async def statistics(self) -> dict[str, int | str | None]:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM gpu_heartbeat_samples) AS gpu_samples, "
                            "(SELECT count(*) FROM composite_resource_samples) AS composite_samples, "
                            "(SELECT count(*) FROM resource_profiles) AS profiles, "
                            "(SELECT max(received_at) FROM gpu_heartbeat_samples) AS last_received_at"
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {
            "gpu_samples": int(row["gpu_samples"]),
            "composite_samples": int(row["composite_samples"]),
            "profiles": int(row["profiles"]),
            "last_received_at": (
                row["last_received_at"].isoformat() if row["last_received_at"] is not None else None
            ),
        }


def _sample_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float | str):
        return float(value)
    raise TypeError("Expected an optional numeric telemetry value")


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float | str):
        return int(value)
    raise TypeError("Expected an optional integer telemetry value")


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise TypeError("Expected an optional boolean telemetry value")


def _string_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _requirement_params(
    requirement: CompositeRequirement,
    gpu_name: str,
    compute_capability: str,
) -> dict[str, object]:
    return {
        "character_id": requirement.character_id,
        "avatar_model_id": requirement.avatar_model_id,
        "avatar_revision": requirement.avatar_revision,
        "voice_id": requirement.voice_id,
        "tts_model_id": requirement.tts_model_id,
        "tts_revision": requirement.tts_revision,
        "runtime": requirement.runtime,
        "runtime_version": requirement.runtime_version,
        "precision": requirement.precision,
        "resolution": requirement.resolution,
        "fps": requirement.fps,
        "isolation_mode": requirement.isolation_mode,
        "gpu_name": gpu_name,
        "compute_capability": compute_capability,
    }


_INSERT_GPU_SAMPLE = """
INSERT INTO gpu_heartbeat_samples (
 sample_id, worker_id, boot_id, identity_generation, gpu_uuid, gpu_name,
 compute_capability, sampled_at, received_at, total_vram_mb, used_vram_mb,
 free_vram_mb, gpu_utilization, memory_utilization, encoder_utilization,
 decoder_utilization, temperature_c, power_watts, active_children,
 reserved_vram_mb, reserved_compute
) VALUES (
 :sample_id, :worker_id, :boot_id, :identity_generation, :gpu_uuid, :gpu_name,
 :compute_capability, :sampled_at, :received_at, :total_vram_mb, :used_vram_mb,
 :free_vram_mb, :gpu_utilization, :memory_utilization, :encoder_utilization,
 :decoder_utilization, :temperature_c, :power_watts, :active_children,
 :reserved_vram_mb, :reserved_compute
) ON CONFLICT (worker_id, boot_id, gpu_uuid, sampled_at) DO NOTHING
"""

_INSERT_COMPOSITE_SAMPLE = """
INSERT INTO composite_resource_samples (
 sample_id, session_id, generation, child_id, worker_id, boot_id, gpu_uuid,
 gpu_name, compute_capability, process_id, sampled_at, received_at, phase,
 character_id, avatar_model_id, avatar_revision, voice_id, tts_model_id,
 tts_revision, runtime_version, precision, resolution, fps, isolation_mode,
 process_identity_matches, process_status, process_cpu_utilization,
 process_host_rss_bytes, process_host_memory_percent, process_vram_mb,
 process_gpu_memory_percent, process_gpu_sm_utilization,
 process_gpu_memory_utilization, process_gpu_encoder_utilization,
 process_gpu_decoder_utilization, reserved_vram_mb, reserved_gpu_duty_cycle,
 composite_baseline_vram_mb, attributed_vram_mb, gpu_execution_ms,
 gpu_duty_cycle, tts_realtime_factor, render_fps, av_drift_ms, oom
) VALUES (
 :sample_id, :session_id, :generation, :child_id, :worker_id, :boot_id, :gpu_uuid,
 :gpu_name, :compute_capability, :process_id, :sampled_at, :received_at, :phase,
 :character_id, :avatar_model_id, :avatar_revision, :voice_id, :tts_model_id,
 :tts_revision, :runtime_version, :precision, :resolution, :fps, :isolation_mode,
 :process_identity_matches, :process_status, :process_cpu_utilization,
 :process_host_rss_bytes, :process_host_memory_percent, :process_vram_mb,
 :process_gpu_memory_percent, :process_gpu_sm_utilization,
 :process_gpu_memory_utilization, :process_gpu_encoder_utilization,
 :process_gpu_decoder_utilization, :reserved_vram_mb, :reserved_gpu_duty_cycle,
 :composite_baseline_vram_mb, :attributed_vram_mb, :gpu_execution_ms,
 :gpu_duty_cycle, :tts_realtime_factor, :render_fps, :av_drift_ms, :oom
) ON CONFLICT (worker_id, boot_id, child_id, sampled_at) DO NOTHING
"""

_SELECT_CONFIG = """
SELECT * FROM persona_resource_configs
WHERE active = true AND character_id = :character_id
  AND avatar_model_id = :avatar_model_id AND avatar_revision = :avatar_revision
  AND voice_id = :voice_id AND tts_model_id = :tts_model_id
  AND tts_revision = :tts_revision AND runtime = :runtime
  AND runtime_version = :runtime_version AND precision = :precision
  AND resolution = :resolution AND fps = :fps AND isolation_mode = :isolation_mode
  AND (gpu_name = :gpu_name OR gpu_name = '*')
ORDER BY (gpu_name = :gpu_name) DESC, updated_at DESC LIMIT 1
"""

_SELECT_PROFILE = """
SELECT * FROM resource_profiles
WHERE character_id = :character_id AND avatar_model_id = :avatar_model_id
  AND avatar_revision = :avatar_revision AND voice_id = :voice_id
  AND tts_model_id = :tts_model_id AND tts_revision = :tts_revision
  AND gpu_name = :gpu_name AND compute_capability = :compute_capability
  AND runtime_version = :runtime_version AND precision = :precision
  AND resolution = :resolution AND fps = :fps AND isolation_mode = :isolation_mode
ORDER BY CASE state WHEN 'trusted' THEN 0 WHEN 'learning' THEN 1 ELSE 2 END,
 updated_at DESC LIMIT 1
"""

_SELECT_COMPATIBLE_PROFILES = """
SELECT * FROM resource_profiles
WHERE gpu_name = :gpu_name AND compute_capability = :compute_capability
  AND runtime_version = :runtime_version AND precision = :precision
  AND resolution = :resolution AND fps = :fps AND isolation_mode = :isolation_mode
  AND (
    (avatar_model_id = :avatar_model_id AND avatar_revision = :avatar_revision)
    OR
    (voice_id = :voice_id AND tts_model_id = :tts_model_id
      AND tts_revision = :tts_revision)
  )
  AND NOT (
    character_id = :character_id
    AND avatar_model_id = :avatar_model_id AND avatar_revision = :avatar_revision
    AND voice_id = :voice_id AND tts_model_id = :tts_model_id
    AND tts_revision = :tts_revision
  )
ORDER BY CASE state WHEN 'trusted' THEN 0 WHEN 'learning' THEN 1 ELSE 2 END,
 updated_at DESC LIMIT 20
"""

_UPSERT_CONFIG = """
INSERT INTO persona_resource_configs (
 config_id, character_id, avatar_model_id, avatar_revision, voice_id, tts_model_id,
 tts_revision, runtime, runtime_version, precision, resolution, fps, isolation_mode,
 gpu_name, minimum_compute_capability, manifest_peak_vram_mb,
 manifest_gpu_duty_cycle, bootstrap_vram_margin_mb, bootstrap_compute_margin,
 active, created_at, updated_at
) VALUES (
 :config_id, :character_id, :avatar_model_id, :avatar_revision, :voice_id, :tts_model_id,
 :tts_revision, :runtime, :runtime_version, :precision, :resolution, :fps, :isolation_mode,
 :gpu_name, :minimum_compute_capability, :manifest_peak_vram_mb,
 :manifest_gpu_duty_cycle, :bootstrap_vram_margin_mb, :bootstrap_compute_margin,
 :active, :created_at, :updated_at
) ON CONFLICT ON CONSTRAINT uq_persona_resource_config_key DO UPDATE SET
 minimum_compute_capability = EXCLUDED.minimum_compute_capability,
 manifest_peak_vram_mb = EXCLUDED.manifest_peak_vram_mb,
 manifest_gpu_duty_cycle = EXCLUDED.manifest_gpu_duty_cycle,
 bootstrap_vram_margin_mb = EXCLUDED.bootstrap_vram_margin_mb,
 bootstrap_compute_margin = EXCLUDED.bootstrap_compute_margin,
 active = EXCLUDED.active, updated_at = EXCLUDED.updated_at
RETURNING config_id
"""

_AGGREGATE_PROFILES = """
INSERT INTO resource_profiles (
 profile_id, character_id, avatar_model_id, avatar_revision, voice_id, tts_model_id,
 tts_revision, gpu_name, compute_capability, runtime_version, precision, resolution,
 fps, isolation_mode, state, sample_count, clean_session_count, vram_p50_mb,
 vram_p95_mb, vram_p99_mb, vram_high_watermark_mb, speaking_compute_p95,
 render_fps_p95, tts_realtime_factor_p95, oom_count, first_seen_at, last_seen_at,
 last_oom_at, safety_margin_version, policy_version, updated_at
)
SELECT
 md5(concat_ws('|', character_id, avatar_model_id, avatar_revision, voice_id,
   tts_model_id, tts_revision, gpu_name, compute_capability, runtime_version,
   precision, resolution, fps::text, isolation_mode)),
 character_id, avatar_model_id, avatar_revision, voice_id, tts_model_id, tts_revision,
 gpu_name, compute_capability, runtime_version, precision, resolution, fps, isolation_mode,
 CASE WHEN bool_or(oom) THEN 'degraded'
      WHEN count(DISTINCT session_id) >= 20 THEN 'trusted'
      WHEN count(*) >= 10 THEN 'learning' ELSE 'bootstrap' END,
 count(*), count(DISTINCT session_id) FILTER (WHERE NOT oom),
 percentile_cont(0.50) WITHIN GROUP (ORDER BY attributed_vram_mb)
   FILTER (WHERE attributed_vram_mb IS NOT NULL),
 percentile_cont(0.95) WITHIN GROUP (ORDER BY attributed_vram_mb)
   FILTER (WHERE attributed_vram_mb IS NOT NULL),
 percentile_cont(0.99) WITHIN GROUP (ORDER BY attributed_vram_mb)
   FILTER (WHERE attributed_vram_mb IS NOT NULL),
 max(COALESCE(attributed_vram_mb, 0)),
 COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY gpu_duty_cycle)
   FILTER (WHERE phase = 'speaking'), max(gpu_duty_cycle)),
 percentile_cont(0.95) WITHIN GROUP (ORDER BY render_fps)
   FILTER (WHERE render_fps IS NOT NULL),
 percentile_cont(0.95) WITHIN GROUP (ORDER BY tts_realtime_factor)
   FILTER (WHERE tts_realtime_factor IS NOT NULL),
 count(*) FILTER (WHERE oom), min(sampled_at), max(sampled_at),
 max(sampled_at) FILTER (WHERE oom), 'v1', 'v1', :now
FROM composite_resource_samples
GROUP BY character_id, avatar_model_id, avatar_revision, voice_id, tts_model_id,
 tts_revision, gpu_name, compute_capability, runtime_version, precision, resolution,
 fps, isolation_mode
HAVING count(*) FILTER (WHERE attributed_vram_mb IS NOT NULL) > 0
ON CONFLICT ON CONSTRAINT uq_resource_profile_key DO UPDATE SET
 state = EXCLUDED.state, sample_count = EXCLUDED.sample_count,
 clean_session_count = EXCLUDED.clean_session_count, vram_p50_mb = EXCLUDED.vram_p50_mb,
 vram_p95_mb = EXCLUDED.vram_p95_mb, vram_p99_mb = EXCLUDED.vram_p99_mb,
 vram_high_watermark_mb = GREATEST(resource_profiles.vram_high_watermark_mb,
   EXCLUDED.vram_high_watermark_mb),
 speaking_compute_p95 = EXCLUDED.speaking_compute_p95,
 render_fps_p95 = EXCLUDED.render_fps_p95,
 tts_realtime_factor_p95 = EXCLUDED.tts_realtime_factor_p95,
 oom_count = EXCLUDED.oom_count, first_seen_at = EXCLUDED.first_seen_at,
 last_seen_at = EXCLUDED.last_seen_at, last_oom_at = EXCLUDED.last_oom_at,
 updated_at = EXCLUDED.updated_at
"""
