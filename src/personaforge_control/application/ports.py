from datetime import datetime, timedelta
from typing import Protocol

from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    EnrollmentClaims,
    EnrollmentGrant,
    GpuCandidate,
    RedisWorkerCredential,
    RegistrationResult,
    ResourceResolution,
    WorkerHeartbeat,
    WorkerRegistration,
)


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class SecretPort(Protocol):
    def generate(self) -> str: ...
    def hash(self, value: str) -> str: ...


class EnrollmentTokenPort(Protocol):
    def issue(self, grant: EnrollmentGrant) -> str: ...
    def decode(self, token: str) -> EnrollmentClaims: ...


class EnrollmentRepositoryPort(Protocol):
    async def save(self, grant: EnrollmentGrant) -> None: ...
    async def get(self, token_id: str) -> EnrollmentGrant | None: ...
    async def claim(self, token_id: str, request_id: str, now: datetime) -> EnrollmentGrant: ...
    async def activate(
        self,
        token_id: str,
        request_id: str,
        result: RegistrationResult,
    ) -> None: ...
    async def result(self, token_id: str, request_id: str) -> RegistrationResult | None: ...


class WorkerRegistrationRepositoryPort(Protocol):
    async def get(self, worker_id: str) -> WorkerRegistration | None: ...
    async def save(self, registration: WorkerRegistration) -> None: ...
    async def get_identity(self, worker_id: str) -> WorkerRegistration | None: ...
    async def save_identity(self, identity: WorkerRegistration) -> None: ...
    async def renew(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
        expires_at: datetime,
    ) -> bool: ...
    async def delete(
        self,
        worker_id: str,
        boot_id: str,
        owner_token_hash: str,
    ) -> bool: ...


class WorkerCredentialIssuerPort(Protocol):
    async def create(
        self,
        worker_id: str,
        boot_id: str,
        ttl: timedelta,
    ) -> RedisWorkerCredential: ...
    async def revoke(self, username: str) -> None: ...


class WorkerRegistryPort(Protocol):
    async def heartbeat(self, heartbeat: WorkerHeartbeat) -> None: ...
    async def candidates(
        self,
        requirement: CompositeRequirement,
        now: datetime,
    ) -> tuple[GpuCandidate, ...]: ...


class WorkerInventoryPort(Protocol):
    async def inventory(
        self,
        now: datetime,
    ) -> tuple[tuple[WorkerRegistration, WorkerHeartbeat | None], ...]: ...


class CompositeSlotAllocatorPort(Protocol):
    async def acquire(
        self,
        candidate: GpuCandidate,
        requirement: CompositeRequirement,
        resolution: ResourceResolution,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None: ...
    async def renew(
        self,
        lease: CompositeSlotLease,
        now: datetime,
        ttl: timedelta,
    ) -> CompositeSlotLease | None: ...
    async def release(self, lease: CompositeSlotLease) -> bool: ...


class CompositeAssignmentRepositoryPort(Protocol):
    async def get(self, session_id: str) -> CompositeSlotLease | None: ...
    async def save(self, lease: CompositeSlotLease) -> None: ...
    async def delete(self, session_id: str, lease_token: str, generation: int) -> bool: ...


class RenderCommandPort(Protocol):
    async def start(
        self,
        lease: CompositeSlotLease,
        requirement: CompositeRequirement,
        payload: dict[str, object],
    ) -> None: ...
    async def change_state(
        self,
        lease: CompositeSlotLease,
        state: str,
        state_version: int,
    ) -> None: ...
    async def speak(
        self,
        lease: CompositeSlotLease,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None: ...
    async def stop(self, lease: CompositeSlotLease) -> None: ...


class ResourceProfileResolverPort(Protocol):
    async def resolve(
        self,
        requirement: CompositeRequirement,
        gpu_name: str,
        compute_capability: str,
    ) -> ResourceResolution: ...
