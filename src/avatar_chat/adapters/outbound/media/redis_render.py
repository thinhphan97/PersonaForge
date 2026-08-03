import json
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
        await self._send(
            "avatar.session.start",
            session_id,
            {
                "character_id": character_id,
                "room_name": room_name,
                "livekit_url": self._livekit_url,
                "publisher_token": publisher_token,
            },
        )

    async def negotiate(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        del session_id, offer
        raise RuntimeError("SDP negotiation is handled by LiveKit in livekit media mode")

    async def change_state(self, session_id: str, state: AvatarState) -> None:
        await self._send(
            "avatar.state.set",
            session_id,
            {"state": state.value},
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
    ) -> None:
        command = {
            "schema_version": 1,
            "type": command_type,
            "command_id": str(uuid4()),
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        await self._redis.xadd(self._stream, {"data": json.dumps(command)})
