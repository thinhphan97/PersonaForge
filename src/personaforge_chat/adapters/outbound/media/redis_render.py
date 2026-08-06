import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from personaforge_chat.application.dto import WebRtcAnswer, WebRtcOffer
from personaforge_chat.domain.models import AvatarState
from personaforge_observability import current_context


class RedisAvatarStreamAdapter:
    """Send render commands to one worker-specific Redis Stream."""

    def __init__(self, redis_url: str, worker_id: str, livekit_url: str) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._worker_id = worker_id
        self._stream = f"avatar:commands:{worker_id}"
        self._livekit_url = livekit_url

    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None:
        event_stream = f"avatar:events:{self._worker_id}:{session_id}"
        latest = await self._redis.xrevrange(event_stream, count=1)
        last_event_id = latest[0][0] if latest else "0-0"
        command_id = await self._send(
            "avatar.session.start",
            session_id,
            {
                "character_id": character_id,
                "room_name": room_name,
                "livekit_url": self._livekit_url,
                "publisher_token": publisher_token,
            },
        )
        await self._wait_until_ready(
            session_id,
            event_stream,
            last_event_id,
            command_id,
        )

    async def negotiate(
        self,
        session_id: str,
        character_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer:
        del session_id, character_id, offer
        raise RuntimeError("SDP negotiation is handled by LiveKit in livekit media mode")

    async def change_state(
        self,
        session_id: str,
        state: AvatarState,
        state_version: int,
    ) -> None:
        await self._send(
            "avatar.state.set",
            session_id,
            {"state": state.value, "state_version": state_version},
        )

    async def speak(
        self,
        session_id: str,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None:
        event_stream = f"avatar:events:{self._worker_id}:{session_id}"
        latest = await self._redis.xrevrange(event_stream, count=1)
        last_event_id = latest[0][0] if latest else "0-0"
        command_id = await self._send(
            "avatar.speech.synthesize",
            session_id,
            {
                "turn_id": turn_id,
                "utterance_id": utterance_id,
                "text": text,
                "sequence": 1,
                "is_final": True,
            },
        )
        await self._wait_for_event(
            session_id,
            event_stream,
            last_event_id,
            command_id,
            "avatar.speech.completed",
            timeout_seconds=max(30, len(text) * 0.08),
        )

    async def close(self, session_id: str) -> None:
        await self._send("avatar.session.stop", session_id, {})

    async def shutdown(self) -> None:
        await self._redis.aclose()

    async def _send(
        self,
        command_type: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> str:
        command_id = str(uuid4())
        command = {
            "schema_version": 1,
            "type": command_type,
            "command_id": command_id,
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        logging_context = current_context()
        for key in (
            "request_id",
            "trace_id",
            "span_id",
            "trace_flags",
            "turn_id",
            "utterance_id",
        ):
            value = logging_context.get(key)
            if isinstance(value, str):
                command[key] = value
        await self._redis.xadd(self._stream, {"data": json.dumps(command)})
        return command_id

    async def _wait_until_ready(
        self,
        session_id: str,
        event_stream: str,
        last_event_id: str,
        command_id: str,
        timeout_seconds: float = 15,
    ) -> None:
        await self._wait_for_event(
            session_id,
            event_stream,
            last_event_id,
            command_id,
            "avatar.session.ready",
            timeout_seconds,
        )

    async def _wait_for_event(
        self,
        session_id: str,
        event_stream: str,
        last_event_id: str,
        command_id: str,
        success_event: str,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        cursor = last_event_id
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Render worker did not become ready: {session_id}")
            batches = await self._redis.xread(
                streams={event_stream: cursor},
                count=10,
                block=max(1, min(round(remaining * 1_000), 1_000)),
            )
            for _stream, messages in batches:
                for event_id, fields in messages:
                    cursor = event_id
                    raw = fields.get("data")
                    if not isinstance(raw, str):
                        continue
                    event = json.loads(raw)
                    if event.get("command_id") != command_id:
                        continue
                    event_type = event.get("type")
                    if event_type == success_event:
                        return
                    if event_type == "avatar.session.failed":
                        detail = event.get("detail", "Render worker failed")
                        raise RuntimeError(str(detail))
