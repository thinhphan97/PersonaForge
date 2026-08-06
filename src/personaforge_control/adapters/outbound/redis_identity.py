import json
from collections.abc import Awaitable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from redis.asyncio import Redis

from personaforge_control.domain.errors import EnrollmentReplay, InvalidEnrollment
from personaforge_control.domain.models import (
    EnrollmentGrant,
    EnrollmentStatus,
    GpuCapability,
    RedisWorkerCredential,
    RegistrationResult,
    WorkerCapabilities,
    WorkerRegistration,
    WorkerStatus,
)

_CLAIM_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {'missing'} end
local grant = cjson.decode(raw)
if grant.status == 'expired' or grant.status == 'revoked' then return {'inactive'} end
if tonumber(grant.expires_at) <= tonumber(ARGV[2]) then
  grant.status = 'expired'
  redis.call('SET', KEYS[1], cjson.encode(grant), 'KEEPTTL')
  return {'expired'}
end
if grant.registration_request_id and grant.registration_request_id ~= cjson.null then
  if grant.registration_request_id == ARGV[1] then return {'owned', raw} end
  return {'replay'}
end
grant.status = 'claiming'
grant.registration_request_id = ARGV[1]
local updated = cjson.encode(grant)
redis.call('SET', KEYS[1], updated, 'KEEPTTL')
return {'claimed', updated}
"""

_RENEW_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local registration = cjson.decode(raw)
if registration.boot_id ~= ARGV[1] or registration.owner_token_hash ~= ARGV[2] then
  return 0
end
registration.status = 'ready'
registration.lease_expires_at = tonumber(ARGV[3])
redis.call('SET', KEYS[1], cjson.encode(registration), 'PX', ARGV[4])
return 1
"""

_DELETE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 1 end
local registration = cjson.decode(raw)
if registration.boot_id ~= ARGV[1] or registration.owner_token_hash ~= ARGV[2] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisIdentityRepository:
    """Identity state with atomic enrollment claim and owner-fenced registration lease."""

    def __init__(self, redis_url: str, result_recovery_seconds: int = 60) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._result_recovery_seconds = result_recovery_seconds

    async def startup(self) -> None:
        await self._redis.ping()

    async def shutdown(self) -> None:
        await self._redis.aclose()

    async def save(self, grant: EnrollmentGrant) -> None:
        ttl = max(1, round((grant.expires_at - _utc_now()).total_seconds()))
        created = await self._redis.set(
            _enrollment_key(grant.token_id),
            json.dumps(_grant_data(grant)),
            ex=ttl,
            nx=True,
        )
        if not created:
            raise InvalidEnrollment("Enrollment token ID already exists")

    async def get(self, token_id: str) -> EnrollmentGrant | None:
        raw = await self._redis.get(_enrollment_key(token_id))
        return None if raw is None else _grant_from_data(json.loads(raw))

    async def claim(
        self,
        token_id: str,
        request_id: str,
        now: datetime,
    ) -> EnrollmentGrant:
        response = await _eval(
            self._redis,
            _CLAIM_SCRIPT,
            1,
            _enrollment_key(token_id),
            request_id,
            str(now.timestamp()),
        )
        status = response[0]
        if status == "replay":
            raise EnrollmentReplay("Enrollment token replay detected")
        if status in {"missing", "inactive", "expired"}:
            raise InvalidEnrollment("Unknown, expired or inactive enrollment token")
        return _grant_from_data(json.loads(response[1]))

    async def activate(
        self,
        token_id: str,
        request_id: str,
        result: RegistrationResult,
    ) -> None:
        grant = await self.get(token_id)
        if grant is None or grant.registration_request_id != request_id:
            raise EnrollmentReplay("Registration request does not own enrollment token")
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.set(
            _result_key(token_id, request_id),
            json.dumps(_registration_result_data(result)),
            ex=self._result_recovery_seconds,
        )
        pipeline.set(
            _enrollment_key(token_id),
            json.dumps(_grant_data(grant, status=EnrollmentStatus.ACTIVE)),
            keepttl=True,
        )
        await pipeline.execute()

    async def result(self, token_id: str, request_id: str) -> RegistrationResult | None:
        raw = await self._redis.get(_result_key(token_id, request_id))
        return None if raw is None else _registration_result_from_data(json.loads(raw))

    async def get_registration(self, worker_id: str) -> WorkerRegistration | None:
        raw = await self._redis.get(_registration_key(worker_id))
        return None if raw is None else _registration_from_data(json.loads(raw))

    async def save_registration(self, registration: WorkerRegistration) -> None:
        ttl_ms = max(
            1,
            round((registration.lease_expires_at - _utc_now()).total_seconds() * 2_000),
        )
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.set(
            _registration_key(registration.worker_id),
            json.dumps(_registration_data(registration)),
            px=ttl_ms,
        )
        pipeline.sadd("personaforge:control:workers", registration.worker_id)
        await pipeline.execute()

    async def get_worker_identity(self, worker_id: str) -> WorkerRegistration | None:
        raw = await self._redis.get(_identity_key(worker_id))
        return None if raw is None else _registration_from_data(json.loads(raw))

    async def save_worker_identity(self, identity: WorkerRegistration) -> None:
        await self._redis.set(
            _identity_key(identity.worker_id),
            json.dumps(_registration_data(identity)),
        )

    async def renew_registration(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
        expires_at: datetime,
    ) -> bool:
        ttl_ms = max(1, round((expires_at - _utc_now()).total_seconds() * 2_000))
        result = await _eval(
            self._redis,
            _RENEW_SCRIPT,
            1,
            _registration_key(worker_id),
            boot_id,
            owner_token_hash,
            str(expires_at.timestamp()),
            str(ttl_ms),
        )
        return bool(result)

    async def delete_registration(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
    ) -> bool:
        result = await _eval(
            self._redis,
            _DELETE_SCRIPT,
            1,
            _registration_key(worker_id),
            boot_id,
            owner_token_hash,
        )
        if result:
            await _srem(self._redis, "personaforge:control:workers", worker_id)
        return bool(result)


