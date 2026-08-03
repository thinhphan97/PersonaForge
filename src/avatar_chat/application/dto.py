from dataclasses import dataclass
from enum import Enum

from avatar_chat.domain.models import AvatarState


@dataclass(frozen=True, slots=True)
class WebRtcOffer:
    sdp: str
    type: str


@dataclass(frozen=True, slots=True)
class WebRtcAnswer:
    sdp: str
    type: str


@dataclass(frozen=True, slots=True)
class CreateSessionResult:
    session_id: str
    character_id: str


@dataclass(frozen=True, slots=True)
class MediaAccess:
    provider: str
    server_url: str
    access_token: str
    room_name: str


@dataclass(frozen=True, slots=True)
class MediaProvisioning:
    client: MediaAccess
    publisher_token: str


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    text: str


class ChatEventType(str, Enum):
    AVATAR_STATE = "avatar_state"
    ASSISTANT_CHUNK = "assistant_chunk"
    ASSISTANT_COMPLETE = "assistant_complete"


@dataclass(frozen=True, slots=True)
class ChatOutputEvent:
    type: ChatEventType
    content: str | None = None
    avatar_state: AvatarState | None = None
