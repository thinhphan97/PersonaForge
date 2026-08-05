import asyncio
from dataclasses import dataclass

from personaforge_worker.application.ports import (
    CompositeSessionFactoryPort,
    CompositeSessionRuntimePort,
    WorkerEventPort,
)
from personaforge_worker.domain.models import (
    CompositeReservation,
    CompositeSessionSnapshot,
    CompositeStartRequest,
    PersonaSessionState,
)


class CompositeLeaseRejected(RuntimeError):
    pass


class CompositeCapacityExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _ActiveSession:
    request: CompositeStartRequest
    runtime: CompositeSessionRuntimePort
    state: PersonaSessionState


class CompositeSlotManager:
    """Own local persona slots and fence every mutation by generation and lease token."""

    def __init__(
        self,
        factory: CompositeSessionFactoryPort,
        events: WorkerEventPort,
        maximum_slots: int,
        schedulable_vram_mb: int,
        compute_budget: float,
    ) -> None:
        if maximum_slots < 1 or schedulable_vram_mb < 0:
            raise ValueError("Invalid composite worker capacity")
        if compute_budget <= 0 or compute_budget > 1:
            raise ValueError("Compute budget must be in (0, 1]")
        self._factory = factory
        self._events = events
        self._maximum_slots = maximum_slots
        self._schedulable_vram_mb = schedulable_vram_mb
        self._compute_budget = compute_budget
        self._sessions: dict[str, _ActiveSession] = {}
        self._highest_generation: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def start(self, request: CompositeStartRequest) -> CompositeSessionSnapshot:
        reservation = request.reservation
        previous: _ActiveSession | None = None
        async with self._lock:
            current = self._sessions.get(reservation.session_id)
            highest = self._highest_generation.get(reservation.session_id, 0)
            if current is not None and _same_owner(current.request.reservation, reservation):
                return _snapshot(current)
            if reservation.generation <= highest:
                raise CompositeLeaseRejected("Stale composite slot generation")
            if current is not None:
                previous = self._sessions.pop(reservation.session_id)
            self._assert_capacity(reservation)
            runtime = await self._factory.create(request)
            active = _ActiveSession(request, runtime, PersonaSessionState.LOADING)
            self._sessions[reservation.session_id] = active
            self._highest_generation[reservation.session_id] = reservation.generation

        if previous is not None:
            await previous.runtime.stop()
        try:
            await runtime.start()
            await runtime.wait_ready()
        except BaseException as exc:
            async with self._lock:
                if self._sessions.get(reservation.session_id) is active:
                    self._sessions.pop(reservation.session_id, None)
                active.state = PersonaSessionState.FAILED
            await runtime.stop()
            await self._events.publish(
                "avatar.session.failed",
                reservation.session_id,
                reservation.generation,
                PersonaSessionState.FAILED,
                str(exc),
            )
            raise

        async with self._lock:
            if self._sessions.get(reservation.session_id) is not active:
                await runtime.stop()
                raise CompositeLeaseRejected("Composite session ownership changed while loading")
            active.state = PersonaSessionState.READY
        await self._events.publish(
            "avatar.session.ready",
            reservation.session_id,
            reservation.generation,
            PersonaSessionState.READY,
        )
        return _snapshot(active)

    async def stop(self, reservation: CompositeReservation) -> bool:
        async with self._lock:
            current = self._sessions.get(reservation.session_id)
            if current is None:
                highest = self._highest_generation.get(reservation.session_id, 0)
                if reservation.generation < highest:
                    raise CompositeLeaseRejected("Stale composite stop generation")
                return False
            if not _same_owner(current.request.reservation, reservation):
                raise CompositeLeaseRejected("Composite stop does not own the active slot")
            current.state = PersonaSessionState.DRAINING
            del self._sessions[reservation.session_id]
        await current.runtime.stop()
        current.state = PersonaSessionState.STOPPED
        await self._events.publish(
            "avatar.session.stopped",
            reservation.session_id,
            reservation.generation,
            PersonaSessionState.STOPPED,
        )
        return True

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(session.runtime.stop() for session in sessions))

    def snapshots(self) -> tuple[CompositeSessionSnapshot, ...]:
        return tuple(_snapshot(session) for session in self._sessions.values())

    def _assert_capacity(self, reservation: CompositeReservation) -> None:
        active = tuple(self._sessions.values())
        if len(active) >= self._maximum_slots:
            raise CompositeCapacityExceeded("No local composite slot available")
        reserved_vram = sum(
            item.request.reservation.reserved_vram_mb for item in active
        )
        reserved_compute = sum(
            item.request.reservation.reserved_gpu_duty_cycle for item in active
        )
        if reserved_vram + reservation.reserved_vram_mb > self._schedulable_vram_mb:
            raise CompositeCapacityExceeded("Local VRAM reservation exceeded")
        if reserved_compute + reservation.reserved_gpu_duty_cycle > self._compute_budget:
            raise CompositeCapacityExceeded("Local compute reservation exceeded")


def _same_owner(first: CompositeReservation, second: CompositeReservation) -> bool:
    return (
        first.session_id == second.session_id
        and first.generation == second.generation
        and first.lease_token == second.lease_token
        and first.slot_id == second.slot_id
    )


def _snapshot(session: _ActiveSession) -> CompositeSessionSnapshot:
    reservation = session.request.reservation
    return CompositeSessionSnapshot(
        session_id=reservation.session_id,
        slot_id=reservation.slot_id,
        generation=reservation.generation,
        state=session.state,
        reserved_vram_mb=reservation.reserved_vram_mb,
        reserved_gpu_duty_cycle=reservation.reserved_gpu_duty_cycle,
    )
