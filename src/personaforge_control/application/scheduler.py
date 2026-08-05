from datetime import timedelta

from personaforge_control.application.ports import (
    ClockPort,
    CompositeAssignmentRepositoryPort,
    CompositeSlotAllocatorPort,
    RenderCommandPort,
    WorkerRegistryPort,
)
from personaforge_control.domain.errors import NoWorkerCapacity, SlotLeaseLost
from personaforge_control.domain.models import CompositeRequirement, CompositeSlotLease


class CompositeScheduler:
    def __init__(
        self,
        registry: WorkerRegistryPort,
        allocator: CompositeSlotAllocatorPort,
        assignments: CompositeAssignmentRepositoryPort,
        commands: RenderCommandPort,
        clock: ClockPort,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._registry = registry
        self._allocator = allocator
        self._assignments = assignments
        self._commands = commands
        self._clock = clock
        self._lease_ttl = lease_ttl

    async def provision(
        self,
        requirement: CompositeRequirement,
        media_payload: dict[str, object],
    ) -> CompositeSlotLease:
        current = await self._assignments.get(requirement.session_id)
        now = self._clock.now()
        if current is not None and current.expires_at > now:
            return current

        candidates = await self._registry.candidates(requirement, now)
        ordered = sorted(
            candidates,
            key=lambda item: (
                item[1].queue_depth,
                item[1].reserved_compute,
                -item[1].free_vram_mb,
                item[0].worker_id,
            ),
        )
        for registration, heartbeat in ordered:
            lease = await self._allocator.acquire(
                registration,
                heartbeat,
                requirement,
                now,
                self._lease_ttl,
            )
            if lease is None:
                continue
            try:
                await self._assignments.save(lease)
                await self._commands.start(lease, requirement, media_payload)
            except BaseException:
                await self._assignments.delete(
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                )
                await self._allocator.release(lease)
                raise
            return lease
        raise NoWorkerCapacity("No healthy worker can reserve the composite requirement")

    async def renew(self, session_id: str) -> CompositeSlotLease:
        lease = await self._assignments.get(session_id)
        if lease is None:
            raise SlotLeaseLost("Composite assignment not found")
        renewed = await self._allocator.renew(
            lease,
            self._clock.now(),
            self._lease_ttl,
        )
        if renewed is None:
            await self._assignments.delete(
                lease.session_id,
                lease.lease_token,
                lease.generation,
            )
            raise SlotLeaseLost("Composite slot lease ownership changed or expired")
        await self._assignments.save(renewed)
        return renewed

    async def change_state(
        self,
        session_id: str,
        state: str,
        state_version: int,
    ) -> CompositeSlotLease:
        lease = await self.renew(session_id)
        await self._commands.change_state(lease, state, state_version)
        return lease

    async def speak(
        self,
        session_id: str,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> CompositeSlotLease:
        lease = await self.renew(session_id)
        await self._commands.speak(lease, turn_id, utterance_id, text)
        return lease

    async def release(self, session_id: str) -> bool:
        lease = await self._assignments.get(session_id)
        if lease is None:
            return False
        await self._commands.stop(lease)
        deleted = await self._assignments.delete(
            lease.session_id,
            lease.lease_token,
            lease.generation,
        )
        released = await self._allocator.release(lease)
        if not deleted or not released:
            raise SlotLeaseLost("Composite slot release lost lease ownership")
        return True
