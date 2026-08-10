"""Model-independent avatar runtime value objects."""

from dataclasses import dataclass
from enum import Enum, IntFlag


class AvatarOutputKind(str, Enum):
    VIDEO = "video"
    MOTION = "motion"


class RenderLocation(str, Enum):
    SERVER = "server"
    CLIENT = "client"


class AvatarTransportKind(str, Enum):
    MEDIA_TRACK = "media-track"
    DATA_TRACK = "data-track"
    DATA_PACKET = "data-packet"


class MotionFrameFlags(IntFlag):
    NONE = 0
    RESET = 1
    END_OF_UTTERANCE = 2
    DISCONTINUITY = 4


@dataclass(frozen=True, slots=True)
class AudioWindow:
    session_id: str
    utterance_id: str
    start_sample: int
    samples: bytes
    sample_rate: int = 48_000
    channels: int = 1


@dataclass(frozen=True, slots=True)
class ExpressionFrame:
    sequence: int
    pts_sample: int
    duration_samples: int
    schema: str
    values: tuple[float, ...]
    flags: MotionFrameFlags = MotionFrameFlags.NONE


@dataclass(frozen=True, slots=True)
class TimedMotionFrame:
    session_id: str
    utterance_id: str
    generation: int
    sequence: int
    pts_sample: int
    duration_samples: int
    sample_rate: int
    schema: str
    values: tuple[float, ...]
    flags: MotionFrameFlags = MotionFrameFlags.NONE
