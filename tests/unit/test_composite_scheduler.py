from datetime import datetime, timedelta, timezone

import pytest

from personaforge_control.application.scheduler import CompositeScheduler
from personaforge_control.domain.errors import NoWorkerCapacity
from personaforge_control.domain.models import (
    CompositeRequirement,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerStatus,
)

from ..support.persona_control_memory import (
    InMemoryCompositeAssignmentRepository,
    InMemoryCompositeSlotAllocator,
    InMemoryWorkerRegistrationRepository,
    InMemoryWorkerRegistry,
    RecordingRenderCommands,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class DeterministicSecrets:
    def __init__(self) -> None:
        self.counter = 0

    def generate(self) -> str:
        self.counter += 1
        return f"lease-{self.counter}"

    def hash(self, value: str) -> str:
        return value


def requirement(session_id: str = "session-1") -> CompositeRequirement:
    return CompositeRequirement(
        session_id=session_id,
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


async def scheduler_fixture() -> tuple[
    CompositeScheduler,
    InMemoryWorkerRegistry,
    InMemoryCompositeAssignmentRepository,
    InMemoryCompositeSlotAllocator,
    RecordingRenderCommands,
    FixedClock,
]:
    clock = FixedClock()
    registrations = InMemoryWorkerRegistrationRepository()
    registration = WorkerRegistration(
        worker_id="worker-1",
        boot_id="boot-1",
        owner_token_hash="owner-hash",
        management_token_hash="management-hash",
        management_family_id="family-1",
        identity_generation=1,
        capabilities=WorkerCapabilities(
            worker_type="persona-render",
            host_id="host-1",
            gpu_uuid="GPU-1",
            gpu_name="Fake GPU",
            compute_capability="8.0",
            total_vram_mb=12_000,
            schedulable_vram_mb=10_000,
            max_slots=2,
            compute_budget=0.8,
            supported_runtimes=("scripted",),
        ),
        status=WorkerStatus.READY,
        registered_at=clock.now(),
        lease_expires_at=clock.now() + timedelta(seconds=15),
        redis_username="worker-user",
    )
    await registrations.save(registration)
    registry = InMemoryWorkerRegistry(registrations)
    await registry.heartbeat(
        WorkerHeartbeat(
            worker_id="worker-1",
            boot_id="boot-1",
            identity_generation=1,
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
    allocator = InMemoryCompositeSlotAllocator(DeterministicSecrets())
    assignments = InMemoryCompositeAssignmentRepository()
    commands = RecordingRenderCommands()
    return (
        CompositeScheduler(registry, allocator, assignments, commands, clock),
        registry,
        assignments,
        allocator,
        commands,
        clock,
    )


@pytest.mark.asyncio
async def test_scheduler_provisions_idempotently_and_releases_owner_lease() -> None:
    scheduler, _registry, assignments, _allocator, commands, _clock = (
        await scheduler_fixture()
    )

    first = await scheduler.provision(requirement(), {"room_name": "room-1"})
    repeated = await scheduler.provision(requirement(), {"room_name": "ignored"})

    assert repeated == first
    assert first.worker_id == "worker-1"
    assert first.generation == 1
    assert len(commands.starts) == 1
    assert await scheduler.release(first.session_id)
    assert await assignments.get(first.session_id) is None
    assert commands.stops == [first]


@pytest.mark.asyncio
async def test_scheduler_excludes_expired_heartbeat() -> None:
    scheduler, _registry, _assignments, _allocator, _commands, clock = (
        await scheduler_fixture()
    )
    clock.value += timedelta(seconds=20)

    with pytest.raises(NoWorkerCapacity):
        await scheduler.provision(requirement(), {})


@pytest.mark.asyncio
async def test_scheduler_rolls_back_assignment_when_command_delivery_fails() -> None:
    scheduler, _registry, assignments, allocator, commands, _clock = (
        await scheduler_fixture()
    )
    commands.start_error = RuntimeError("command bus unavailable")

    with pytest.raises(RuntimeError, match="command bus unavailable"):
        await scheduler.provision(requirement(), {})

    assert await assignments.get("session-1") is None
    commands.start_error = None
    recovered = await scheduler.provision(requirement(), {})
    assert recovered.generation == 2
    assert await allocator.release(recovered)
