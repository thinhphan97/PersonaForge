"""In-memory test doubles; never imported by production composition roots."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from personaforge_control.application.ports import SecretPort
from personaforge_control.domain.errors import EnrollmentReplay, InvalidEnrollment
from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    EnrollmentGrant,
    EnrollmentStatus,
    GpuCandidate,
    GpuHeartbeat,
    RedisWorkerCredential,
    RegistrationResult,
    ResourceResolution,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerStatus,
)


class InMemoryEnrollmentRepository:
    def __init__(self) -> None:
        self._grants: dict[str, EnrollmentGrant] = {}
        self._results: dict[tuple[str, str], RegistrationResult] = {}
        self._lock = asyncio.Lock()

    async def save(self, grant: EnrollmentGrant) -> None:
        async with self._lock:
            self._grants[grant.token_id] = grant

    async def get(self, token_id: str) -> EnrollmentGrant | None:
        return self._grants.get(token_id)

    async def claim(
        self,
        token_id: str,
        request_id: str,
        now: datetime,
    ) -> EnrollmentGrant:
        async with self._lock:
            grant = self._grants.get(token_id)
            if grant is None or grant.status in {
                EnrollmentStatus.EXPIRED,
                EnrollmentStatus.REVOKED,
            }:
                raise InvalidEnrollment("Unknown or inactive enrollment token")
            if grant.expires_at <= now:
                self._grants[token_id] = replace(grant, status=EnrollmentStatus.EXPIRED)
                raise InvalidEnrollment("Enrollment token expired")
            if grant.registration_request_id is not None:
                if grant.registration_request_id != request_id:
                    raise EnrollmentReplay("Enrollment token replay detected")
                return grant
            claimed = replace(
                grant,
                status=EnrollmentStatus.CLAIMING,
                registration_request_id=request_id,
            )
            self._grants[token_id] = claimed
            return claimed

    async def activate(
        self,
        token_id: str,
        request_id: str,
        result: RegistrationResult,
    ) -> None:
        async with self._lock:
            grant = self._grants[token_id]
            if grant.registration_request_id != request_id:
                raise EnrollmentReplay("Registration request does not own the token")
            self._results[(token_id, request_id)] = result
            self._grants[token_id] = replace(grant, status=EnrollmentStatus.ACTIVE)

    async def result(self, token_id: str, request_id: str) -> RegistrationResult | None:
        return self._results.get((token_id, request_id))


class InMemoryWorkerRegistrationRepository:
    def __init__(self) -> None:
        self._registrations: dict[str, WorkerRegistration] = {}
        self._identities: dict[str, WorkerRegistration] = {}
        self._lock = asyncio.Lock()

    async def get(self, worker_id: str) -> WorkerRegistration | None:
        return self._registrations.get(worker_id)

    async def save(self, registration: WorkerRegistration) -> None:
        async with self._lock:
            self._registrations[registration.worker_id] = registration

    async def get_identity(self, worker_id: str) -> WorkerRegistration | None:
        return self._identities.get(worker_id)

    async def save_identity(self, identity: WorkerRegistration) -> None:
        async with self._lock:
            self._identities[identity.worker_id] = identity

    async def renew(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
        expires_at: datetime,
    ) -> bool:
        async with self._lock:
            current = self._registrations.get(worker_id)
            if (
                current is None
                or current.boot_id != boot_id
                or current.owner_token_hash != owner_token_hash
            ):
                return False
            self._registrations[worker_id] = replace(
                current,
                status=WorkerStatus.READY,
                lease_expires_at=expires_at,
            )
            return True

    async def delete(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
    ) -> bool:
        async with self._lock:
            current = self._registrations.get(worker_id)
            if (
                current is None
                or current.boot_id != boot_id
                or current.owner_token_hash != owner_token_hash
            ):
                return False
            del self._registrations[worker_id]
            return True

    def values(self) -> tuple[WorkerRegistration, ...]:
        return tuple(self._registrations.values())


class InMemoryWorkerCredentialIssuer:
    def __init__(self, redis_url: str, secrets: SecretPort) -> None:
        self._redis_url = redis_url
        self._secrets = secrets
        self.active: dict[str, RedisWorkerCredential] = {}

    async def create(
        self,
        worker_id: str,
        boot_id: str,
        ttl: timedelta,
    ) -> RedisWorkerCredential:
        now = datetime.now().astimezone()
        username = f"pf-avatar:{worker_id}:{boot_id}"
        credential = RedisWorkerCredential(
            username=username,
            password=self._secrets.generate(),
            redis_url=self._redis_url,
            expires_at=now + ttl,
        )
        self.active[username] = credential
        return credential

    async def revoke(self, username: str) -> None:
        self.active.pop(username, None)


class InMemoryWorkerRegistry:
    def __init__(self, registrations: InMemoryWorkerRegistrationRepository) -> None:
        self._registrations = registrations
        self._heartbeats: dict[str, WorkerHeartbeat] = {}

    async def heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        registration = await self._registrations.get(heartbeat.worker_id)
        if (
            registration is None
            or registration.boot_id != heartbeat.boot_id
            or registration.identity_generation != heartbeat.identity_generation
        ):
            raise InvalidEnrollment("Heartbeat does not own worker registration")
        self._heartbeats[heartbeat.worker_id] = heartbeat

    async def candidates(
        self,
        requirement: CompositeRequirement,
        now: datetime,
    ) -> tuple[GpuCandidate, ...]:
        candidates: list[GpuCandidate] = []
        for registration in self._registrations.values():
            heartbeat = self._heartbeats.get(registration.worker_id)
            if heartbeat is None or heartbeat.expires_at <= now:
                continue
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
            gpu_capability = capabilities.advertised_gpus()[0]
            gpu = next(
                (
                    item
                    for item in heartbeat.gpu_resources
                    if item.gpu_uuid == gpu_capability.gpu_uuid
                ),
                GpuHeartbeat(
                    gpu_uuid=gpu_capability.gpu_uuid,
                    gpu_name=gpu_capability.gpu_name,
                    compute_capability=gpu_capability.compute_capability,
                    total_vram_mb=gpu_capability.total_vram_mb,
                    free_vram_mb=heartbeat.free_vram_mb,
                    used_vram_mb=heartbeat.gpu_used_vram_mb,
                    gpu_utilization=heartbeat.gpu_utilization,
                    memory_utilization=heartbeat.gpu_memory_utilization,
                    temperature_c=heartbeat.gpu_temperature_c,
                    power_watts=heartbeat.gpu_power_watts,
                ),
            )
            candidates.append(
                GpuCandidate(
                    registration=registration,
                    heartbeat=heartbeat,
                    gpu=gpu,
                    reserved_slots=0,
                    reserved_vram_mb=0,
                    reserved_compute=0,
                    utilization_ewma=gpu.gpu_utilization,
                )
            )
        return tuple(candidates)


class InMemoryCompositeSlotAllocator:
    def __init__(self, secrets: SecretPort) -> None:
        self._secrets = secrets
        self._leases: dict[str, CompositeSlotLease] = {}
        self._generation: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        candidate: GpuCandidate,
        requirement: CompositeRequirement,
        resolution: ResourceResolution,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None:
        registration = candidate.registration
        async with self._lock:
            active = [
                lease
                for lease in self._leases.values()
                if lease.worker_id == registration.worker_id and lease.expires_at > now
                and lease.gpu_uuid == candidate.gpu.gpu_uuid
            ]
            used_vram = sum(lease.reserved_vram_mb for lease in active)
            used_compute = sum(lease.reserved_gpu_duty_cycle for lease in active)
            capabilities = registration.capabilities
            if len(active) >= capabilities.max_slots:
                return None
            if used_vram + requirement.peak_vram_mb > capabilities.schedulable_vram_mb:
                return None
            if used_compute + requirement.gpu_duty_cycle > capabilities.compute_budget:
                return None
            generation = self._generation.get(requirement.session_id, 0) + 1
            self._generation[requirement.session_id] = generation
            lease = CompositeSlotLease(
                session_id=requirement.session_id,
                worker_id=registration.worker_id,
                slot_id=f"slot-{len(active) + 1}",
                lease_token=self._secrets.generate(),
                generation=generation,
                reserved_vram_mb=requirement.peak_vram_mb,
                reserved_gpu_duty_cycle=requirement.gpu_duty_cycle,
                expires_at=now + ttl,
                gpu_uuid=candidate.gpu.gpu_uuid,
                resource_source=resolution.source,
                resource_profile_id=resolution.profile_id,
            )
            self._leases[requirement.session_id] = lease
            return lease

    async def renew(
        self,
        lease: CompositeSlotLease,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None:
        async with self._lock:
            current = self._leases.get(lease.session_id)
            if current != lease or current.expires_at <= now:
                return None
            renewed = replace(current, expires_at=now + ttl)
            self._leases[lease.session_id] = renewed
            return renewed

    async def release(self, lease: CompositeSlotLease) -> bool:
        async with self._lock:
            current = self._leases.get(lease.session_id)
            if current != lease:
                return False
            del self._leases[lease.session_id]
            return True


class InMemoryCompositeAssignmentRepository:
    def __init__(self) -> None:
        self._assignments: dict[str, CompositeSlotLease] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> CompositeSlotLease | None:
        return self._assignments.get(session_id)

    async def save(self, lease: CompositeSlotLease) -> None:
        async with self._lock:
            current = self._assignments.get(lease.session_id)
            if current is not None and current.generation > lease.generation:
                raise InvalidEnrollment("Cannot overwrite a newer composite assignment")
            self._assignments[lease.session_id] = lease

    async def delete(self, session_id: str, lease_token: str, generation: int) -> bool:
        async with self._lock:
            current = self._assignments.get(session_id)
            if current is None:
                return True
            if current.lease_token != lease_token or current.generation != generation:
                return False
            del self._assignments[session_id]
            return True


class RecordingRenderCommands:
    def __init__(self) -> None:
        self.starts: list[
            tuple[CompositeSlotLease, CompositeRequirement, dict[str, object]]
        ] = []
        self.stops: list[CompositeSlotLease] = []
        self.start_error: Exception | None = None

    async def start(
        self,
        lease: CompositeSlotLease,
        requirement: CompositeRequirement,
        payload: dict[str, object],
    ) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.starts.append((lease, requirement, payload))

    async def stop(self, lease: CompositeSlotLease) -> None:
        self.stops.append(lease)

    async def change_state(
        self,
        lease: CompositeSlotLease,
        state: str,
        state_version: int,
    ) -> None:
        del lease, state, state_version

    async def speak(
        self,
        lease: CompositeSlotLease,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None:
        del lease, turn_id, utterance_id, text
