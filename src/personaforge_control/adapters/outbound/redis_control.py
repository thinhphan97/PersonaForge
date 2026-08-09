import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis

from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    GpuCandidate,
    GpuCapability,
    GpuHeartbeat,
    ResourceResolution,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerStatus,
)
from personaforge_observability import current_context

_ACQUIRE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[4]) == 1 then
  return {0, 'session-exists'}
end
local usage = redis.call('HMGET', KEYS[1], 'slots', 'vram', 'compute')
local slots = tonumber(usage[1]) or 0
local vram = tonumber(usage[2]) or 0
local compute = tonumber(usage[3]) or 0
if slots + 1 > tonumber(ARGV[1]) then return {0, 'slots'} end
if vram + tonumber(ARGV[2]) > tonumber(ARGV[3]) then return {0, 'vram'} end
if compute + tonumber(ARGV[4]) > tonumber(ARGV[5]) then return {0, 'compute'} end
local generation = redis.call('INCR', KEYS[3])
local lease = {
  session_id=ARGV[6], worker_id=ARGV[7], gpu_uuid=ARGV[8],
  slot_id=ARGV[8] .. '-slot-' .. tostring(slots + 1),
  lease_token=ARGV[9], generation=generation,
  reserved_vram_mb=tonumber(ARGV[2]), reserved_gpu_duty_cycle=tonumber(ARGV[4]),
  expires_at=tonumber(ARGV[10]), resource_source=ARGV[11],
  resource_profile_id=ARGV[12]
}
redis.call('HSET', KEYS[1], 'slots', slots + 1, 'vram', vram + tonumber(ARGV[2]),
  'compute', compute + tonumber(ARGV[4]))
redis.call('SET', KEYS[2], cjson.encode(lease), 'PX', ARGV[13])
redis.call('SET', KEYS[4], cjson.encode(lease), 'PX', ARGV[13])
redis.call('SADD', KEYS[5], ARGV[6])
return {generation, cjson.encode(lease)}
"""

_RELEASE_SCRIPT = """
local raw = redis.call('GET', KEYS[2])
if not raw then return 0 end
local lease = cjson.decode(raw)
if lease.lease_token ~= ARGV[1] or tonumber(lease.generation) ~= tonumber(ARGV[2]) then
  return 0
end
local usage = redis.call('HMGET', KEYS[1], 'slots', 'vram', 'compute')
redis.call('HSET', KEYS[1],
  'slots', math.max(0, (tonumber(usage[1]) or 0) - 1),
  'vram', math.max(0, (tonumber(usage[2]) or 0) - tonumber(lease.reserved_vram_mb)),
  'compute', math.max(0, (tonumber(usage[3]) or 0) - tonumber(lease.reserved_gpu_duty_cycle)))
redis.call('DEL', KEYS[2])
return 1
"""

_RENEW_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local lease = cjson.decode(raw)
if lease.lease_token ~= ARGV[1] or tonumber(lease.generation) ~= tonumber(ARGV[2]) then
  return 0
end
lease.expires_at = tonumber(ARGV[3])
redis.call('SET', KEYS[1], cjson.encode(lease), 'PX', ARGV[4])
return 1
"""

_DELETE_ASSIGNMENT_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 1 end
local lease = cjson.decode(raw)
if lease.lease_token ~= ARGV[1] or tonumber(lease.generation) ~= tonumber(ARGV[2]) then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

logger = logging.getLogger(__name__)


