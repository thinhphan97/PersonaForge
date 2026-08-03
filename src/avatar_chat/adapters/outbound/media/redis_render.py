import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from avatar_chat.application.dto import WebRtcAnswer, WebRtcOffer
from avatar_chat.domain.models import AvatarState


class RedisAvatarStreamAdapter:
    """Send render commands to one worker-specific Redis Stream."""

    def __init__(self, redis_url: str, worker_id: str, livekit_url: str) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._stream = f"avatar:commands:{worker_id}"
        self._livekit_url = livekit_url

    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None:
        event_stream = f"avatar:events:{session_id}"
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

    async def negotiate(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        del session_id, offer
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
                    if event_type == "avatar.session.ready":
                        return
                    if event_type == "avatar.session.failed":
                        detail = event.get("detail", "Render worker failed")
                        raise RuntimeError(str(detail))
