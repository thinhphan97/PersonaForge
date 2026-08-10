import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from personaforge_observability import bind_context
from personaforge_observability.context import context_from_mapping
from personaforge_worker.adapters.outbound.gpu_probe import build_gpu_probe
from personaforge_worker.domain.hardware import GpuHardwareSnapshot, WorkerResourceSnapshot
from personaforge_worker.identity import WorkerIdentityClient
from personaforge_worker.master.child_server import (
    ChildConnection,
    ChildControlServer,
    ChildControlService,
)
from personaforge_worker.master.supervisor import ChildSupervisor
from personaforge_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


class RedisRenderWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._redis: Redis | None = None
        self._worker_id = settings.worker_id or "avatar-worker-1"
        self._boot_id = "legacy"
        self._identity_generation = 0
        self._stream = f"avatar:commands:{self._worker_id}"
        self._group = "avatar-render-workers"
        self._supervisor: ChildSupervisor | None = None
        self._child_service: ChildControlService | None = None
        self._child_server: ChildControlServer | None = None
        self._identity_client: WorkerIdentityClient | None = None
        self._hardware_probe = build_gpu_probe(
            settings.gpu_probe_provider,
            settings.gpu_uuid,
            settings.gpu_name,
            settings.gpu_compute_capability,
            settings.total_vram_mb,
            settings.gpu_probe_timeout_seconds,
        )
        self._hardware: GpuHardwareSnapshot | None = None
        self._resources: WorkerResourceSnapshot | None = None

    async def run(self) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self._connect()
            await self._start_child_runtime()
            await self._ensure_group()
            heartbeat = asyncio.create_task(self._heartbeat(), name="worker-heartbeat")
            while True:
                if heartbeat.done():
                    await heartbeat
                batches = await self._require_redis().xreadgroup(
                    groupname=self._group,
                    consumername=self._worker_id,
                    streams={self._stream: ">"},
                    count=10,
                    block=1_000,
                )
                for _stream_name, messages in batches:
                    for message_id, fields in messages:
                        await self._handle_with_retry(message_id, fields)
                if heartbeat.done():
                    await heartbeat
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if self._supervisor is not None:
                await self._supervisor.shutdown()
            if self._child_server is not None:
                await self._child_server.stop()
            if self._redis is not None:
                await self._redis.aclose()
            if self._identity_client is not None:
                try:
                    await self._identity_client.deregister()
                finally:
                    await self._identity_client.close()

    async def _handle_with_retry(
        self,
        message_id: str,
        fields: dict[str, Any],
    ) -> None:
        command: dict[str, Any] | None = None
        for attempt in range(1, 4):
            try:
                raw = fields.get("data")
                if not isinstance(raw, str):
                    raise ValueError("Redis command has no string data field")
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError("Redis command data must be an object")
                command_id = _required_string(command, "command_id")
                context_fields = context_from_mapping(command)
                context_fields["stream_message_id"] = message_id
                context_fields.setdefault("worker_id", self._worker_id)
                with bind_context(**context_fields):
                    processed_key = (
                        f"avatar:worker:{self._worker_id}:command-processed:{command_id}"
                    )
                    redis = self._require_redis()
                    if await redis.exists(processed_key):
                        logger.info(
                            "Duplicate render command acknowledged",
                            extra={
                                "event": "worker.command.duplicate",
                                "command_type": command.get("type"),
                                "outcome": "duplicate",
                            },
                        )
                        await redis.xack(self._stream, self._group, message_id)
                        return
                    logger.info(
                        "Render command received",
                        extra={
                            "event": "worker.command.received",
                            "command_type": command.get("type"),
                            "attempt": attempt,
                        },
                    )
                    await self._dispatch(command)
                    await redis.set(processed_key, "1", ex=86_400)
                    await redis.xack(self._stream, self._group, message_id)
                    logger.info(
                        "Render command completed",
                        extra={
                            "event": "worker.command.completed",
                            "command_type": command.get("type"),
                            "outcome": "success",
                        },
                    )
                    return
            except Exception:
                failure_context = context_from_mapping(command or {})
                failure_context["stream_message_id"] = message_id
                failure_context.setdefault("worker_id", self._worker_id)
                with bind_context(**failure_context):
                    logger.exception(
                        "Render command failed",
                        extra={
                            "event": "worker.command.failed",
                            "command_type": (command or {}).get("type"),
                            "attempt": attempt,
                            "outcome": "failure",
                        },
                    )
                if attempt < 3:
                    await asyncio.sleep(0.25 * 2 ** (attempt - 1))
        if command is not None:
            await self._publish_event(
                command,
                "avatar.session.failed",
                detail="Render command failed after three attempts",
            )

    async def _dispatch(self, command: dict[str, Any]) -> None:
        command_type = _required_string(command, "type")
        if command_type == "avatar.session.start":
            await self._require_supervisor().start(command)
        elif command_type == "avatar.state.set":
            await self._require_supervisor().dispatch(command)
        elif command_type == "avatar.speech.synthesize":
            await self._require_supervisor().dispatch(command)
        elif command_type == "avatar.session.stop":
            await self._require_supervisor().stop(command)
        else:
            raise ValueError(f"Unsupported render command: {command_type}")

    async def _publish_event(
        self,
        command: dict[str, Any],
        event_type: str,
        detail: str | None = None,
        utterance_id: str | None = None,
    ) -> None:
        session_id = _required_string(command, "session_id")
        event = {
            "schema_version": 1,
            "type": event_type,
            "session_id": session_id,
            "command_id": command.get("command_id"),
            "worker_id": self._worker_id,
            "generation": command.get("generation", 0),
        }
        for key in ("request_id", "trace_id", "span_id", "trace_flags", "turn_id"):
            value = command.get(key)
            if isinstance(value, str):
                event[key] = value
        if detail is not None:
            event["detail"] = detail
        payload = command.get("payload")
        if utterance_id is not None:
            event["utterance_id"] = utterance_id
        elif isinstance(payload, dict) and isinstance(payload.get("utterance_id"), str):
            event["utterance_id"] = payload["utterance_id"]
        await self._require_redis().xadd(
            f"avatar:events:{self._worker_id}:{session_id}",
            {"data": json.dumps(event)},
            maxlen=100,
            approximate=True,
        )
        if event_type == "avatar.session.failed":
            await self._require_redis().set(
                f"avatar:worker:{self._worker_id}:session-failure:{session_id}",
                json.dumps(
                    {
                        "session_id": session_id,
                        "worker_id": self._worker_id,
                        "gpu_uuid": command.get("gpu_uuid"),
                        "generation": command.get("generation", 0),
                        "detail": detail or "unknown failure",
                    }
                ),
                ex=300,
            )
            logger.warning(
                "Persisted session failure marker for Control recovery",
                extra={
                    "event": "worker.session.failure_marker.persisted",
                    "session_id": session_id,
                    "gpu_uuid": command.get("gpu_uuid"),
                    "outcome": "failure",
                },
            )
            await self._publish_failure_telemetry(command, detail or "unknown failure")

    async def _publish_failure_telemetry(
        self,
        command: dict[str, Any],
        detail: str,
    ) -> None:
        session_id = _required_string(command, "session_id")
        gpu_uuid = str(command.get("gpu_uuid") or self._settings.gpu_uuid)
        specification: dict[str, Any] = {}
        # Worker Redis identities are intentionally restricted to the
        # avatar:worker:{worker_id}:* namespace. Do not read Control Plane
        # recovery keys here; the command payload already contains the
        # durable runtime metadata needed for failure telemetry.
        payload = command.get("payload")
        if isinstance(payload, dict):
            specification = dict(payload)
        gpu_payload = (
            []
            if self._resources is None
            else _gpu_resource_payload(
                self._resources,
                gpu_uuid,
                self._settings.maximum_gpu_count,
            )
        )
        sample = {
            "telemetry_type": "composite_failure",
            "worker_id": self._worker_id,
            "boot_id": self._boot_id,
            "identity_generation": self._identity_generation,
            "hardware_sampled_at": datetime.now(timezone.utc).timestamp(),
            "gpu_resources": gpu_payload,
            "child_resources": [
                {
                    **specification,
                    "session_id": session_id,
                    "child_id": f"failed-{session_id}-{command.get('generation', 0)}",
                    "generation": command.get("generation", 0),
                    "assigned_gpu_uuid": gpu_uuid,
                    "phase": "failed",
                    "reserved_vram_mb": command.get("reserved_vram_mb", 0),
                    "reserved_gpu_duty_cycle": command.get(
                        "reserved_gpu_duty_cycle", 0
                    ),
                    "oom": "oom" in detail.lower() or "out of memory" in detail.lower(),
                    "failure_detail": detail[:1000],
                }
            ],
        }
        await self._require_redis().xadd(
            f"avatar:worker:{self._worker_id}:telemetry",
            {"data": json.dumps(sample)},
            maxlen=self._settings.telemetry_stream_maxlen,
            approximate=True,
        )

    async def _ensure_group(self) -> None:
        try:
            await self._require_redis().xgroup_create(
                self._stream,
                self._group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _heartbeat(self) -> None:
        key = f"avatar:worker:{self._worker_id}:heartbeat"
        while True:
            if self._identity_client is not None:
                await self._identity_client.renew()
            children = self._require_supervisor().children
            resources = await self._hardware_probe.sample_resources(
                tuple(
                    child.process.pid
                    for child in children
                    if child.process is not None and child.process.pid is not None
                )
            )
            self._resources = resources
            hardware = _select_gpu(resources, self._settings.gpu_uuid)
            self._hardware = hardware
            schedulable_limit = (
                self._settings.schedulable_vram_mb
                if self._settings.schedulable_vram_mb > 0
                else max(
                    0,
                    hardware.total_vram_mb - self._settings.gpu_reserve_vram_mb,
                )
            )
            observed_fps_values = [
                child.observed_fps for child in children if child.observed_fps > 0
            ]
            heartbeat_data = {
                    "worker_id": self._worker_id,
                    "boot_id": self._boot_id,
                    "identity_generation": self._identity_generation,
                    "status": "ready",
                    "draining": False,
                    "active_slots": self._require_supervisor().active_sessions,
                    "reserved_slots": self._require_supervisor().active_sessions,
                    "reserved_vram_mb": self._require_supervisor().reserved_vram_mb,
                    "reserved_compute": self._require_supervisor().reserved_compute,
                    "free_vram_mb": min(hardware.free_vram_mb, schedulable_limit),
                    "queue_depth": 0,
                    "observed_fps": (
                        sum(observed_fps_values) / len(observed_fps_values)
                        if observed_fps_values
                        else 0.0
                    ),
                    "gpu_used_vram_mb": hardware.used_vram_mb,
                    "gpu_utilization": hardware.gpu_utilization,
                    "gpu_memory_utilization": hardware.memory_utilization,
                    "gpu_temperature_c": hardware.temperature_c,
                    "gpu_power_watts": hardware.power_watts,
                    "hardware_source": hardware.source,
                    "hardware_sampled_at": hardware.sampled_at.timestamp(),
                    "heartbeat_at": datetime.now(timezone.utc).timestamp(),
                    "hardware_probe_error": hardware.probe_error,
                    "host_resources": _host_resource_payload(resources),
                    "gpu_resources": _gpu_resource_payload(
                        resources,
                        self._settings.gpu_uuid,
                        self._settings.maximum_gpu_count,
                    ),
                    "child_resources": _child_resource_payload(resources, children),
                }
            payload = json.dumps(heartbeat_data)
            await self._require_redis().set(
                key,
                payload,
                ex=self._settings.registration_ttl_seconds,
            )
            await self._require_redis().xadd(
                f"avatar:worker:{self._worker_id}:telemetry",
                {"data": payload},
                maxlen=self._settings.telemetry_stream_maxlen,
                approximate=True,
            )
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)

    async def _connect(self) -> None:
        resources = await self._hardware_probe.sample_resources(())
        self._resources = resources
        self._hardware = _select_gpu(resources, self._settings.gpu_uuid)
        if self._settings.registration_url is None:
            self._redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
            return
        identity_client = WorkerIdentityClient(self._settings)
        try:
            identity = await identity_client.enroll(self._hardware, resources)
        except BaseException:
            await identity_client.close()
            raise
        self._identity_client = identity_client
        self._worker_id = identity.worker_id
        self._boot_id = identity.boot_id
        self._identity_generation = identity.identity_generation
        self._stream = f"avatar:commands:{self._worker_id}"
        self._redis = Redis.from_url(
            identity.redis_url,
            username=identity.redis_username,
            password=identity.redis_password,
            decode_responses=True,
        )

    def _require_redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("Render worker Redis connection is not initialized")
        return self._redis

    async def _start_child_runtime(self) -> None:
        service = ChildControlService(self._on_child_event)
        server = ChildControlServer(self._settings.master_socket, service)
        supervisor = ChildSupervisor(
            self._settings,
            self._worker_id,
            self._require_hardware().uuid,
            service,
            self._publish_event,
        )
        self._child_service = service
        self._child_server = server
        self._supervisor = supervisor
        await server.start()
        await supervisor.start_monitoring()

    async def _on_child_event(
        self,
        child: ChildConnection,
        event: Any,
    ) -> None:
        await self._require_supervisor().on_child_event(child, event)

    def _require_supervisor(self) -> ChildSupervisor:
        if self._supervisor is None:
            raise RuntimeError("Worker Master child supervisor is not initialized")
        return self._supervisor

    def _require_hardware(self) -> GpuHardwareSnapshot:
        if self._hardware is None:
            raise RuntimeError("Worker Master hardware probe is not initialized")
        return self._hardware


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _select_gpu(resources: WorkerResourceSnapshot, selected_uuid: str) -> GpuHardwareSnapshot:
    selected = next((gpu for gpu in resources.gpus if gpu.uuid == selected_uuid), None)
    if selected is not None:
        return selected
    if resources.gpus:
        return resources.gpus[0]
    raise RuntimeError("Resource probe returned no GPU snapshot")


