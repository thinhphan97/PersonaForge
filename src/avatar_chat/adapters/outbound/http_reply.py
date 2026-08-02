import json
from collections.abc import AsyncIterator

import httpx

from avatar_chat.application.dto import ReplyChunk
from avatar_chat.domain.models import ChatMessage


class HttpStreamingReplyAdapter:
    """Adapter for a model service exposing newline-delimited JSON or SSE.

    Request:  {"session_id": "...", "messages": [{"role": "...", "content": "..."}]}
    Event:    {"text": "partial response"}
    """

    def __init__(self, url: str, token: str | None = None, timeout_seconds: float = 60) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_seconds

    async def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]:
        headers = {"Accept": "text/event-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {
            "session_id": session_id,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in messages
            ],
        }
        timeout = httpx.Timeout(self._timeout, connect=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self._url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    value = line.strip()
                    if not value or value.startswith(":"):
                        continue
                    if value.startswith("data:"):
                        value = value[5:].strip()
                    if value == "[DONE]":
                        break
                    event = json.loads(value)
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        yield ReplyChunk(text)

