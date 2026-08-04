import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import uuid4

from redis.asyncio import Redis

from personaforge_agent.application.ports import TurnClaimPort

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

_COMPLETE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('del', KEYS[1])
  redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""

_CLAIM_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 or redis.call('exists', KEYS[2]) == 1 then
  return 0
end
redis.call('psetex', KEYS[1], ARGV[2], ARGV[1])
return 1
"""


class RedisTurnRegistry:
    def __init__(
        self,
        redis_url: str,
        lease_seconds: float = 60,
        result_retention_seconds: int = 86_400,
    ) -> None:
        if lease_seconds < 3 or result_retention_seconds <= 0:
            raise ValueError("Invalid Agent turn registry lease or retention")
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._lease_ms = round(lease_seconds * 1_000)
        self._retention = result_retention_seconds
        self._claims: set[RedisTurnClaim] = set()

    async def startup(self) -> None:
        await self._redis.ping()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(claim.release() for claim in tuple(self._claims)),
            return_exceptions=True,
        )
        await self._redis.aclose()

    async def claim(self, turn_id: str) -> TurnClaimPort | None:
        result_key = f"personaforge:agent-turn:result:{turn_id}"
        claim_key = f"personaforge:agent-turn:claim:{turn_id}"
        token = str(uuid4())
        claimed = await _eval_int(
            self._redis,
            _CLAIM_SCRIPT,
            2,
            claim_key,
            result_key,
            token,
            str(self._lease_ms),
        )
        if claimed != 1:
            return None
        claim = RedisTurnClaim(
            self._redis,
            claim_key,
            result_key,
            token,
            self._lease_ms,
            self._retention,
            self._claim_released,
        )
        self._claims.add(claim)
        return claim

    def _claim_released(self, claim: "RedisTurnClaim") -> None:
        self._claims.discard(claim)


class RedisTurnClaim:
    def __init__(
        self,
        redis: Redis,
        claim_key: str,
        result_key: str,
        token: str,
        lease_ms: int,
        retention: int,
        released: Callable[["RedisTurnClaim"], None],
    ) -> None:
        self._redis = redis
        self._claim_key = claim_key
        self._result_key = result_key
        self._token = token
        self._lease_ms = lease_ms
        self._retention = retention
        self._released_callback = released
        self._lost = asyncio.Event()
        self._closed = False
        self._renewal = asyncio.create_task(
            self._renew(),
            name=f"agent-turn-claim-{claim_key.rsplit(':', 1)[-1]}",
        )

    async def ensure_valid(self) -> None:
        if self._closed or self._lost.is_set():
            raise RuntimeError(f"Distributed Agent turn claim lost: {self._claim_key}")

    async def complete(self, outcome: str) -> None:
        if self._closed:
            return
        await self._stop_renewal()
        try:
            completed = await _eval_int(
                self._redis,
                _COMPLETE_SCRIPT,
                2,
                self._claim_key,
                self._result_key,
                self._token,
                outcome,
                str(self._retention),
            )
            if completed != 1:
                self._lost.set()
                raise RuntimeError(
                    f"Could not complete lost Agent turn claim: {self._claim_key}"
                )
        finally:
            self._close()

    async def release(self) -> None:
        if self._closed:
            return
        await self._stop_renewal()
        try:
            await _eval_int(
                self._redis,
                _RELEASE_SCRIPT,
                1,
                self._claim_key,
                self._token,
            )
        finally:
            self._close()

    async def _stop_renewal(self) -> None:
        self._renewal.cancel()
        await asyncio.gather(self._renewal, return_exceptions=True)

    def _close(self) -> None:
        self._closed = True
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
                    self._claim_key,
                    self._token,
                    str(self._lease_ms),
                )
                if renewed != 1:
                    self._lost.set()
                    logger.error("Redis Agent turn claim lost key=%s", self._claim_key)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lost.set()
            logger.exception("Redis Agent turn claim renewal failed key=%s", self._claim_key)


class InMemoryTurnRegistry:
    def __init__(self) -> None:
        self._active: set[str] = set()
        self._completed: set[str] = set()
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def claim(self, turn_id: str) -> TurnClaimPort | None:
        async with self._lock:
            if turn_id in self._active or turn_id in self._completed:
                return None
            self._active.add(turn_id)
        return InMemoryTurnClaim(turn_id, self)

    async def finish(self, turn_id: str, terminal: bool) -> None:
        async with self._lock:
            self._active.discard(turn_id)
            if terminal:
                self._completed.add(turn_id)


class InMemoryTurnClaim:
    def __init__(self, turn_id: str, registry: InMemoryTurnRegistry) -> None:
        self._turn_id = turn_id
        self._registry = registry
        self._closed = False

    async def ensure_valid(self) -> None:
        if self._closed:
            raise RuntimeError(f"Agent turn claim is closed: {self._turn_id}")

    async def complete(self, outcome: str) -> None:
        del outcome
        if not self._closed:
            self._closed = True
            await self._registry.finish(self._turn_id, terminal=True)

    async def release(self) -> None:
        if not self._closed:
            self._closed = True
            await self._registry.finish(self._turn_id, terminal=False)


async def _eval_int(redis: Redis, script: str, numkeys: int, *args: str) -> int:
    operation = cast(Awaitable[object], redis.eval(script, numkeys, *args))
    return int(cast(int | str, await operation))
