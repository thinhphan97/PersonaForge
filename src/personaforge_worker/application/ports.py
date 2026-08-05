from typing import Protocol

from personaforge_worker.domain.models import CompositeStartRequest, PersonaSessionState


class CompositeSessionRuntimePort(Protocol):
    async def start(self) -> None: ...
    async def wait_ready(self) -> None: ...
    async def stop(self) -> None: ...


class CompositeSessionFactoryPort(Protocol):
    async def create(self, request: CompositeStartRequest) -> CompositeSessionRuntimePort: ...


class WorkerEventPort(Protocol):
    async def publish(
        self,
        event_type: str,
        session_id: str,
        generation: int,
        state: PersonaSessionState,
        detail: str | None = None,
    ) -> None: ...
