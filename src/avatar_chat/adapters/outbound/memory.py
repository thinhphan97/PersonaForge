import asyncio
from collections import defaultdict
from copy import deepcopy

from avatar_chat.domain.errors import ConcurrentSessionUpdate
from avatar_chat.domain.models import AvatarSession, ChatMessage


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._items: dict[str, AvatarSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> AvatarSession | None:
        async with self._lock:
            session = self._items.get(session_id)
            return deepcopy(session) if session else None

    async def save(
        self,
        session: AvatarSession,
        expected_version: int | None = None,
    ) -> None:
        async with self._lock:
            current = self._items.get(session.session_id)
            if expected_version is not None:
                if current is None or current.version != expected_version:
                    raise ConcurrentSessionUpdate(session.session_id)
                session.version = expected_version + 1
            elif current is not None:
                raise ConcurrentSessionUpdate(session.session_id)
            self._items[session.session_id] = deepcopy(session)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._items.pop(session_id, None)


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._items: dict[str, list[ChatMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def append(self, message: ChatMessage) -> None:
        async with self._lock:
            self._items[message.session_id].append(deepcopy(message))

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]:
        async with self._lock:
            selected = self._items.get(session_id, [])[-max(0, limit) :]
            return tuple(deepcopy(selected))
