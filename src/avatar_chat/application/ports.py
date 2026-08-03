from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from avatar_chat.application.dto import (
    CharacterSummary,
    MediaProvisioning,
    ReplyChunk,
    WebRtcAnswer,
    WebRtcOffer,
)
from avatar_chat.domain.models import AvatarSession, AvatarState, ChatMessage


class SessionRepository(Protocol):
    async def get(self, session_id: str) -> AvatarSession | None: ...

    async def save(
        self,
        session: AvatarSession,
        expected_version: int | None = None,
    ) -> None: ...

    async def delete(self, session_id: str) -> None: ...


class ConversationRepository(Protocol):
    async def append(self, message: ChatMessage) -> None: ...

    async def list_recent(self, session_id: str, limit: int) -> tuple[ChatMessage, ...]: ...


class CharacterCatalogPort(Protocol):
    def get(self, character_id: str) -> CharacterSummary | None: ...

    def list(self) -> tuple[CharacterSummary, ...]: ...


class AvatarStreamPort(Protocol):
    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None: ...

    async def negotiate(
        self,
        session_id: str,
        character_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer: ...

    async def change_state(
        self,
        session_id: str,
        state: AvatarState,
        state_version: int,
    ) -> None: ...

    async def close(self, session_id: str) -> None: ...

    async def shutdown(self) -> None: ...


class MediaRoomPort(Protocol):
    async def provision(self, session_id: str) -> MediaProvisioning: ...

    async def close(self, session_id: str) -> None: ...


class ReplyGeneratorPort(Protocol):
    def stream_reply(
        self,
        session_id: str,
        messages: tuple[ChatMessage, ...],
    ) -> AsyncIterator[ReplyChunk]: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
