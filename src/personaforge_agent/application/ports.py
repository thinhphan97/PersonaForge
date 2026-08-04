from collections.abc import AsyncIterator
from typing import Protocol

from personaforge_agent.domain.models import AgentTurn


class LanguageModelPort(Protocol):
    def stream(self, turn: AgentTurn) -> AsyncIterator[str]: ...


class DelegationVerifierPort(Protocol):
    def verify(self, token: str, turn: AgentTurn) -> None: ...
