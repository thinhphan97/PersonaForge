import asyncio
import re
from collections.abc import AsyncIterator

from personaforge_agent.domain.models import AgentTurn


class ScriptedLanguageModelAdapter:
    def __init__(self, chunk_delay_seconds: float = 0.1) -> None:
        self._chunk_delay_seconds = max(0.0, chunk_delay_seconds)

    async def stream(self, turn: AgentTurn) -> AsyncIterator[str]:
        response = (
            f"Xin chào! Tôi là Agent '{turn.agent_id}' của PersonaForge. "
            f"Bạn vừa hỏi: {turn.user_message}"
        )
        for token in re.findall(r"\S+\s*", response):
            if self._chunk_delay_seconds:
                await asyncio.sleep(self._chunk_delay_seconds)
            yield token