def _host_resource_payload(resources: WorkerResourceSnapshot) -> dict[str, object] | None:
    host = resources.host
    if host is None:
        return None
    return {
        "cpu_utilization": host.cpu_utilization,
        "cpu_count_logical": host.cpu_count_logical,
        "load_average_1m": host.load_average_1m,
        "ram_total_bytes": host.ram_total_bytes,
        "ram_used_bytes": host.ram_used_bytes,
        "ram_available_bytes": host.ram_available_bytes,
        "swap_used_bytes": host.swap_used_bytes,
    }


def _gpu_resource_payload(
    resources: WorkerResourceSnapshot,
    selected_uuid: str,
    maximum_gpu_count: int,
) -> list[dict[str, object]]:
    ordered = sorted(resources.gpus, key=lambda gpu: gpu.uuid != selected_uuid)
    return [
        {
            "gpu_uuid": gpu.uuid,
            "gpu_name": gpu.name,
            "compute_capability": gpu.compute_capability,
            "total_vram_mb": gpu.total_vram_mb,
            "free_vram_mb": gpu.free_vram_mb,
            "used_vram_mb": gpu.used_vram_mb,
            "gpu_utilization": gpu.gpu_utilization,
            "memory_utilization": gpu.memory_utilization,
            "encoder_utilization": gpu.encoder_utilization,
            "decoder_utilization": gpu.decoder_utilization,
            "temperature_c": gpu.temperature_c,
            "power_watts": gpu.power_watts,
            "fan_speed_percent": gpu.fan_speed_percent,
            "sm_clock_mhz": gpu.sm_clock_mhz,
            "memory_clock_mhz": gpu.memory_clock_mhz,
            "pcie_tx_bytes_per_second": gpu.pcie_tx_bytes_per_second,
            "pcie_rx_bytes_per_second": gpu.pcie_rx_bytes_per_second,
        }
        for gpu in ordered[:maximum_gpu_count]
    ]


