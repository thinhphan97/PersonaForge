import asyncio
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic

from personaforge_agent.domain.models import AgentTurn


class AdmissionRejected(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    active_turns: int
    queued_turns: int
    max_concurrent_turns: int
    max_queued_turns: int
    accepted_total: int
    rejected_total: int
    completed_total: int
    failed_total: int
    cancelled_total: int
    ready: bool
    draining: bool


CapacityChanged = Callable[[bool], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Ticket:
    turn_id: str
    user_id: str
    session_id: str


class TurnAdmissionController:
    def __init__(
        self,
        max_concurrent_turns: int,
        max_queued_turns: int,
        max_concurrent_per_user: int,
        max_concurrent_per_session: int,
        queue_timeout_seconds: float,
        capacity_changed: CapacityChanged | None = None,
    ) -> None:
        if min(
            max_concurrent_turns,
            max_concurrent_per_user,
            max_concurrent_per_session,
        ) <= 0:
            raise ValueError("Agent concurrency limits must be positive")
        if max_queued_turns < 0 or queue_timeout_seconds <= 0:
            raise ValueError("Agent queue limits must be non-negative with a positive timeout")
        self._max_concurrent = max_concurrent_turns
        self._max_queued = max_queued_turns
        self._max_per_user = max_concurrent_per_user
        self._max_per_session = max_concurrent_per_session
        self._queue_timeout = queue_timeout_seconds
        self._capacity_changed = capacity_changed
        self._condition = asyncio.Condition()
        self._queue: deque[_Ticket] = deque()
        self._active_users: Counter[str] = Counter()
        self._active_sessions: Counter[str] = Counter()
        self._active = 0
        self._accepted = 0
        self._rejected = 0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._ready = True
        self._draining = False

    @asynccontextmanager
    async def slot(
        self,
        turn: AgentTurn,
        cancelled: asyncio.Event,
    ) -> AsyncIterator[None]:
        ticket = _Ticket(turn.turn_id, turn.user_id, turn.session_id)
        await self._acquire(ticket, cancelled)
        try:
            yield
        finally:
            await self._release(ticket)

    async def _acquire(self, ticket: _Ticket, cancelled: asyncio.Event) -> None:
        deadline = monotonic() + self._queue_timeout
        async with self._condition:
            if self._draining:
                self._rejected += 1
                raise AdmissionRejected("Agent Runtime is draining")
            if self._can_run(ticket) and not self._queue:
                await self._admit(ticket)
                return
            if self._max_queued == 0 or len(self._queue) >= self._max_queued:
                self._rejected += 1
                await self._update_readiness()
                raise AdmissionRejected("Agent turn queue is full")
            self._queue.append(ticket)
            await self._update_readiness()
            try:
                while True:
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    if self._draining:
                        self._rejected += 1
                        raise AdmissionRejected("Agent Runtime is draining")
                    if self._is_next_eligible(ticket):
                        self._queue.remove(ticket)
                        await self._admit(ticket)
                        return
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        self._rejected += 1
                        raise AdmissionRejected("Agent turn queue wait timed out")
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            timeout=min(remaining, 0.25),
                        )
                    except asyncio.TimeoutError:
                        continue
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()
                    await self._update_readiness()

    async def _admit(self, ticket: _Ticket) -> None:
        self._active += 1
        self._active_users[ticket.user_id] += 1
        self._active_sessions[ticket.session_id] += 1
        self._accepted += 1
        await self._update_readiness()

    async def _release(self, ticket: _Ticket) -> None:
        async with self._condition:
            self._active -= 1
            self._decrement(self._active_users, ticket.user_id)
            self._decrement(self._active_sessions, ticket.session_id)
            self._condition.notify_all()
            await self._update_readiness()

    def _can_run(self, ticket: _Ticket) -> bool:
        return (
            not self._draining
            and self._active < self._max_concurrent
            and self._active_users[ticket.user_id] < self._max_per_user
            and self._active_sessions[ticket.session_id] < self._max_per_session
        )

    def _is_next_eligible(self, ticket: _Ticket) -> bool:
        if not self._can_run(ticket):
            return False
        return next((item for item in self._queue if self._can_run(item)), None) == ticket

    @staticmethod
    def _decrement(counter: Counter[str], key: str) -> None:
        counter[key] -= 1
        if counter[key] <= 0:
            del counter[key]

    async def record_completed(self) -> None:
        async with self._condition:
            self._completed += 1

    async def record_failed(self) -> None:
        async with self._condition:
            self._failed += 1

    async def record_cancelled(self) -> None:
        async with self._condition:
            self._cancelled += 1

    async def snapshot(self) -> AdmissionSnapshot:
        async with self._condition:
            return self._snapshot()

    async def start_draining(self) -> None:
        async with self._condition:
            self._draining = True
            self._condition.notify_all()
            await self._update_readiness()

    def _snapshot(self) -> AdmissionSnapshot:
        return AdmissionSnapshot(
            active_turns=self._active,
            queued_turns=len(self._queue),
            max_concurrent_turns=self._max_concurrent,
            max_queued_turns=self._max_queued,
            accepted_total=self._accepted,
            rejected_total=self._rejected,
            completed_total=self._completed,
            failed_total=self._failed,
            cancelled_total=self._cancelled,
            ready=self._is_ready(),
            draining=self._draining,
        )

    def _is_ready(self) -> bool:
        return not self._draining and not (
            self._active >= self._max_concurrent
            and len(self._queue) >= self._max_queued
        )

    async def _update_readiness(self) -> None:
        ready = self._is_ready()
        if ready == self._ready:
            return
        self._ready = ready
        if self._capacity_changed is not None:
            await self._capacity_changed(ready)
