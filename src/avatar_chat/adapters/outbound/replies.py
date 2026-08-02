import asyncio
import re
from collections.abc import AsyncIterator

from avatar_chat.application.dto import ReplyChunk
from avatar_chat.application.ports import ReplyGeneratorPort
from avatar_chat.domain.models import ChatMessage, ChatRole


class ScriptedReplyAdapter:
    """Development adapter; replace it with any LLM adapter implementing the same port."""

    def __init__(self, chunk_delay_seconds: float = 0) -> None:
        self._chunk_delay_seconds = chunk_delay_seconds

    async def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]:
        del session_id
        latest = next(
            (message.content for message in reversed(messages) if message.role is ChatRole.USER),
            "",
        )
        response = (
            "Xin chào! Đây là phản hồi streaming giả lập của nhân vật. "
            f"Bạn vừa nói: {latest}"
        )
        for token in re.findall(r"\S+\s*", response):
            if self._chunk_delay_seconds:
                await asyncio.sleep(self._chunk_delay_seconds)
            yield ReplyChunk(token)


class DelayedReplyAdapter:
    """Decorator applying presentation pacing to any reply provider."""

    def __init__(self, delegate: ReplyGeneratorPort, chunk_delay_seconds: float) -> None:
        self._delegate = delegate
        self._chunk_delay_seconds = max(0, chunk_delay_seconds)

    async def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]:
        async for chunk in self._delegate.stream_reply(session_id, messages):
            if self._chunk_delay_seconds:
                await asyncio.sleep(self._chunk_delay_seconds)
            yield chunk
