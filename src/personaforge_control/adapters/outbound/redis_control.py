import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

from redis.asyncio import Redis

from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerStatus,
)

_ACQUIRE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then return {0, 'session-exists'} end
local usage = redis.call('HMGET', KEYS[1], 'slots', 'vram', 'compute')
local slots = tonumber(usage[1]) or 0
local vram = tonumber(usage[2]) or 0
local compute = tonumber(usage[3]) or 0
if slots + 1 > tonumber(ARGV[1]) then return {0, 'slots'} end
if vram + tonumber(ARGV[2]) > tonumber(ARGV[3]) then return {0, 'vram'} end
if compute + tonumber(ARGV[4]) > tonumber(ARGV[5]) then return {0, 'compute'} end
local generation = redis.call('INCR', KEYS[3])
local lease = {
  session_id=ARGV[6], worker_id=ARGV[7], slot_id='slot-' .. tostring(slots + 1),
  lease_token=ARGV[8], generation=generation,
  reserved_vram_mb=tonumber(ARGV[2]), reserved_gpu_duty_cycle=tonumber(ARGV[4]),
  expires_at=tonumber(ARGV[9])
}
redis.call('HSET', KEYS[1], 'slots', slots + 1, 'vram', vram + tonumber(ARGV[2]),
  'compute', compute + tonumber(ARGV[4]))
redis.call('SET', KEYS[2], cjson.encode(lease), 'PX', ARGV[10])
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
    ) -> tuple[tuple[WorkerRegistration, WorkerHeartbeat], ...]:
        worker_ids = await _smembers(self._redis, "personaforge:control:workers")
        candidates: list[tuple[WorkerRegistration, WorkerHeartbeat]] = []
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
                registration.lease_expires_at <= now
                or registration.status is not WorkerStatus.READY
            ):
                continue
            if heartbeat.status is not WorkerStatus.READY or heartbeat.draining:
                continue
            if requirement.runtime not in capabilities.supported_runtimes:
                continue
            if _compute_capability(capabilities.compute_capability) < _compute_capability(
                requirement.minimum_compute_capability
            ):
                continue
            if heartbeat.free_vram_mb < requirement.peak_vram_mb:
                continue
            candidates.append((registration, heartbeat))
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
            snapshots.append(
                (_registration_from_data(json.loads(registration_raw)), heartbeat)
            )
        return tuple(snapshots)

    async def acquire(
        self,
        registration: WorkerRegistration,
        heartbeat: WorkerHeartbeat,
        requirement: CompositeRequirement,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None:
        if heartbeat.free_vram_mb < requirement.peak_vram_mb:
            return None
        ttl_ms = max(1, round(ttl.total_seconds() * 1_000))
        expires_at = now + ttl
        lease_token = uuid4().hex
        response = await _eval(
            self._redis,
            _ACQUIRE_SCRIPT,
            3,
            _usage_key(registration.worker_id),
            _slot_lease_key(requirement.session_id),
            _generation_key(requirement.session_id),
            str(registration.capabilities.max_slots),
            str(requirement.peak_vram_mb),
            str(registration.capabilities.schedulable_vram_mb),
            str(requirement.gpu_duty_cycle),
            str(registration.capabilities.compute_budget),
            requirement.session_id,
            registration.worker_id,
            lease_token,
            str(expires_at.timestamp()),
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
            _usage_key(lease.worker_id),
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
        return bool(result)

    async def start(
        self,
        lease: CompositeSlotLease,
        requirement: CompositeRequirement,
        payload: dict[str, object],
    ) -> None:
        await self._command(
            lease,
            "avatar.session.start",
            {
                **payload,
                "character_id": requirement.character_id,
                "avatar_model_id": requirement.avatar_model_id,
                "tts_model_id": requirement.tts_model_id,
                "runtime": requirement.runtime,
                "resolution": requirement.resolution,
                "fps": requirement.fps,
            },
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
        command = {
            "schema_version": 2,
            "type": command_type,
            "command_id": uuid4().hex,
            "session_id": lease.session_id,
            "worker_id": lease.worker_id,
            "generation": lease.generation,
            "slot_id": lease.slot_id,
            "lease_token": lease.lease_token,
            "reserved_vram_mb": lease.reserved_vram_mb,
            "reserved_gpu_duty_cycle": lease.reserved_gpu_duty_cycle,
            "created_at": _utc_now().isoformat(),
            "payload": payload,
        }
        await self._redis.xadd(
            f"avatar:commands:{lease.worker_id}",
            {"data": json.dumps(command)},
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
                    if await _exists(self._redis, _heartbeat_key(lease.worker_id)):
                        renewed = await self.renew(
                            lease,
                            _utc_now(),
                            self._slot_lease_ttl,
                        )
                        if renewed is not None:
                            await self.save(renewed)
                            continue
                    await self.release(lease)
                    await self.delete(
                        lease.session_id,
                        lease.lease_token,
                        lease.generation,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Composite slot reconciliation failed")
            await asyncio.sleep(interval)


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


async def _exists(redis: Redis, key: str) -> bool:
    operation = cast(Awaitable[int], redis.exists(key))
    return bool(await operation)


def _registration_key(worker_id: str) -> str:
    return f"avatar:worker:{worker_id}:registration"


def _heartbeat_key(worker_id: str) -> str:
    return f"avatar:worker:{worker_id}:heartbeat"


def _usage_key(worker_id: str) -> str:
    return f"personaforge:control:worker-usage:{worker_id}"


def _slot_lease_key(session_id: str) -> str:
    return f"personaforge:control:slot-lease:{session_id}"


def _generation_key(session_id: str) -> str:
    return f"personaforge:control:session-generation:{session_id}"


def _assignment_key(session_id: str) -> str:
    return f"personaforge:control:assignment:{session_id}"


def _compute_capability(value: str) -> tuple[int, int]:
    major, _, minor = value.partition(".")
    try:
        return int(major), int(minor or 0)
    except ValueError:
        return 0, 0


def _registration_from_data(data: dict[str, Any]) -> WorkerRegistration:
    capability_data = data["capabilities"]
    capability_data["supported_runtimes"] = tuple(
        capability_data.get("supported_runtimes", ())
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
        heartbeat_at=_utc_now(),
        expires_at=expires_at,
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
    )