class RedisWorkerRegistrationRepository:
    def __init__(self, identity: RedisIdentityRepository) -> None:
        self._identity = identity

    async def get(self, worker_id: str) -> WorkerRegistration | None:
        return await self._identity.get_registration(worker_id)

    async def save(self, registration: WorkerRegistration) -> None:
        await self._identity.save_registration(registration)

    async def get_identity(self, worker_id: str) -> WorkerRegistration | None:
        return await self._identity.get_worker_identity(worker_id)

    async def save_identity(self, identity: WorkerRegistration) -> None:
        await self._identity.save_worker_identity(identity)

    async def renew(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
        expires_at: datetime,
    ) -> bool:
        return await self._identity.renew_registration(
            worker_id,
            boot_id,
            owner_token_hash,
            expires_at,
        )

    async def delete(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
    ) -> bool:
        return await self._identity.delete_registration(
            worker_id,
            boot_id,
            owner_token_hash,
        )


class RedisAclCredentialIssuer:
    """Create one Redis ACL identity per worker process incarnation."""

    def __init__(self, redis_url: str, public_redis_url: str, secrets: Any) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._public_redis_url = public_redis_url
        self._secrets = secrets

    async def startup(self) -> None:
        await self._redis.ping()

    async def shutdown(self) -> None:
        await self._redis.aclose()

    async def create(
        self,
        worker_id: str,
        boot_id: str,
        ttl: timedelta,
    ) -> RedisWorkerCredential:
        username = f"pf-avatar:{worker_id}:{boot_id}"
        password = self._secrets.generate()
        rules = (
            "reset",
            "on",
            f">{password}",
            f"~avatar:commands:{worker_id}",
            f"~avatar:events:{worker_id}:*",
            f"~avatar:worker:{worker_id}:*",
            "+ping",
            "+xreadgroup",
            "+xack",
            "+xgroup",
            "+xadd",
            "+get",
            "+set",
            "+hget",
            "+hset",
            "+pexpire",
            "+pttl",
            "+exists",
        )
        await _execute(self._redis, "ACL", "SETUSER", username, *rules)
        return RedisWorkerCredential(
            username=username,
            password=password,
            redis_url=self._public_redis_url,
            expires_at=_utc_now() + ttl,
        )

    async def revoke(self, username: str) -> None:
        await _execute(self._redis, "ACL", "DELUSER", username)


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


async def _srem(redis: Redis, key: str, member: str) -> int:
    operation = cast(Awaitable[int], redis.srem(key, member))
    return await operation


