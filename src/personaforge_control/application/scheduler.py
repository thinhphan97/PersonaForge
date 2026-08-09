import logging
from datetime import timedelta

from personaforge_control.application.ports import (
    ClockPort,
    CompositeAssignmentRepositoryPort,
    CompositeSlotAllocatorPort,
    RenderCommandPort,
    ResourceProfileResolverPort,
    WorkerRegistryPort,
)
from personaforge_control.domain.errors import NoWorkerCapacity, SlotLeaseLost
from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    GpuCandidate,
    ResourceProfileState,
    ResourceResolution,
)

logger = logging.getLogger(__name__)


class CompositeScheduler:
    def __init__(
        self,
        registry: WorkerRegistryPort,
        allocator: CompositeSlotAllocatorPort,
        assignments: CompositeAssignmentRepositoryPort,
        commands: RenderCommandPort,
        clock: ClockPort,
        lease_ttl: timedelta = timedelta(seconds=30),
        resource_profiles: ResourceProfileResolverPort | None = None,
    ) -> None:
        self._registry = registry
        self._allocator = allocator
        self._assignments = assignments
        self._commands = commands
        self._clock = clock
        self._lease_ttl = lease_ttl
        self._resource_profiles = resource_profiles

    async def provision(
        self,
        requirement: CompositeRequirement,
        media_payload: dict[str, object],
        *,
        excluded_worker_id: str | None = None,
        excluded_gpu_uuid: str | None = None,
    ) -> CompositeSlotLease:
        current = await self._assignments.get(requirement.session_id)
        now = self._clock.now()
        if current is not None and current.expires_at > now:
            # Provision is also a repair signal. Re-emitting the fenced start command is safe:
            # an existing Child treats the same lease/generation as idempotent, while a freshly
            # restarted Worker can recreate the missing Child and LiveKit publisher.
            await self._commands.start(current, requirement, media_payload)
            logger.info(
                "Existing composite assignment was reprovisioned",
                extra={
                    "event": "control.assignment.reprovisioned",
                    "session_id": requirement.session_id,
                    "worker_id": current.worker_id,
                    "gpu_uuid": current.gpu_uuid,
                    "outcome": "success",
                },
            )
            return current

        candidates = await self._registry.candidates(requirement, now)
        evaluated = []
        for candidate in candidates:
            if (
                excluded_worker_id is not None
                and candidate.registration.worker_id == excluded_worker_id
            ):
                continue
            if excluded_gpu_uuid is not None and candidate.gpu.gpu_uuid == excluded_gpu_uuid:
                continue
            resolution = await self._resolve_resources(requirement, candidate)
            normalized = _with_resolution(requirement, resolution)
            capability = next(
                item
                for item in candidate.registration.capabilities.advertised_gpus()
                if item.gpu_uuid == candidate.gpu.gpu_uuid
            )
            if _compute_capability(capability.compute_capability) < _compute_capability(
                normalized.minimum_compute_capability
            ):
                continue
            if candidate.reserved_slots + 1 > capability.max_slots:
                continue
            if (
                candidate.reserved_vram_mb + normalized.peak_vram_mb
                > capability.schedulable_vram_mb
            ):
                continue
            if candidate.reserved_compute + normalized.gpu_duty_cycle > capability.compute_budget:
                continue
            if candidate.gpu.free_vram_mb < normalized.peak_vram_mb:
                continue
            evaluated.append((candidate, resolution, normalized))
        ordered = sorted(
            evaluated,
            key=lambda item: (
                -_candidate_score(item[0], item[2]),
                item[0].registration.worker_id,
                item[0].gpu.gpu_uuid,
            ),
        )
        for candidate, resolution, normalized in ordered[:8]:
            lease = await self._allocator.acquire(
                candidate,
                normalized,
                resolution,
                now,
                self._lease_ttl,
            )
            if lease is None:
                continue
            try:
                await self._assignments.save(lease)
                await self._commands.start(lease, normalized, media_payload)
            except BaseException:
                await self._assignments.delete(
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                )
                await self._allocator.release(lease)
                raise
            logger.info(
                "Composite assignment allocated",
                extra={
                    "event": "control.assignment.allocated",
                    "session_id": requirement.session_id,
                    "worker_id": lease.worker_id,
                    "gpu_uuid": lease.gpu_uuid,
                    "outcome": "success",
                },
            )
            return lease
        logger.warning(
            "No worker has enough capacity for the composite session",
            extra={
                "event": "control.assignment.rejected",
                "session_id": requirement.session_id,
                "reason": "no_worker_capacity",
                "outcome": "rejected",
            },
        )
        raise NoWorkerCapacity("No healthy worker can reserve the composite requirement")

    async def recover(
        self,
        requirement: CompositeRequirement,
        media_payload: dict[str, object],
        failed_worker_id: str,
        failed_gpu_uuid: str,
    ) -> bool:
        """Migrate a lost runtime to a different worker/GPU.

        The old lease is fenced and released before allocating the replacement.
        The caller persists the recovery payload and retries when no capacity is
        available.
        """
        current = await self._assignments.get(requirement.session_id)
        if current is not None:
            if (
                (failed_worker_id and current.worker_id != failed_worker_id)
                or current.gpu_uuid != failed_gpu_uuid
            ):
                # Another recovery already replaced the failed assignment.
                return True
            await self._assignments.delete(
                current.session_id,
                current.lease_token,
                current.generation,
            )
            await self._allocator.release(current)
        try:
            await self.provision(
                requirement,
                media_payload,
                excluded_worker_id=failed_worker_id,
                excluded_gpu_uuid=failed_gpu_uuid,
            )
        except NoWorkerCapacity:
            return False
        logger.info(
            "Composite session migrated after worker failure",
            extra={
                "event": "control.session.recovery.migrated",
                "session_id": requirement.session_id,
                "failed_worker_id": failed_worker_id,
                "failed_gpu_uuid": failed_gpu_uuid,
                "outcome": "success",
            },
        )
        return True

    async def _resolve_resources(
        self,
        requirement: CompositeRequirement,
        candidate: GpuCandidate,
    ) -> ResourceResolution:
        if self._resource_profiles is not None:
            return await self._resource_profiles.resolve(
                requirement,
                candidate.gpu.gpu_name,
                candidate.gpu.compute_capability,
            )
        return ResourceResolution(
            peak_vram_mb=requirement.peak_vram_mb,
            gpu_duty_cycle=requirement.gpu_duty_cycle,
            source="request",
            profile_state=ResourceProfileState.BOOTSTRAP,
        )

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


