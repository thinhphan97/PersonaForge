import pytest

from avatar_worker.application.composite import (
    CompositeCapacityExceeded,
    CompositeLeaseRejected,
    CompositeSlotManager,
)
from avatar_worker.domain.models import (
    CompositeReservation,
    CompositeStartRequest,
    PersonaRuntimeSpec,
    PersonaSessionState,
)

from ..support.avatar_worker_deterministic import (
    DeterministicPersonaSessionFactory,
    RecordingWorkerEvents,
)


def request(
    session_id: str,
    generation: int,
    lease_token: str,
    vram_mb: int = 4_000,
    compute: float = 0.3,
) -> CompositeStartRequest:
    return CompositeStartRequest(
        reservation=CompositeReservation(
            session_id=session_id,
            slot_id=f"slot-{session_id}",
            lease_token=lease_token,
            generation=generation,
            reserved_vram_mb=vram_mb,
            reserved_gpu_duty_cycle=compute,
        ),
        specification=PersonaRuntimeSpec(
            character_id="luna",
            avatar_model_id="recorded-luna",
            tts_model_id="scripted-tone",
            voice_id="default",
            runtime="scripted",
            resolution="640x360",
            fps=25,
        ),
        media_payload={"room_name": "session-room"},
    )


@pytest.mark.asyncio
async def test_composite_start_is_idempotent_and_owner_fenced() -> None:
    factory = DeterministicPersonaSessionFactory()
    events = RecordingWorkerEvents()
    manager = CompositeSlotManager(factory, events, 2, 10_000, 0.8)
    initial = request("session-1", 1, "lease-1")

    first = await manager.start(initial)
    repeated = await manager.start(initial)

    assert first == repeated
    assert first.state is PersonaSessionState.READY
    assert len(factory.created) == 1
    assert [event[0] for event in events.events] == ["avatar.session.ready"]

    with pytest.raises(CompositeLeaseRejected):
        await manager.stop(request("session-1", 1, "wrong").reservation)

    assert await manager.stop(initial.reservation)
    assert not await manager.stop(initial.reservation)
    assert factory.created[0][1].stopped


@pytest.mark.asyncio
async def test_new_generation_replaces_old_runtime_and_stale_commands_are_rejected() -> None:
    factory = DeterministicPersonaSessionFactory()
    events = RecordingWorkerEvents()
    manager = CompositeSlotManager(factory, events, 1, 8_000, 0.8)
    old = request("session-1", 3, "lease-old")
    new = request("session-1", 4, "lease-new")

    await manager.start(old)
    await manager.start(new)

    assert factory.created[0][1].stopped
    assert manager.snapshots()[0].generation == 4
    with pytest.raises(CompositeLeaseRejected):
        await manager.start(old)


@pytest.mark.asyncio
async def test_composite_capacity_checks_vram_compute_and_slot_count() -> None:
    manager = CompositeSlotManager(
        DeterministicPersonaSessionFactory(),
        RecordingWorkerEvents(),
        maximum_slots=2,
        schedulable_vram_mb=8_000,
        compute_budget=0.7,
    )
    await manager.start(request("session-1", 1, "lease-1", 5_000, 0.4))

    with pytest.raises(CompositeCapacityExceeded):
        await manager.start(request("session-2", 1, "lease-2", 4_000, 0.2))
    with pytest.raises(CompositeCapacityExceeded):
        await manager.start(request("session-3", 1, "lease-3", 2_000, 0.4))
