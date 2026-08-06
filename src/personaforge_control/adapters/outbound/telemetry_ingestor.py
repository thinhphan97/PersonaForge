import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from personaforge_control.adapters.outbound.telemetry_store import PostgresTelemetryStore

logger = logging.getLogger(__name__)


class RedisTelemetryIngestor:
    """At-least-once Redis Stream consumer with idempotent PostgreSQL writes."""

    def __init__(
        self,
        redis_url: str,
        store: PostgresTelemetryStore,
        consumer_group: str = "personaforge-control-telemetry",
        consumer_name: str = "control-1",
        batch_size: int = 100,
        poll_interval_seconds: float = 1,
        claim_idle_ms: int = 30_000,
        aggregate_interval_seconds: float = 60,
        retention_days: int = 30,
        retention_interval_seconds: float = 3600,
    ) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._store = store
        self._group = consumer_group
        self._consumer = consumer_name
        self._batch_size = batch_size
        self._poll_interval = poll_interval_seconds
        self._claim_idle_ms = claim_idle_ms
        self._aggregate_interval = aggregate_interval_seconds
        self._retention_days = retention_days
        self._retention_interval = retention_interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        await self._redis.ping()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="resource-telemetry-ingestor")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._redis.aclose()

    async def ingest_once(self) -> int:
        worker_ids = await self._redis.smembers("personaforge:control:workers")
        ingested = 0
        for worker_id in sorted(worker_ids):
            stream = f"avatar:worker:{worker_id}:telemetry"
            if not await self._redis.exists(stream):
                continue
            await self._ensure_group(stream)
            entries = await self._read_new(stream)
            if not entries:
                entries = await self._reclaim(stream)
            for message_id, fields in entries:
                raw = fields.get("data")
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else None
                    if not isinstance(payload, dict):
                        raise ValueError("Telemetry event must contain a JSON object")
                    await self._store.ingest(payload, datetime.now(timezone.utc))
                    await self._update_realtime_metrics(payload)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    # Poison data cannot become valid on retry. Preserve a bounded forensic
                    # copy and ACK so one malformed sample cannot stall the worker stream.
                    logger.exception(
                        "Dropping malformed telemetry stream=%s id=%s",
                        stream,
                        message_id,
                    )
                    await self._redis.xadd(
                        f"{stream}:dlq",
                        {"source_id": message_id, "data": raw or ""},
                        maxlen=1000,
                        approximate=True,
                    )
                except Exception:
                    logger.exception(
                        "Telemetry persistence failed stream=%s id=%s",
                        stream,
                        message_id,
                    )
                    continue
                await self._redis.xack(stream, self._group, message_id)
                ingested += 1
        return ingested

    async def status(self) -> dict[str, object]:
        worker_ids = await self._redis.smembers("personaforge:control:workers")
        streams: list[dict[str, object]] = []
        for worker_id in sorted(worker_ids):
            stream = f"avatar:worker:{worker_id}:telemetry"
            length = int(await self._redis.xlen(stream)) if await self._redis.exists(stream) else 0
            pending = 0
            if length:
                await self._ensure_group(stream)
                summary = await self._redis.xpending(stream, self._group)
                pending = int(summary.get("pending", 0))
            streams.append(
                {"worker_id": worker_id, "stream": stream, "length": length, "pending": pending}
            )
        return {"consumer_group": self._group, "consumer": self._consumer, "streams": streams}

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_aggregate = loop.time() + self._aggregate_interval
        next_retention = loop.time() + self._retention_interval
        while True:
            try:
                await self.ingest_once()
                now = loop.time()
                if now >= next_aggregate:
                    await self._store.aggregate_profiles()
                    next_aggregate = now + self._aggregate_interval
                if now >= next_retention:
                    await self._store.delete_expired_samples(self._retention_days)
                    next_retention = now + self._retention_interval
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telemetry ingestion cycle failed")
            await asyncio.sleep(self._poll_interval)

    async def _ensure_group(self, stream: str) -> None:
        try:
            await self._redis.xgroup_create(stream, self._group, id="0")
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _update_realtime_metrics(self, payload: dict[str, Any]) -> None:
        worker_id = str(payload.get("worker_id", ""))
        if not worker_id:
            return
        sampled_at = float(payload.get("hardware_sampled_at", 0))
        for gpu in payload.get("gpu_resources", ()):
            if not isinstance(gpu, dict) or not gpu.get("gpu_uuid"):
                continue
            key = f"personaforge:control:gpu:{worker_id}:{gpu['gpu_uuid']}:runtime"
            await self._redis.eval(
                """
                local previous = tonumber(redis.call('HGET', KEYS[1], 'utilization_ewma'))
                local current = tonumber(ARGV[1]) or 0
                local ewma = current
                if previous then ewma = 0.2 * current + 0.8 * previous end
                redis.call('HSET', KEYS[1], 'utilization_ewma', ewma,
                  'temperature_c', ARGV[2], 'sampled_at', ARGV[3])
                redis.call('EXPIRE', KEYS[1], 60)
                return ewma
                """,
                1,
                key,
                str(float(gpu.get("gpu_utilization", 0))),
                str(gpu.get("temperature_c") or ""),
                str(sampled_at),
            )
        if payload.get("telemetry_type") == "composite_failure":
            for child in payload.get("child_resources", ()):
                if not isinstance(child, dict):
                    continue
                gpu_uuid = str(child.get("assigned_gpu_uuid") or "")
                if not gpu_uuid:
                    continue
                key = f"personaforge:control:gpu:{worker_id}:{gpu_uuid}:runtime"
                oom = bool(child.get("oom", False))
                await self._redis.hset(
                    key,
                    mapping={
                        "failure_penalty": "1" if oom else "0.5",
                        "cooldown_until": str(sampled_at + 300 if oom else sampled_at + 30),
                    },
                )
                await self._redis.expire(key, 600)

    async def _read_new(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        result = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {stream: ">"},
            count=self._batch_size,
        )
        return _entries(result)

    async def _reclaim(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        result: Any = await self._redis.xautoclaim(
            stream,
            self._group,
            self._consumer,
            min_idle_time=self._claim_idle_ms,
            start_id="0-0",
            count=self._batch_size,
        )
        if not isinstance(result, list | tuple) or len(result) < 2:
            return []
        return list(result[1])


def _entries(result: Any) -> list[tuple[str, dict[str, str]]]:
    if not result:
        return []
    entries: list[tuple[str, dict[str, str]]] = []
    for _stream, stream_entries in result:
        entries.extend(stream_entries)
    return entries