def _candidate_score(candidate: GpuCandidate, requirement: CompositeRequirement) -> float:
    gpu = candidate.gpu
    capability = next(
        item
        for item in candidate.registration.capabilities.advertised_gpus()
        if item.gpu_uuid == gpu.gpu_uuid
    )
    vram_after = max(
        0,
        capability.schedulable_vram_mb
        - candidate.reserved_vram_mb
        - requirement.peak_vram_mb,
    )
    vram_headroom = vram_after / max(1, capability.schedulable_vram_mb)
    compute_after = max(
        0.0,
        capability.compute_budget
        - candidate.reserved_compute
        - requirement.gpu_duty_cycle,
    )
    compute_headroom = compute_after / max(0.01, capability.compute_budget)
    queue_headroom = 1 / (1 + candidate.heartbeat.queue_depth)
    runtime_headroom = max(0.0, 1 - candidate.utilization_ewma / 100)
    thermal_headroom = (
        0.5 if gpu.temperature_c is None else max(0.0, min(1.0, (90 - gpu.temperature_c) / 40))
    )
    reliability = max(0.0, 1 - candidate.recent_failure_penalty)
    # Cache affinity and region locality remain zero until the Worker advertises trusted
    # model-residency and region data. Capacity/reliability terms are already versionable at
    # this boundary and deterministic tie-breaking is applied by the caller.
    return (
        0.20 * vram_headroom
        + 0.15 * compute_headroom
        + 0.10 * queue_headroom
        + 0.10 * reliability
        + 0.10 * runtime_headroom
        + 0.05 * thermal_headroom
        - 0.15 * candidate.recent_failure_penalty
    )


def _with_resolution(
    requirement: CompositeRequirement,
    resolution: ResourceResolution,
) -> CompositeRequirement:
    return CompositeRequirement(
        session_id=requirement.session_id,
        character_id=requirement.character_id,
        avatar_model_id=requirement.avatar_model_id,
        tts_model_id=requirement.tts_model_id,
        runtime=requirement.runtime,
        minimum_compute_capability=_maximum_compute_capability(
            requirement.minimum_compute_capability,
            resolution.minimum_compute_capability,
        ),
        resolution=requirement.resolution,
        fps=requirement.fps,
        peak_vram_mb=resolution.peak_vram_mb,
        gpu_duty_cycle=resolution.gpu_duty_cycle,
        voice_id=requirement.voice_id,
        avatar_revision=requirement.avatar_revision,
        tts_revision=requirement.tts_revision,
        runtime_version=requirement.runtime_version,
        precision=requirement.precision,
        isolation_mode=requirement.isolation_mode,
    )


def _maximum_compute_capability(first: str, second: str) -> str:
    return first if _compute_capability(first) >= _compute_capability(second) else second


def _compute_capability(value: str) -> tuple[int, int]:
    major, _, minor = value.partition(".")
    try:
        return int(major), int(minor or 0)
    except ValueError:
        return 0, 0
