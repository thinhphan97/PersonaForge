import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from personaforge_agent.application.ports import LanguageModelPort
from personaforge_agent.domain.models import AgentEvent, AgentEventType, AgentTurn


@dataclass(slots=True)
class ExecuteAgentTurn:
    model: LanguageModelPort
    heartbeat_interval_seconds: float = 5.0

    async def execute(
        self,
        turn: AgentTurn,
        cancelled: asyncio.Event,
    ) -> AsyncIterator[AgentEvent]:
        sequence = 1
        yield AgentEvent(AgentEventType.TURN_STARTED, sequence, stage="model")
        iterator = self.model.stream(turn).__aiter__()
        try:
            while not cancelled.is_set():
                next_chunk: asyncio.Future[str] = asyncio.ensure_future(anext(iterator))
                try:
                    while not cancelled.is_set():
                        done, _ = await asyncio.wait(
                            {next_chunk},
                            timeout=self.heartbeat_interval_seconds,
                        )
                        if next_chunk in done:
                            try:
                                text = next_chunk.result()
                            except StopAsyncIteration:
                                sequence += 1
                                yield AgentEvent(
                                    AgentEventType.TURN_COMPLETED,
                                    sequence,
                                    stage="complete",
                                )
                                return
                            sequence += 1
                            yield AgentEvent(
                                AgentEventType.TEXT_DELTA,
                                sequence,
                                text=text,
                            )
                            break
                        sequence += 1
                        yield AgentEvent(
                            AgentEventType.HEARTBEAT,
                            sequence,
                            stage="model",
                        )
                finally:
                    if cancelled.is_set() and not next_chunk.done():
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
        except Exception as exc:
            sequence += 1
            yield AgentEvent(
                AgentEventType.TURN_FAILED,
                sequence,
                stage="model",
                error_code=type(exc).__name__,
                error_detail=str(exc),
            )
        finally:
            if hasattr(iterator, "aclose"):
                await iterator.aclose()
