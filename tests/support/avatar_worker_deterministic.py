"""Deterministic Composite Worker test doubles."""

import asyncio

from avatar_worker.domain.models import CompositeStartRequest, PersonaSessionState


class DeterministicPersonaSession:
    def __init__(self) -> None:
        self.started = False
        self.ready = asyncio.Event()
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self.ready.set()

    async def wait_ready(self) -> None:
        await self.ready.wait()

    async def stop(self) -> None:
        self.stopped = True


class DeterministicPersonaSessionFactory:
    def __init__(self) -> None:
        self.created: list[tuple[CompositeStartRequest, DeterministicPersonaSession]] = []

    async def create(self, request: CompositeStartRequest) -> DeterministicPersonaSession:
        session = DeterministicPersonaSession()
        self.created.append((request, session))
        return session


class RecordingWorkerEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, int, PersonaSessionState, str | None]] = []

    async def publish(
        self,
        event_type: str,
        session_id: str,
        generation: int,
        state: PersonaSessionState,
        detail: str | None = None,
    ) -> None:
        self.events.append((event_type, session_id, generation, state, detail))
