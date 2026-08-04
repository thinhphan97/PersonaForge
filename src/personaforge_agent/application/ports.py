from collections.abc import AsyncIterator
from typing import Protocol

from personaforge_agent.domain.models import AgentTurn


class LanguageModelPort(Protocol):
    def stream(self, turn: AgentTurn) -> AsyncIterator[str]: ...


class DelegationVerifierPort(Protocol):
    def verify(self, token: str, turn: AgentTurn) -> None: ...


class TurnClaimPort(Protocol):
    async def ensure_valid(self) -> None: ...

    async def complete(self, outcome: str) -> None: ...

    async def release(self) -> None: ...


class TurnRegistryPort(Protocol):
    async def claim(self, turn_id: str) -> TurnClaimPort | None: ...

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...