class RedisControlPlane:
    """Redis-backed registry, atomic slot allocator, assignments and command bus."""

    def __init__(self, redis_url: str, slot_lease_seconds: int = 30) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._slot_lease_ttl = timedelta(seconds=slot_lease_seconds)
        self._reconciler: asyncio.Task[None] | None = None
        self._recovery_handler: Callable[
            [CompositeRequirement, dict[str, object], str, str], Awaitable[bool]
        ] | None = None

    def set_recovery_handler(
        self,
        handler: Callable[
            [CompositeRequirement, dict[str, object], str, str], Awaitable[bool]
        ],
    ) -> None:
        self._recovery_handler = handler

    async def startup(self) -> None:
        await self._redis.ping()
        if self._reconciler is None:
            self._reconciler = asyncio.create_task(
                self._reconcile_assignments(),
                name="composite-slot-reconciler",
            )

    async def shutdown(self) -> None:
        if self._reconciler is not None:
            self._reconciler.cancel()
            await asyncio.gather(self._reconciler, return_exceptions=True)
            self._reconciler = None
        await self._redis.aclose()

    async def heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        ttl = max(1, round((heartbeat.expires_at - _utc_now()).total_seconds()))
        await self._redis.set(
            _heartbeat_key(heartbeat.worker_id),
            json.dumps(_heartbeat_data(heartbeat)),
            ex=ttl,
        )

    async def candidates(
        self,
        requirement: CompositeRequirement,
        now: datetime,
    ) -> tuple[GpuCandidate, ...]:
        worker_ids = await _smembers(self._redis, "personaforge:control:workers")
        candidates: list[GpuCandidate] = []
        for worker_id in worker_ids:
            registration_raw, heartbeat_raw, heartbeat_ttl = await _execute(
                self._redis,
                "EVAL",
                "return {redis.call('GET', KEYS[1]), redis.call('GET', KEYS[2]), "
                "redis.call('PTTL', KEYS[2])}",
                "2",
                _registration_key(worker_id),
                _heartbeat_key(worker_id),
            )
            if not registration_raw or not heartbeat_raw or int(heartbeat_ttl) <= 0:
                continue
            registration = _registration_from_data(json.loads(registration_raw))
            heartbeat = _heartbeat_from_data(
                json.loads(heartbeat_raw),
                now + timedelta(milliseconds=int(heartbeat_ttl)),
            )
            capabilities = registration.capabilities
            if (
                heartbeat.boot_id != registration.boot_id
                or heartbeat.identity_generation != registration.identity_generation
            ):
                continue
            if (
                registration.lease_expires_at <= now
                or registration.status is not WorkerStatus.READY
            ):
                continue
            if heartbeat.status is not WorkerStatus.READY or heartbeat.draining:
                continue
            if requirement.runtime not in capabilities.supported_runtimes:
                logger.info(
                    "Worker does not advertise the requested composite runtime",
                    extra={
                        "event": "control.candidate.runtime_mismatch",
                        "worker_id": worker_id,
                        "session_id": requirement.session_id,
                        "requested_runtime": requirement.runtime,
                        "supported_runtimes": list(capabilities.supported_runtimes),
                        "reason": "runtime_not_supported",
                        "outcome": "rejected",
                    },
                )
                continue
            heartbeat_by_uuid = {item.gpu_uuid: item for item in heartbeat.gpu_resources}
            for gpu_capability in capabilities.advertised_gpus():
                gpu = heartbeat_by_uuid.get(gpu_capability.gpu_uuid)
                if gpu is None:
                    # Backward-compatible workers only reported their selected GPU at top level.
                    if gpu_capability.gpu_uuid != capabilities.gpu_uuid:
                        continue
                    gpu = GpuHeartbeat(
                        gpu_uuid=capabilities.gpu_uuid,
                        gpu_name=capabilities.gpu_name,
                        compute_capability=capabilities.compute_capability,
                        total_vram_mb=capabilities.total_vram_mb,
                        free_vram_mb=heartbeat.free_vram_mb,
                        used_vram_mb=heartbeat.gpu_used_vram_mb,
                        gpu_utilization=heartbeat.gpu_utilization,
                        memory_utilization=heartbeat.gpu_memory_utilization,
                        temperature_c=heartbeat.gpu_temperature_c,
                        power_watts=heartbeat.gpu_power_watts,
                        healthy=heartbeat.hardware_probe_error is None,
                    )
                if not gpu.healthy:
                    continue
                if gpu.temperature_c is not None and gpu.temperature_c >= 90:
                    continue
                if _compute_capability(gpu_capability.compute_capability) < _compute_capability(
                    requirement.minimum_compute_capability
                ):
                    continue
                usage = await _hmget(
                    self._redis,
                    _usage_key(worker_id, gpu.gpu_uuid),
                    ["slots", "vram", "compute"],
                )
                slots = int(usage[0] or 0)
                reserved_vram = int(usage[1] or 0)
                reserved_compute = float(usage[2] or 0)
                utilization_ewma_raw = await _hget(
                    self._redis, _runtime_key(worker_id, gpu.gpu_uuid), "utilization_ewma"
                )
                failure_penalty_raw, cooldown_until_raw = await _hmget(
                    self._redis,
                    _runtime_key(worker_id, gpu.gpu_uuid),
                    ["failure_penalty", "cooldown_until"],
                )
                if cooldown_until_raw is not None and float(cooldown_until_raw) > now.timestamp():
                    continue
                utilization_ewma = (
                    float(utilization_ewma_raw)
                    if utilization_ewma_raw is not None
                    else gpu.gpu_utilization
                )
                candidates.append(
                    GpuCandidate(
                        registration=registration,
                        heartbeat=heartbeat,
                        gpu=gpu,
                        reserved_slots=slots,
                        reserved_vram_mb=reserved_vram,
                        reserved_compute=reserved_compute,
                        utilization_ewma=utilization_ewma,
                        recent_failure_penalty=float(failure_penalty_raw or 0),
                    )
                )
        return tuple(candidates)

    async def inventory(
        self,
        now: datetime,
    ) -> tuple[tuple[WorkerRegistration, WorkerHeartbeat | None], ...]:
        worker_ids = await _smembers(self._redis, "personaforge:control:workers")
        snapshots: list[tuple[WorkerRegistration, WorkerHeartbeat | None]] = []
        for worker_id in sorted(worker_ids):
            registration_raw, heartbeat_raw, heartbeat_ttl = await _execute(
                self._redis,
                "EVAL",
                "return {redis.call('GET', KEYS[1]), redis.call('GET', KEYS[2]), "
                "redis.call('PTTL', KEYS[2])}",
                "2",
                _registration_key(worker_id),
                _heartbeat_key(worker_id),
            )
            if not registration_raw:
                await _srem(self._redis, "personaforge:control:workers", worker_id)
                continue
            heartbeat = None
            if heartbeat_raw and int(heartbeat_ttl) > 0:
                heartbeat = _heartbeat_from_data(
                    json.loads(heartbeat_raw),
                    now + timedelta(milliseconds=int(heartbeat_ttl)),
                )
            snapshots.append((_registration_from_data(json.loads(registration_raw)), heartbeat))
        return tuple(snapshots)

    async def acquire(
        self,
        candidate: GpuCandidate,
        requirement: CompositeRequirement,
        resolution: ResourceResolution,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None:
        registration = candidate.registration
        gpu = candidate.gpu
        gpu_capability = next(
            item
            for item in registration.capabilities.advertised_gpus()
            if item.gpu_uuid == gpu.gpu_uuid
        )
        if gpu.free_vram_mb < requirement.peak_vram_mb:
            return None
        ttl_ms = max(1, round(ttl.total_seconds() * 1_000))
        expires_at = now + ttl
        lease_token = uuid4().hex
        response = await _eval(
            self._redis,
            _ACQUIRE_SCRIPT,
            5,
            _usage_key(registration.worker_id, gpu.gpu_uuid),
            _slot_lease_key(requirement.session_id),
            _generation_key(requirement.session_id),
            _assignment_key(requirement.session_id),
            "personaforge:control:assignments",
            str(gpu_capability.max_slots),
            str(requirement.peak_vram_mb),
            str(gpu_capability.schedulable_vram_mb),
            str(requirement.gpu_duty_cycle),
            str(gpu_capability.compute_budget),
            requirement.session_id,
            registration.worker_id,
            gpu.gpu_uuid,
            lease_token,
            str(expires_at.timestamp()),
            resolution.source,
            resolution.profile_id or "",
            str(ttl_ms * 2),
        )
        if int(response[0]) == 0:
            return None
        return _lease_from_data(json.loads(response[1]))

    async def renew(
        self,
        lease: CompositeSlotLease,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None:
        expires_at = now + ttl
        ttl_ms = max(1, round(ttl.total_seconds() * 1_000))
        result = await _eval(
            self._redis,
            _RENEW_SCRIPT,
            1,
            _slot_lease_key(lease.session_id),
            lease.lease_token,
            str(lease.generation),
            str(expires_at.timestamp()),
            str(ttl_ms),
        )
        return replace(lease, expires_at=expires_at) if result else None

    async def release(self, lease: CompositeSlotLease) -> bool:
        result = await _eval(
            self._redis,
            _RELEASE_SCRIPT,
            2,
            _usage_key(lease.worker_id, lease.gpu_uuid),
            _slot_lease_key(lease.session_id),
            lease.lease_token,
            str(lease.generation),
        )
        return bool(result)

    async def get(self, session_id: str) -> CompositeSlotLease | None:
        raw = await self._redis.get(_assignment_key(session_id))
        return None if raw is None else _lease_from_data(json.loads(raw))

    async def save(self, lease: CompositeSlotLease) -> None:
        ttl = max(1, round((lease.expires_at - _utc_now()).total_seconds()))
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.set(
            _assignment_key(lease.session_id),
            json.dumps(_lease_data(lease)),
            ex=ttl,
        )
        pipeline.sadd("personaforge:control:assignments", lease.session_id)
        pipeline.expire(_recovery_start_key(lease.session_id), ttl * 2)
        pipeline.expire(_recovery_boot_key(lease.session_id), ttl * 2)
        await pipeline.execute()

    async def delete(self, session_id: str, lease_token: str, generation: int) -> bool:
        result = await _eval(
            self._redis,
            _DELETE_ASSIGNMENT_SCRIPT,
            1,
            _assignment_key(session_id),
            lease_token,
            str(generation),
        )
        if result:
            await _srem(self._redis, "personaforge:control:assignments", session_id)
            await self._redis.delete(
                _recovery_start_key(session_id),
                _recovery_boot_key(session_id),
            )
        return bool(result)

    async def start(
        self,
        lease: CompositeSlotLease,
        requirement: CompositeRequirement,
        payload: dict[str, object],
    ) -> None:
        start_payload = {
            **payload,
            "character_id": requirement.character_id,
            "avatar_model_id": requirement.avatar_model_id,
            "tts_model_id": requirement.tts_model_id,
            "voice_id": requirement.voice_id,
            "avatar_revision": requirement.avatar_revision,
            "tts_revision": requirement.tts_revision,
            "runtime_version": requirement.runtime_version,
            "precision": requirement.precision,
            "isolation_mode": requirement.isolation_mode,
            "runtime": requirement.runtime,
            "minimum_compute_capability": requirement.minimum_compute_capability,
            "resolution": requirement.resolution,
            "fps": requirement.fps,
            "peak_vram_mb": requirement.peak_vram_mb,
            "gpu_duty_cycle": requirement.gpu_duty_cycle,
        }
        ttl = max(1, round(self._slot_lease_ttl.total_seconds() * 2))
        await self._redis.set(
            _recovery_start_key(lease.session_id),
            json.dumps(start_payload),
            ex=ttl,
        )
        await self._command(
            lease,
            "avatar.session.start",
            start_payload,
        )
        heartbeat_raw = await self._redis.get(_heartbeat_key(lease.worker_id))
        boot_id = _boot_id_from_heartbeat(heartbeat_raw)
        if boot_id is not None:
            await self._redis.set(
                _recovery_boot_key(lease.session_id),
                boot_id,
                ex=ttl,
            )

    async def stop(self, lease: CompositeSlotLease) -> None:
        await self._command(lease, "avatar.session.stop", {})

    async def change_state(
        self,
        lease: CompositeSlotLease,
        state: str,
        state_version: int,
    ) -> None:
        await self._command(
            lease,
            "avatar.state.set",
            {"state": state, "state_version": state_version},
        )

    async def speak(
        self,
        lease: CompositeSlotLease,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None:
        await self._command(
            lease,
            "avatar.speech.synthesize",
            {
                "turn_id": turn_id,
                "utterance_id": utterance_id,
                "text": text,
                "sequence": 1,
                "is_final": True,
            },
        )

    async def _command(
        self,
        lease: CompositeSlotLease,
        command_type: str,
        payload: dict[str, object],
    ) -> None:
        logging_context = current_context()
        command = {
            "schema_version": 2,
            "type": command_type,
            "command_id": uuid4().hex,
            "session_id": lease.session_id,
            "worker_id": lease.worker_id,
            "gpu_uuid": lease.gpu_uuid,
            "generation": lease.generation,
            "slot_id": lease.slot_id,
            "lease_token": lease.lease_token,
            "reserved_vram_mb": lease.reserved_vram_mb,
            "reserved_gpu_duty_cycle": lease.reserved_gpu_duty_cycle,
            "resource_source": lease.resource_source,
            "resource_profile_id": lease.resource_profile_id,
            "created_at": _utc_now().isoformat(),
            "payload": payload,
        }
        for key in (
            "request_id",
            "trace_id",
            "span_id",
            "trace_flags",
            "turn_id",
            "utterance_id",
        ):
            value = logging_context.get(key)
            if isinstance(value, str):
                command[key] = value
        await self._redis.xadd(
            f"avatar:commands:{lease.worker_id}",
            {"data": json.dumps(command)},
        )
        logger.info(
            "Render command published",
            extra={
                "event": "control.command.published",
                "session_id": lease.session_id,
                "worker_id": lease.worker_id,
                "gpu_uuid": lease.gpu_uuid,
                "command_id": command["command_id"],
                "command_type": command_type,
                "outcome": "success",
            },
        )

    async def _reconcile_assignments(self) -> None:
        interval = max(1.0, self._slot_lease_ttl.total_seconds() / 3)
        while True:
            try:
                session_ids = await _smembers(
                    self._redis,
                    "personaforge:control:assignments",
                )
                for session_id in session_ids:
                    lease = await self.get(session_id)
                    if lease is None:
                        await _srem(
                            self._redis,
                            "personaforge:control:assignments",
                            session_id,
                        )
                        continue
                    heartbeat_raw = await self._redis.get(_heartbeat_key(lease.worker_id))
                    if heartbeat_raw is not None:
                        failure_raw = await self._redis.get(
                            _session_failure_key(lease.worker_id, lease.session_id)
                        )
                        if failure_raw is not None:
                            if await self._recover_lost_assignment(
                                lease,
                                failed_worker_id="",
                            ):
                                continue
                        renewed = await self.renew(
                            lease,
                            _utc_now(),
                            self._slot_lease_ttl,
                        )
                        if renewed is not None:
                            await self.save(renewed)
                            await self._recover_for_worker_boot(renewed, heartbeat_raw)
                            continue
                    if not await self._recover_lost_assignment(lease):
                        await self.release(lease)
                        await self.delete(
                            lease.session_id,
                            lease.lease_token,
                            lease.generation,
                        )
                await self._retry_pending_recoveries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Composite slot reconciliation failed")
            await asyncio.sleep(interval)

    async def _recover_lost_assignment(
        self,
        lease: CompositeSlotLease,
        failed_worker_id: str | None = None,
    ) -> bool:
        if self._recovery_handler is None:
            return False
        start_raw = await self._redis.get(_recovery_start_key(lease.session_id))
        if start_raw is None:
            return False
        lock_key = _recovery_lock_key(lease.session_id)
        acquired = await self._redis.set(
            lock_key,
            "1",
            nx=True,
            ex=max(5, int(self._slot_lease_ttl.total_seconds())),
        )
        if not acquired:
            return True
        try:
            payload = json.loads(start_raw)
            if not isinstance(payload, dict):
                return False
            logger.warning(
                "Starting composite session recovery",
                extra={
                    "event": "control.session.recovery.started",
                    "session_id": lease.session_id,
                    "failed_worker_id": lease.worker_id
                    if failed_worker_id is None
                    else failed_worker_id,
                    "failed_gpu_uuid": lease.gpu_uuid,
                    "reason": "worker_heartbeat_timeout"
                    if failed_worker_id is None
                    else "child_failure",
                    "outcome": "started",
                },
            )
            requirement = _requirement_from_recovery_payload(
                lease.session_id,
                payload,
                default_peak_vram_mb=lease.reserved_vram_mb,
                default_gpu_duty_cycle=lease.reserved_gpu_duty_cycle,
            )
            success = await self._recovery_handler(
                requirement,
                payload,
                lease.worker_id if failed_worker_id is None else failed_worker_id,
                lease.gpu_uuid,
            )
            if success:
                await self._redis.delete(
                    _session_failure_key(lease.worker_id, lease.session_id)
                )
                await self._redis.srem(_recovery_pending_set(), lease.session_id)
                await self._redis.delete(_recovery_pending_key(lease.session_id))
            else:
                logger.warning(
                    "Composite session recovery is pending capacity",
                    extra={
                        "event": "control.session.recovery.pending",
                        "session_id": lease.session_id,
                        "failed_worker_id": lease.worker_id
                        if failed_worker_id is None
                        else failed_worker_id,
                        "failed_gpu_uuid": lease.gpu_uuid,
                        "outcome": "pending",
                    },
                )
                await self._redis.set(
                    _recovery_pending_key(lease.session_id),
                    json.dumps(
                        {
                            "payload": payload,
                            "worker_id": lease.worker_id,
                            "gpu_uuid": lease.gpu_uuid,
                            "exclude_worker": bool(failed_worker_id),
                            "default_peak_vram_mb": lease.reserved_vram_mb,
                            "default_gpu_duty_cycle": lease.reserved_gpu_duty_cycle,
                        }
                    ),
                    ex=max(60, int(self._slot_lease_ttl.total_seconds() * 10)),
                )
                await self._redis.sadd(_recovery_pending_set(), lease.session_id)
            return True
        finally:
            await self._redis.delete(lock_key)

    async def _retry_pending_recoveries(self) -> None:
        if self._recovery_handler is None:
            return
        for session_id in await _smembers(self._redis, _recovery_pending_set()):
            raw = await self._redis.get(_recovery_pending_key(session_id))
            if raw is None:
                await self._redis.srem(_recovery_pending_set(), session_id)
                continue
            data = json.loads(raw)
            payload = data.get("payload")
            if not isinstance(payload, dict):
                await self._redis.srem(_recovery_pending_set(), session_id)
                continue
            lock_key = _recovery_lock_key(session_id)
            if not await self._redis.set(
                lock_key,
                "1",
                nx=True,
                ex=max(5, int(self._slot_lease_ttl.total_seconds())),
            ):
                continue
            try:
                requirement = _requirement_from_recovery_payload(
                    session_id,
                    payload,
                    default_peak_vram_mb=int(data.get("default_peak_vram_mb", 0)),
                    default_gpu_duty_cycle=float(data.get("default_gpu_duty_cycle", 0)),
                )
                success = await self._recovery_handler(
                    requirement,
                    payload,
                    (
                        str(data.get("worker_id", ""))
                        if bool(data.get("exclude_worker", True))
                        else ""
                    ),
                    str(data.get("gpu_uuid", "")),
                )
                if success:
                    await self._redis.delete(
                        _session_failure_key(str(data.get("worker_id", "")), session_id)
                    )
                    await self._redis.srem(_recovery_pending_set(), session_id)
                    await self._redis.delete(_recovery_pending_key(session_id))
            finally:
                await self._redis.delete(lock_key)

    async def _recover_for_worker_boot(
        self,
        lease: CompositeSlotLease,
        heartbeat_raw: str,
    ) -> None:
        boot_id = _boot_id_from_heartbeat(heartbeat_raw)
        if boot_id is None:
            return
        recovery_boot = await self._redis.get(_recovery_boot_key(lease.session_id))
        if recovery_boot == boot_id:
            return
        start_raw = await self._redis.get(_recovery_start_key(lease.session_id))
        if start_raw is None:
            logger.warning(
                "Cannot recover session without start specification session=%s worker=%s",
                lease.session_id,
                lease.worker_id,
            )
            return
        start_payload = json.loads(start_raw)
        if not isinstance(start_payload, dict):
            raise ValueError("Composite recovery start payload must be an object")
        await self._command(lease, "avatar.session.start", start_payload)
        ttl = max(1, round(self._slot_lease_ttl.total_seconds() * 2))
        await self._redis.set(
            _recovery_boot_key(lease.session_id),
            boot_id,
            ex=ttl,
        )
        logger.info(
            "Reissued composite start after worker restart session=%s worker=%s boot=%s",
            lease.session_id,
            lease.worker_id,
            boot_id,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _eval(redis: Redis, script: str, numkeys: int, *args: str) -> Any:
    operation = cast(Awaitable[Any], redis.eval(script, numkeys, *args))
    return await operation


async def _execute(redis: Redis, *args: str) -> Any:
    operation = cast(
        Awaitable[Any],
        redis.execute_command(*args),  # type: ignore[no-untyped-call]
    )
    return await operation


async def _smembers(redis: Redis, key: str) -> set[str]:
    operation = cast(Awaitable[set[str]], redis.smembers(key))
    return await operation


async def _srem(redis: Redis, key: str, member: str) -> int:
    operation = cast(Awaitable[int], redis.srem(key, member))
    return await operation


async def _hmget(redis: Redis, key: str, fields: list[str]) -> list[Any]:
    operation = cast(Awaitable[list[Any]], redis.hmget(key, fields))
    return await operation


async def _hget(redis: Redis, key: str, field: str) -> str | None:
    operation = cast(Awaitable[str | None], redis.hget(key, field))
    return await operation


async def _exists(redis: Redis, key: str) -> bool:
    operation = cast(Awaitable[int], redis.exists(key))
    return bool(await operation)


def _registration_key(worker_id: str) -> str:
    return f"avatar:worker:{worker_id}:registration"


def _heartbeat_key(worker_id: str) -> str:
    return f"avatar:worker:{worker_id}:heartbeat"


def _usage_key(worker_id: str, gpu_uuid: str) -> str:
    if not gpu_uuid:
        return f"personaforge:control:worker-usage:{worker_id}"
    return f"personaforge:control:gpu:{worker_id}:{gpu_uuid}:usage"


def _runtime_key(worker_id: str, gpu_uuid: str) -> str:
    return f"personaforge:control:gpu:{worker_id}:{gpu_uuid}:runtime"


def _slot_lease_key(session_id: str) -> str:
    return f"personaforge:control:slot-lease:{session_id}"


def _generation_key(session_id: str) -> str:
    return f"personaforge:control:session-generation:{session_id}"


def _assignment_key(session_id: str) -> str:
    return f"personaforge:control:assignment:{session_id}"


def _recovery_start_key(session_id: str) -> str:
    return f"personaforge:control:recovery-start:{session_id}"


def _recovery_boot_key(session_id: str) -> str:
    return f"personaforge:control:recovery-boot:{session_id}"


def _recovery_lock_key(session_id: str) -> str:
    return f"personaforge:control:recovery-lock:{session_id}"


def _recovery_pending_key(session_id: str) -> str:
    return f"personaforge:control:recovery-pending:{session_id}"


def _recovery_pending_set() -> str:
    return "personaforge:control:recovery-pending"


def _session_failure_key(worker_id: str, session_id: str) -> str:
    return f"avatar:worker:{worker_id}:session-failure:{session_id}"


def _requirement_from_recovery_payload(
    session_id: str,
    payload: dict[str, object],
    *,
    default_peak_vram_mb: int = 0,
    default_gpu_duty_cycle: float = 0.0,
) -> CompositeRequirement:
    def text(name: str, default: str) -> str:
        value = payload.get(name)
        return value if isinstance(value, str) and value else default

    def number(name: str, default: float) -> float:
        value = payload.get(name)
        return float(value) if isinstance(value, int | float) else default

    def integer(name: str, default: int) -> int:
        value = payload.get(name)
        return int(value) if isinstance(value, int | float) else default

    return CompositeRequirement(
        session_id=session_id,
        character_id=text("character_id", "default"),
        avatar_model_id=text("avatar_model_id", "default"),
        tts_model_id=text("tts_model_id", "default"),
        runtime=text("runtime", "recorded"),
        minimum_compute_capability=text("minimum_compute_capability", "0.0"),
        resolution=text("resolution", "640x360"),
        fps=number("fps", 25.0),
        peak_vram_mb=integer("peak_vram_mb", default_peak_vram_mb),
        gpu_duty_cycle=number("gpu_duty_cycle", default_gpu_duty_cycle),
        voice_id=text("voice_id", "default"),
        avatar_revision=text("avatar_revision", "default"),
        tts_revision=text("tts_revision", "default"),
        runtime_version=text("runtime_version", "default"),
        precision=text("precision", "fp32"),
        isolation_mode=text("isolation_mode", "subprocess"),
    )


def _boot_id_from_heartbeat(raw: str | None) -> str | None:
    if raw is None:
        return None
    data = json.loads(raw)
    boot_id = data.get("boot_id") if isinstance(data, dict) else None
    return boot_id if isinstance(boot_id, str) and boot_id else None


def _compute_capability(value: str) -> tuple[int, int]:
    major, _, minor = value.partition(".")
    try:
        return int(major), int(minor or 0)
    except ValueError:
        return 0, 0


def _registration_from_data(data: dict[str, Any]) -> WorkerRegistration:
    capability_data = data["capabilities"]
    capability_data["supported_runtimes"] = tuple(capability_data.get("supported_runtimes", ()))
    capability_data["gpus"] = tuple(
        GpuCapability(**item) for item in capability_data.get("gpus", ())
    )
    return WorkerRegistration(
        worker_id=data["worker_id"],
        boot_id=data["boot_id"],
        owner_token_hash=data["owner_token_hash"],
        management_token_hash=data["management_token_hash"],
        management_family_id=data["management_family_id"],
        identity_generation=int(data["identity_generation"]),
        capabilities=WorkerCapabilities(**capability_data),
        status=WorkerStatus(data["status"]),
        registered_at=datetime.fromtimestamp(data["registered_at"], timezone.utc),
        lease_expires_at=datetime.fromtimestamp(data["lease_expires_at"], timezone.utc),
        redis_username=data["redis_username"],
    )


def _heartbeat_data(value: WorkerHeartbeat) -> dict[str, object]:
    data = asdict(value)
    data["status"] = value.status.value
    data["hardware_sampled_at"] = value.hardware_sampled_at.timestamp()
    data["heartbeat_at"] = value.heartbeat_at.timestamp()
    data["expires_at"] = value.expires_at.timestamp()
    return data


def _heartbeat_from_data(data: dict[str, Any], expires_at: datetime) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_id=data["worker_id"],
        boot_id=data["boot_id"],
        identity_generation=int(data["identity_generation"]),
        status=WorkerStatus(data["status"]),
        draining=bool(data.get("draining", False)),
        active_slots=int(data.get("active_slots", 0)),
        reserved_slots=int(data.get("reserved_slots", 0)),
        reserved_vram_mb=int(data.get("reserved_vram_mb", 0)),
        reserved_compute=float(data.get("reserved_compute", 0)),
        free_vram_mb=int(data.get("free_vram_mb", 0)),
        queue_depth=int(data.get("queue_depth", 0)),
        observed_fps=float(data.get("observed_fps", 0)),
        gpu_used_vram_mb=int(data.get("gpu_used_vram_mb", 0)),
        gpu_utilization=float(data.get("gpu_utilization", 0)),
        gpu_memory_utilization=float(data.get("gpu_memory_utilization", 0)),
        gpu_temperature_c=_optional_float(data.get("gpu_temperature_c")),
        gpu_power_watts=_optional_float(data.get("gpu_power_watts")),
        hardware_source=str(data.get("hardware_source", "unknown")),
        hardware_sampled_at=datetime.fromtimestamp(
            float(data.get("hardware_sampled_at", _utc_now().timestamp())),
            timezone.utc,
        ),
        hardware_probe_error=(
            str(data["hardware_probe_error"])
            if data.get("hardware_probe_error") is not None
            else None
        ),
        heartbeat_at=datetime.fromtimestamp(
            float(data.get("heartbeat_at", _utc_now().timestamp())),
            timezone.utc,
        ),
        expires_at=expires_at,
        gpu_resources=tuple(
            GpuHeartbeat(
                gpu_uuid=str(item["gpu_uuid"]),
                gpu_name=str(item.get("gpu_name", "unknown")),
                compute_capability=str(item.get("compute_capability", "0.0")),
                total_vram_mb=int(item.get("total_vram_mb", 0)),
                free_vram_mb=int(item.get("free_vram_mb", 0)),
                used_vram_mb=int(item.get("used_vram_mb", 0)),
                gpu_utilization=float(item.get("gpu_utilization", 0)),
                memory_utilization=float(item.get("memory_utilization", 0)),
                encoder_utilization=_optional_float(item.get("encoder_utilization")),
                decoder_utilization=_optional_float(item.get("decoder_utilization")),
                temperature_c=_optional_float(item.get("temperature_c")),
                power_watts=_optional_float(item.get("power_watts")),
                healthy=not bool(item.get("probe_error")),
            )
            for item in data.get("gpu_resources", ())
            if isinstance(item, dict) and item.get("gpu_uuid")
        ),
        host_resources=(
            dict(data["host_resources"]) if isinstance(data.get("host_resources"), dict) else None
        ),
        child_resources=tuple(
            dict(item) for item in data.get("child_resources", ()) if isinstance(item, dict)
        ),
    )


def _lease_data(value: CompositeSlotLease) -> dict[str, object]:
    data = asdict(value)
    data["expires_at"] = value.expires_at.timestamp()
    return data


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float | str):
        return float(value)
    raise TypeError("Expected an optional numeric Redis value")


def _lease_from_data(data: dict[str, Any]) -> CompositeSlotLease:
    return CompositeSlotLease(
        session_id=data["session_id"],
        worker_id=data["worker_id"],
        slot_id=data["slot_id"],
        lease_token=data["lease_token"],
        generation=int(data["generation"]),
        reserved_vram_mb=int(data["reserved_vram_mb"]),
        reserved_gpu_duty_cycle=float(data["reserved_gpu_duty_cycle"]),
        expires_at=datetime.fromtimestamp(data["expires_at"], timezone.utc),
        gpu_uuid=str(data.get("gpu_uuid", "")),
        resource_source=str(data.get("resource_source", "request")),
        resource_profile_id=(
            str(data["resource_profile_id"]) if data.get("resource_profile_id") else None
        ),
    )