def _enrollment_key(token_id: str) -> str:
    return f"personaforge:control:enrollment:{token_id}"


def _result_key(token_id: str, request_id: str) -> str:
    return f"personaforge:control:enrollment-result:{token_id}:{request_id}"


def _registration_key(worker_id: str) -> str:
    return f"avatar:worker:{worker_id}:registration"


def _identity_key(worker_id: str) -> str:
    return f"personaforge:control:worker-identity:{worker_id}"


def _grant_data(
    grant: EnrollmentGrant,
    status: EnrollmentStatus | None = None,
) -> dict[str, object]:
    return {
        "token_id": grant.token_id,
        "subject": grant.subject,
        "cluster_id": grant.cluster_id,
        "host_id": grant.host_id,
        "worker_type": grant.worker_type,
        "max_gpu_count": grant.max_gpu_count,
        "issued_at": grant.issued_at.timestamp(),
        "expires_at": grant.expires_at.timestamp(),
        "status": (status or grant.status).value,
        "registration_request_id": grant.registration_request_id,
    }


def _grant_from_data(data: dict[str, Any]) -> EnrollmentGrant:
    return EnrollmentGrant(
        token_id=data["token_id"],
        subject=data["subject"],
        cluster_id=data["cluster_id"],
        host_id=data["host_id"],
        worker_type=data["worker_type"],
        max_gpu_count=int(data["max_gpu_count"]),
        issued_at=datetime.fromtimestamp(data["issued_at"], timezone.utc),
        expires_at=datetime.fromtimestamp(data["expires_at"], timezone.utc),
        status=EnrollmentStatus(data["status"]),
        registration_request_id=data.get("registration_request_id"),
    )


def _registration_data(value: WorkerRegistration) -> dict[str, object]:
    data = asdict(value)
    data["status"] = value.status.value
    data["registered_at"] = value.registered_at.timestamp()
    data["lease_expires_at"] = value.lease_expires_at.timestamp()
    return data


def _registration_from_data(data: dict[str, Any]) -> WorkerRegistration:
    capability_data = data["capabilities"]
    capability_data["supported_runtimes"] = tuple(
        capability_data.get("supported_runtimes", ())
    )
    capability_data["gpus"] = tuple(
        GpuCapability(**item) for item in capability_data.get("gpus", ())
    )
    capabilities = WorkerCapabilities(**capability_data)
    return WorkerRegistration(
        worker_id=data["worker_id"],
        boot_id=data["boot_id"],
        owner_token_hash=data["owner_token_hash"],
        management_token_hash=data["management_token_hash"],
        management_family_id=data["management_family_id"],
        identity_generation=int(data["identity_generation"]),
        capabilities=capabilities,
        status=WorkerStatus(data["status"]),
        registered_at=datetime.fromtimestamp(data["registered_at"], timezone.utc),
        lease_expires_at=datetime.fromtimestamp(data["lease_expires_at"], timezone.utc),
        redis_username=data["redis_username"],
    )


def _registration_result_data(value: RegistrationResult) -> dict[str, object]:
    return {
        "worker_id": value.worker_id,
        "boot_id": value.boot_id,
        "owner_token": value.owner_token,
        "management_token": value.management_token,
        "management_family_id": value.management_family_id,
        "identity_generation": value.identity_generation,
        "credential": {
            "username": value.credential.username,
            "password": value.credential.password,
            "redis_url": value.credential.redis_url,
            "expires_at": value.credential.expires_at.timestamp(),
        },
        "registration_expires_at": value.registration_expires_at.timestamp(),
    }


def _registration_result_from_data(data: dict[str, Any]) -> RegistrationResult:
    credential = data["credential"]
    return RegistrationResult(
        worker_id=data["worker_id"],
        boot_id=data["boot_id"],
        owner_token=data["owner_token"],
        management_token=data["management_token"],
        management_family_id=data["management_family_id"],
        identity_generation=int(data["identity_generation"]),
        credential=RedisWorkerCredential(
            username=credential["username"],
            password=credential["password"],
            redis_url=credential["redis_url"],
            expires_at=datetime.fromtimestamp(credential["expires_at"], timezone.utc),
        ),
        registration_expires_at=datetime.fromtimestamp(
            data["registration_expires_at"],
            timezone.utc,
        ),
    )
