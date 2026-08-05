import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from avatar_chat.domain.models import AvatarState
from avatar_worker.adapters.outbound.gpu_probe import build_gpu_probe
from avatar_worker.domain.hardware import GpuHardwareSnapshot
from avatar_worker.identity import WorkerIdentityClient
from avatar_worker.livekit_session import LiveKitRenderSession
from avatar_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ManagedRenderSession:
    session: LiveKitRenderSession
    task: asyncio.Task[None]
    generation: int
    lease_token: str
    slot_id: str
    reserved_vram_mb: int
    reserved_gpu_duty_cycle: float


class RenderSessionManager:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._sessions: dict[str, _ManagedRenderSession] = {}
        self._highest_generation: dict[str, int] = {}

    async def start(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        payload = _required_mapping(command, "payload")
        generation = _optional_int(command, "generation", 0)
        lease_token = _optional_string(command, "lease_token", "legacy")
        slot_id = _optional_string(command, "slot_id", "legacy-slot")
        current = self._sessions.get(session_id)
        if (
            current is not None
            and current.generation == generation
            and current.lease_token == lease_token
            and current.slot_id == slot_id
        ):
            await current.session.wait_ready()
            return
        if generation < self._highest_generation.get(session_id, -1):
            raise RuntimeError(f"Stale render generation for session: {session_id}")
        if current is not None:
            await self._stop_current(session_id, current)
        session = LiveKitRenderSession(
            session_id=session_id,
            character_id=_required_string(payload, "character_id"),
            livekit_url=_required_string(payload, "livekit_url"),
            publisher_token=_required_string(payload, "publisher_token"),
            settings=self._settings,
        )
        task = asyncio.create_task(session.run(), name=f"render-{session_id}")
        managed = _ManagedRenderSession(
            session,
            task,
            generation,
            lease_token,
            slot_id,
            _optional_int(command, "reserved_vram_mb", 0),
            _optional_float(command, "reserved_gpu_duty_cycle", 0),
        )
        self._sessions[session_id] = managed
        self._highest_generation[session_id] = generation

        def on_done(completed: asyncio.Task[None]) -> None:
            self._session_done(session_id, completed)

        task.add_done_callback(on_done)
        try:
            await session.wait_ready()
        except BaseException:
            await self.stop(session_id)
            raise

    async def set_state(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        payload = _required_mapping(command, "payload")
        context = self._sessions.get(session_id)
        if context is None:
            raise RuntimeError(f"Render session not found: {session_id}")
        _require_command_owner(command, context)
        context.session.set_state(
            AvatarState(_required_string(payload, "state")),
            _required_int(payload, "state_version"),
        )

    async def speak(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        context = self._sessions.get(session_id)
        if context is None:
            raise RuntimeError(f"Render session not found: {session_id}")
        _require_command_owner(command, context)
        payload = _required_mapping(command, "payload")
        await context.session.speak(
            turn_id=_required_string(payload, "turn_id"),
            utterance_id=_required_string(payload, "utterance_id"),
            text=_required_string(payload, "text"),
            sequence=_required_int(payload, "sequence"),
            is_final=_required_bool(payload, "is_final"),
        )

    async def stop(self, session_id: str) -> None:
        context = self._sessions.pop(session_id, None)
        if context is None:
            return
        await self._stop_current(session_id, context)

    async def stop_command(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        context = self._sessions.get(session_id)
        if context is None:
            return
        _require_command_owner(command, context)
        self._sessions.pop(session_id, None)
        await self._stop_current(session_id, context)

    async def _stop_current(
        self,
        session_id: str,
        context: _ManagedRenderSession,
    ) -> None:
        await context.session.stop()
        try:
            await asyncio.wait_for(context.task, timeout=5)
        except TimeoutError:
            context.task.cancel()
            await asyncio.gather(context.task, return_exceptions=True)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.stop(session_id) for session_id in tuple(self._sessions)))

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    @property
    def reserved_vram_mb(self) -> int:
        return sum(item.reserved_vram_mb for item in self._sessions.values())

    @property
    def reserved_compute(self) -> float:
        return sum(item.reserved_gpu_duty_cycle for item in self._sessions.values())

    def _session_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        current = self._sessions.get(session_id)
        if current is not None and current.task is task:
            self._sessions.pop(session_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Render session crashed session=%s",
                session_id,
                exc_info=task.exception(),
            )


class RedisRenderWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._redis: Redis | None = None
        self._worker_id = settings.worker_id or "avatar-worker-1"
        self._boot_id = "legacy"
        self._identity_generation = 0
        self._stream = f"avatar:commands:{self._worker_id}"
        self._group = "avatar-render-workers"
        self._manager = RenderSessionManager(settings)
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

    async def run(self) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self._connect()
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
            await self._manager.shutdown()
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
                processed_key = (
                    f"avatar:worker:{self._worker_id}:command-processed:{command_id}"
                )
                redis = self._require_redis()
                if await redis.exists(processed_key):
                    await redis.xack(self._stream, self._group, message_id)
                    return
                await self._dispatch(command)
                await redis.set(processed_key, "1", ex=86_400)
                await redis.xack(self._stream, self._group, message_id)
                return
            except Exception:
                logger.exception(
                    "Command failed id=%s attempt=%s",
                    message_id,
                    attempt,
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
            await self._manager.start(command)
            await self._publish_event(command, "avatar.session.ready")
        elif command_type == "avatar.state.set":
            await self._manager.set_state(command)
        elif command_type == "avatar.speech.synthesize":
            await self._manager.speak(command)
            await self._publish_event(command, "avatar.speech.completed")
        elif command_type == "avatar.session.stop":
            await self._manager.stop_command(command)
            await self._publish_event(command, "avatar.session.stopped")
        else:
            raise ValueError(f"Unsupported render command: {command_type}")

    async def _publish_event(
        self,
        command: dict[str, Any],
        event_type: str,
        detail: str | None = None,
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
        if detail is not None:
            event["detail"] = detail
        payload = command.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("utterance_id"), str):
            event["utterance_id"] = payload["utterance_id"]
        await self._require_redis().xadd(
            f"avatar:events:{self._worker_id}:{session_id}",
            {"data": json.dumps(event)},
            maxlen=100,
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
            self._hardware = await self._hardware_probe.sample()
            hardware = self._hardware
            schedulable_limit = (
                self._settings.schedulable_vram_mb
                if self._settings.schedulable_vram_mb > 0
                else max(
                    0,
                    hardware.total_vram_mb - self._settings.gpu_reserve_vram_mb,
                )
            )
            payload = json.dumps(
                {
                    "worker_id": self._worker_id,
                    "boot_id": self._boot_id,
                    "identity_generation": self._identity_generation,
                    "status": "ready",
                    "draining": False,
                    "active_slots": self._manager.active_sessions,
                    "reserved_slots": self._manager.active_sessions,
                    "reserved_vram_mb": self._manager.reserved_vram_mb,
                    "reserved_compute": self._manager.reserved_compute,
                    "free_vram_mb": min(hardware.free_vram_mb, schedulable_limit),
                    "queue_depth": 0,
                    "observed_fps": self._settings.video_fps,
                    "gpu_used_vram_mb": hardware.used_vram_mb,
                    "gpu_utilization": hardware.gpu_utilization,
                    "gpu_memory_utilization": hardware.memory_utilization,
                    "gpu_temperature_c": hardware.temperature_c,
                    "gpu_power_watts": hardware.power_watts,
                    "hardware_source": hardware.source,
                    "hardware_sampled_at": hardware.sampled_at.timestamp(),
                    "hardware_probe_error": hardware.probe_error,
                }
            )
            await self._require_redis().set(
                key,
                payload,
                ex=self._settings.registration_ttl_seconds,
            )
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)

    async def _connect(self) -> None:
        self._hardware = await self._hardware_probe.sample()
        if self._settings.registration_url is None:
            self._redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
            return
        identity_client = WorkerIdentityClient(self._settings)
        try:
            identity = await identity_client.enroll(self._hardware)
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


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _optional_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _optional_string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _optional_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return float(value)


def _require_command_owner(
    command: dict[str, Any],
    context: _ManagedRenderSession,
) -> None:
    generation = _optional_int(command, "generation", 0)
    lease_token = _optional_string(command, "lease_token", "legacy")
    if generation != context.generation or lease_token != context.lease_token:
        raise RuntimeError("Render command does not own the active composite session")
