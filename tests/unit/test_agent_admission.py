import asyncio

import pytest

from personaforge_agent.application.admission import (
    AdmissionRejected,
    TurnAdmissionController,
)
from personaforge_agent.domain.models import AgentTurn


def turn(turn_id: str, user_id: str, session_id: str) -> AgentTurn:
    return AgentTurn(
        turn_id,
        session_id,
        user_id,
        "default",
        1,
        "hello",
        (),
        (),
        (),
    )


async def occupy(
    admission: TurnAdmissionController,
    agent_turn: AgentTurn,
    entered: asyncio.Event,
    release: asyncio.Event,
) -> None:
    async with admission.slot(agent_turn, asyncio.Event()):
        entered.set()
        await release.wait()


@pytest.mark.asyncio
async def test_admission_rejects_when_bounded_queue_is_full() -> None:
    readiness: list[bool] = []

    async def capacity_changed(ready: bool) -> None:
        readiness.append(ready)

    admission = TurnAdmissionController(1, 1, 1, 1, 1, capacity_changed)
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    first = asyncio.create_task(
        occupy(admission, turn("t1", "u1", "s1"), first_entered, first_release)
    )
    await first_entered.wait()
    second = asyncio.create_task(
        occupy(admission, turn("t2", "u2", "s2"), second_entered, second_release)
    )
    await asyncio.sleep(0)
    assert (await admission.snapshot()).queued_turns == 1

    with pytest.raises(AdmissionRejected, match="queue is full"):
        async with admission.slot(turn("t3", "u3", "s3"), asyncio.Event()):
            pass

    assert readiness[-1] is False
    first_release.set()
    await second_entered.wait()
    second_release.set()
    await asyncio.gather(first, second)
    snapshot = await admission.snapshot()
    assert snapshot.active_turns == 0
    assert snapshot.accepted_total == 2
    assert snapshot.rejected_total == 1
    assert snapshot.ready is True
    assert readiness[-1] is True


@pytest.mark.asyncio
async def test_eligible_user_bypasses_a_temporarily_blocked_queue_head() -> None:
    admission = TurnAdmissionController(2, 5, 1, 1, 1)
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    blocked_entered = asyncio.Event()
    blocked_release = asyncio.Event()
    other_entered = asyncio.Event()
    other_release = asyncio.Event()
    first = asyncio.create_task(
        occupy(admission, turn("t1", "u1", "s1"), first_entered, first_release)
    )
    await first_entered.wait()
    blocked = asyncio.create_task(
        occupy(admission, turn("t2", "u1", "s2"), blocked_entered, blocked_release)
    )
    await asyncio.sleep(0)
    assert (await admission.snapshot()).queued_turns == 1
    other = asyncio.create_task(
        occupy(admission, turn("t3", "u2", "s3"), other_entered, other_release)
    )
    await asyncio.wait_for(other_entered.wait(), timeout=1)

    snapshot = await admission.snapshot()
    assert snapshot.active_turns == 2
    assert snapshot.queued_turns == 1
    assert not blocked_entered.is_set()

    first_release.set()
    await asyncio.wait_for(blocked_entered.wait(), timeout=1)
    blocked_release.set()
    other_release.set()
    await asyncio.gather(first, blocked, other)


@pytest.mark.asyncio
async def test_queue_timeout_releases_ticket_and_counts_rejection() -> None:
    admission = TurnAdmissionController(1, 1, 1, 1, 0.01)
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    first = asyncio.create_task(
        occupy(admission, turn("t1", "u1", "s1"), first_entered, first_release)
    )
    await first_entered.wait()

    with pytest.raises(AdmissionRejected, match="wait timed out"):
        async with admission.slot(turn("t2", "u2", "s2"), asyncio.Event()):
            pass

    first_release.set()
    await first
    snapshot = await admission.snapshot()
    assert snapshot.queued_turns == 0
    assert snapshot.rejected_total == 1


@pytest.mark.asyncio
async def test_draining_rejects_new_turns_and_marks_not_ready() -> None:
    readiness: list[bool] = []

    async def capacity_changed(ready: bool) -> None:
        readiness.append(ready)

    admission = TurnAdmissionController(1, 1, 1, 1, 1, capacity_changed)
    await admission.start_draining()

    with pytest.raises(AdmissionRejected, match="draining"):
        async with admission.slot(turn("t1", "u1", "s1"), asyncio.Event()):
            pass

    snapshot = await admission.snapshot()
    assert snapshot.draining is True
    assert snapshot.ready is False
    assert snapshot.rejected_total == 1
    assert readiness == [False]
