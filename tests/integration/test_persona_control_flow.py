from datetime import datetime, timedelta, timezone

import pytest

from avatar_worker.application.composite import CompositeSlotManager
from avatar_worker.domain.models import (
    CompositeReservation,
    CompositeStartRequest,
    PersonaRuntimeSpec,
)
from personaforge_control.application.identity import (
    EnrollmentService,
    WorkerRegistrationService,
)
from personaforge_control.application.scheduler import CompositeScheduler
from personaforge_control.domain.errors import InvalidEnrollment
from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    EnrollmentClaims,
    EnrollmentGrant,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerStatus,
)

from ..support.avatar_worker_deterministic import (
    DeterministicPersonaSessionFactory,
    RecordingWorkerEvents,
)
from ..support.persona_control_memory import (
    InMemoryCompositeAssignmentRepository,
    InMemoryCompositeSlotAllocator,
    InMemoryEnrollmentRepository,
    InMemoryWorkerCredentialIssuer,
    InMemoryWorkerRegistrationRepository,
    InMemoryWorkerRegistry,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class Secrets:
    def __init__(self) -> None:
        self.count = 0

    def generate(self) -> str:
        self.count += 1
        return f"secret-{self.count}"

    def hash(self, value: str) -> str:
        return f"hash:{value}"


class EnrollmentTokens:
    def __init__(self) -> None:
        self.grants: dict[str, EnrollmentGrant] = {}

    def issue(self, grant: EnrollmentGrant) -> str:
        token = f"bootstrap:{grant.token_id}"
        self.grants[token] = grant
        return token

    def decode(self, token: str) -> EnrollmentClaims:
        grant = self.grants.get(token)
        if grant is None:
            raise InvalidEnrollment("Invalid token")
        return EnrollmentClaims(
            grant.subject,
            grant.token_id,
            grant.cluster_id,
            grant.host_id,
            grant.worker_type,
            grant.max_gpu_count,
            grant.expires_at,
        )


class DirectWorkerCommands:
    def __init__(self, worker: CompositeSlotManager) -> None:
        self.worker = worker

    async def start(
        self,
        lease: CompositeSlotLease,
        requirement: CompositeRequirement,
        payload: dict[str, object],
    ) -> None:
        await self.worker.start(
            CompositeStartRequest(
                reservation=CompositeReservation(
                    session_id=lease.session_id,
                    slot_id=lease.slot_id,
                    lease_token=lease.lease_token,
                    generation=lease.generation,
                    reserved_vram_mb=lease.reserved_vram_mb,
                    reserved_gpu_duty_cycle=lease.reserved_gpu_duty_cycle,
                ),
                specification=PersonaRuntimeSpec(
                    character_id=requirement.character_id,
                    avatar_model_id=requirement.avatar_model_id,
                    tts_model_id=requirement.tts_model_id,
                    voice_id="voice-default",
                    runtime=requirement.runtime,
                    resolution=requirement.resolution,
                    fps=requirement.fps,
                ),
                media_payload={key: str(value) for key, value in payload.items()},
            )
        )

    async def stop(self, lease: CompositeSlotLease) -> None:
        await self.worker.stop(
            CompositeReservation(
                session_id=lease.session_id,
                slot_id=lease.slot_id,
                lease_token=lease.lease_token,
                generation=lease.generation,
                reserved_vram_mb=lease.reserved_vram_mb,
                reserved_gpu_duty_cycle=lease.reserved_gpu_duty_cycle,
            )
        )

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


@pytest.mark.asyncio
async def test_enrollment_to_composite_ready_and_release_flow() -> None:
    clock = FixedClock()
    secrets = Secrets()
    tokens = EnrollmentTokens()
    enrollments = InMemoryEnrollmentRepository()
    registrations = InMemoryWorkerRegistrationRepository()
    credentials = InMemoryWorkerCredentialIssuer("redis://redis:6379/0", secrets)
    enrollment = EnrollmentService(enrollments, tokens, secrets, clock)
    registration = WorkerRegistrationService(
        enrollments,
        registrations,
        credentials,
        tokens,
        secrets,
        clock,
    )
    bootstrap_token = await enrollment.issue(
        "deployment-controller",
        "development",
        "gpu-host-a",
        "persona-render",
        1,
        timedelta(minutes=10),
    )
    capabilities = WorkerCapabilities(
        worker_type="persona-render",
        host_id="gpu-host-a",
        gpu_uuid="GPU-test",
        gpu_name="Fake GPU",
        compute_capability="8.0",
        total_vram_mb=12_000,
        schedulable_vram_mb=10_000,
        max_slots=2,
        compute_budget=0.8,
        supported_runtimes=("scripted",),
    )
    identity = await registration.register(
        bootstrap_token,
        "registration-1",
        "gpu-host-a:GPU-test:persona-1",
        "boot-1",
        capabilities,
    )
    await registration.renew(identity.worker_id, identity.boot_id, identity.owner_token)

    registry = InMemoryWorkerRegistry(registrations)
    await registry.heartbeat(
        WorkerHeartbeat(
            worker_id=identity.worker_id,
            boot_id=identity.boot_id,
            identity_generation=identity.identity_generation,
            status=WorkerStatus.READY,
            draining=False,
            active_slots=0,
            reserved_slots=0,
            reserved_vram_mb=0,
            reserved_compute=0,
            free_vram_mb=10_000,
            queue_depth=0,
            observed_fps=25,
            gpu_used_vram_mb=2_000,
            gpu_utilization=10,
            gpu_memory_utilization=15,
            gpu_temperature_c=55,
            gpu_power_watts=80,
            hardware_source="test",
            hardware_sampled_at=clock.now(),
            hardware_probe_error=None,
            heartbeat_at=clock.now(),
            expires_at=clock.now() + timedelta(seconds=15),
        )
    )
    runtime_factory = DeterministicPersonaSessionFactory()
    worker_events = RecordingWorkerEvents()
    worker = CompositeSlotManager(runtime_factory, worker_events, 2, 10_000, 0.8)
    assignments = InMemoryCompositeAssignmentRepository()
    scheduler = CompositeScheduler(
        registry,
        InMemoryCompositeSlotAllocator(secrets),
        assignments,
        DirectWorkerCommands(worker),
        clock,
    )
    requirement = CompositeRequirement(
        session_id="session-1",
        character_id="luna",
        avatar_model_id="recorded-luna",
        tts_model_id="scripted-tone",
        runtime="scripted",
        minimum_compute_capability="8.0",
        resolution="640x360",
        fps=25,
        peak_vram_mb=4_000,
        gpu_duty_cycle=0.3,
    )

    lease = await scheduler.provision(requirement, {"room_name": "room-1"})

    assert lease.worker_id == identity.worker_id
    assert worker.snapshots()[0].generation == lease.generation
    assert worker_events.events[-1][0] == "avatar.session.ready"
    assert await scheduler.release("session-1")
    assert not worker.snapshots()
    assert worker_events.events[-1][0] == "avatar.session.stopped"
    assert await assignments.get("session-1") is None
