import asyncio
import logging
from contextlib import suppress

from avatar_chat.application.ports import (
    Clock,
    RealtimeEventBusPort,
    RealtimeOutboxRepository,
)

logger = logging.getLogger(__name__)


class RealtimeOutboxDispatcher:
    """Deliver committed events at least once without coupling core code to Redis."""

    def __init__(
        self,
        repository: RealtimeOutboxRepository,
        events: RealtimeEventBusPort,
        clock: Clock,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._repository = repository
        self._events = events
        self._clock = clock
        self._poll_interval_seconds = poll_interval_seconds
        self._flush_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def flush(self) -> None:
        async with self._flush_lock:
            for item in await self._repository.list_pending():
                try:
                    await self._events.publish(item.session_id, item.event)
                    await self._repository.mark_published(
                        item.event_id,
                        self._clock.now(),
                    )
                except Exception:
                    logger.exception(
                        "Realtime outbox delivery failed event=%s session=%s",
                        item.event_id,
                        item.session_id,
                    )
                    break

    async def _run(self) -> None:
        while True:
            await self.flush()
            await asyncio.sleep(self._poll_interval_seconds)
