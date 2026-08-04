import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import cast
from uuid import uuid4

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class RedisSessionTurnLock:
    """A renewable, token-owned Redis lease for serializing one session turn."""

    def __init__(
        self,
        redis_url: str,
        acquire_timeout_seconds: float = 0,
        lease_seconds: float = 30,
        retry_interval_seconds: float = 0.05,
    ) -> None:
        if lease_seconds < 3:
            raise ValueError("Redis session lock lease must be at least three seconds")
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._timeout = max(0.0, acquire_timeout_seconds)
        self._lease_ms = round(lease_seconds * 1_000)
        self._retry_interval = max(0.01, retry_interval_seconds)
        self._leases: set[RedisSessionTurnLockLease] = set()

    async def startup(self) -> None:
        await self._redis.ping()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(lease.release() for lease in tuple(self._leases)),
            return_exceptions=True,
        )
        await self._redis.aclose()

    async def acquire(self, session_id: str) -> "RedisSessionTurnLockLease | None":
        key = f"personaforge:session-turn-lock:{session_id}"
        token = str(uuid4())
        deadline = monotonic() + self._timeout
        while True:
            acquired = await self._redis.set(
                key,
                token,
                nx=True,
                px=self._lease_ms,
            )
            if acquired:
                lease = RedisSessionTurnLockLease(
                    self._redis,
                    key,
                    token,
                    self._lease_ms,
                    self._lease_released,
                )
                self._leases.add(lease)
                return lease
            if monotonic() >= deadline:
                return None
            await asyncio.sleep(
                min(self._retry_interval, max(0.0, deadline - monotonic()))
            )

    def _lease_released(self, lease: "RedisSessionTurnLockLease") -> None:
        self._leases.discard(lease)


class RedisSessionTurnLockLease:
    def __init__(
        self,
        redis: Redis,
        key: str,
        token: str,
        lease_ms: int,
        released: Callable[["RedisSessionTurnLockLease"], None],
    ) -> None:
        self._redis = redis
        self._key = key
        self._token = token
        self._lease_ms = lease_ms
        self._released_callback = released
        self._lost = asyncio.Event()
        self._released = False
        self._renewal = asyncio.create_task(
            self._renew(),
            name=f"redis-lock-renew-{key.rsplit(':', 1)[-1]}",
        )

    async def ensure_valid(self) -> None:
        if self._lost.is_set() or self._released:
            raise RuntimeError(f"Distributed session lock was lost: {self._key}")

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._renewal.cancel()
        await asyncio.gather(self._renewal, return_exceptions=True)
        try:
            await _eval_int(
                self._redis,
                _RELEASE_SCRIPT,
                1,
                self._key,
                self._token,
            )
        finally:
            self._released_callback(self)

    async def _renew(self) -> None:
        interval = self._lease_ms / 3 / 1_000
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await _eval_int(
                    self._redis,
                    _RENEW_SCRIPT,
                    1,
                    self._key,
                    self._token,
                    str(self._lease_ms),
                )
                if renewed != 1:
                    self._lost.set()
                    logger.error("Redis session lock ownership lost key=%s", self._key)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost.set()
            logger.exception("Redis session lock renewal failed key=%s", self._key)


async def _eval_int(redis: Redis, script: str, numkeys: int, *args: str) -> int:
    operation = cast(Awaitable[object], redis.eval(script, numkeys, *args))
    return int(cast(int | str, await operation))
