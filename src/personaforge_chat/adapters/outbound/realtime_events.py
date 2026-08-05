import asyncio
import json
import logging
from contextlib import suppress

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from personaforge_chat.application.dto import ChatEventType, ChatOutputEvent
from personaforge_chat.application.ports import RealtimeEventHandler
from personaforge_chat.domain.models import AvatarState

logger = logging.getLogger(__name__)


class InMemoryRealtimeEventBus:
    def __init__(self) -> None:
        self._handlers: list[RealtimeEventHandler] = []

    async def subscribe(self, handler: RealtimeEventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, session_id: str, event: ChatOutputEvent) -> None:
        for handler in tuple(self._handlers):
            await handler(session_id, event)

    async def shutdown(self) -> None:
        self._handlers.clear()


class RedisRealtimeEventBus:
    def __init__(
        self,
        redis_url: str,
        channel: str = "avatar:chat-events",
    ) -> None:
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._channel = channel
        self._handler: RealtimeEventHandler | None = None
        self._pubsub: PubSub | None = None
        self._listener: asyncio.Task[None] | None = None

    async def subscribe(self, handler: RealtimeEventHandler) -> None:
        if self._listener is not None:
            raise RuntimeError("Realtime event bus already has a subscriber")
        self._handler = handler
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._listener = asyncio.create_task(self._listen())

    async def publish(self, session_id: str, event: ChatOutputEvent) -> None:
        await self._redis.publish(
            self._channel,
            json.dumps(
                {
                    "session_id": session_id,
                    "event": _event_payload(event),
                }
            ),
        )

    async def shutdown(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None
        if self._pubsub is not None:
            await self._pubsub.unsubscribe(self._channel)
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
            self._pubsub = None
        await self._redis.aclose()

    async def _listen(self) -> None:
        assert self._pubsub is not None
        async for message in self._pubsub.listen():
            if message.get("type") != "message":
                continue
            raw = message.get("data")
            if not isinstance(raw, str) or self._handler is None:
                continue
            try:
                payload = json.loads(raw)
                await self._handler(
                    str(payload["session_id"]),
                    _event_from_payload(payload["event"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Ignoring invalid realtime event payload", exc_info=True)
            except Exception:
                logger.exception("Realtime event handler failed")


def _event_payload(event: ChatOutputEvent) -> dict[str, object | None]:
    return {
        "type": event.type.value,
        "content": event.content,
        "avatar_state": event.avatar_state.value if event.avatar_state else None,
        "message_id": event.message_id,
        "sequence": event.sequence,
        "revision": event.revision,
        "author_user_id": event.author_user_id,
        "session_version": event.session_version,
    }


def _event_from_payload(payload: dict[str, object]) -> ChatOutputEvent:
    avatar_state = payload.get("avatar_state")
    return ChatOutputEvent(
        type=ChatEventType(str(payload["type"])),
        content=_optional_string(payload.get("content")),
        avatar_state=AvatarState(str(avatar_state)) if avatar_state else None,
        message_id=_optional_string(payload.get("message_id")),
        sequence=_optional_int(payload.get("sequence")),
        revision=_optional_int(payload.get("revision")),
        author_user_id=_optional_string(payload.get("author_user_id")),
        session_version=_optional_int(payload.get("session_version")),
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    return int(str(value)) if value is not None else None