def _child_resource_payload(
    resources: WorkerResourceSnapshot,
    children: tuple[ChildConnection, ...],
) -> list[dict[str, object]]:
    ownership = {
        child.process.pid: child
        for child in children
        if child.process is not None and child.process.pid is not None
    }
    payload: list[dict[str, object]] = []
    for process in resources.processes:
        child = ownership.get(process.pid)
        if child is None:
            continue
        start_payload = (
            child.lifecycle_command.get("payload", {})
            if child.lifecycle_command is not None
            else {}
        )
        if not isinstance(start_payload, dict):
            start_payload = {}
        payload.append(
            {
                "child_id": child.child_id,
                "session_id": child.session_id,
                "pid": process.pid,
                "process_create_time": process.process_create_time,
                "process_identity_matches": (
                    process.process_create_time is not None
                    and abs(process.process_create_time - child.process_create_time) < 1
                ),
                "status": process.status,
                "cpu_utilization": process.cpu_utilization,
                "host_rss_bytes": process.host_rss_bytes,
                "host_memory_percent": process.host_memory_percent,
                "assigned_gpu_uuid": child.gpu_uuid,
                "gpu_uuid": process.gpu_uuid,
                "gpu_memory_bytes": process.gpu_memory_bytes,
                "gpu_memory_percent": process.gpu_memory_percent,
                "gpu_sm_utilization": process.gpu_sm_utilization,
                "gpu_memory_utilization": process.gpu_memory_utilization,
                "gpu_encoder_utilization": process.gpu_encoder_utilization,
                "gpu_decoder_utilization": process.gpu_decoder_utilization,
                "phase": child.runtime_state,
                "livekit_connected": child.livekit_connected,
                "render_fps": child.observed_fps,
                "av_drift_ms": child.av_drift_ms,
                "last_command_sequence": child.last_command_sequence,
                "generation": child.generation,
                "reserved_vram_mb": child.reserved_vram_mb,
                "reserved_gpu_duty_cycle": child.reserved_gpu_duty_cycle,
                "character_id": start_payload.get("character_id", "default"),
                "avatar_model_id": start_payload.get("avatar_model_id", "recorded-avatar"),
                "avatar_revision": start_payload.get("avatar_revision", "default"),
                "voice_id": start_payload.get("voice_id", "default"),
                "tts_model_id": start_payload.get("tts_model_id", "scripted-tone"),
                "tts_revision": start_payload.get("tts_revision", "default"),
                "runtime_version": start_payload.get("runtime_version", "default"),
                "precision": start_payload.get("precision", "fp32"),
                "resolution": start_payload.get("resolution", "640x360"),
                "fps": start_payload.get("fps", 25),
                "isolation_mode": "subprocess",
            }
        )
    return payload
