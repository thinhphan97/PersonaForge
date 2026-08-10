from dataclasses import dataclass
from enum import Enum


class AudioSampleFormat(str, Enum):
    S16LE = "s16le"


class PixelFormat(str, Enum):
    RGBA = "rgba"


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    session_id: str
    turn_id: str
    utterance_id: str
    sequence: int
    text: str
    language: str = "vi"
    is_final: bool = False


@dataclass(frozen=True, slots=True)
class SilenceSegment:
    """A finite silence window injected into the same canonical audio timeline."""

    session_id: str
    turn_id: str
    utterance_id: str
    duration_ms: int = 500
    sample_rate: int = 48_000


@dataclass(frozen=True, slots=True)
class NativeAudioChunk:
    utterance_id: str
    sequence: int
    samples: bytes
    sample_rate: int
    channels: int
    sample_format: AudioSampleFormat = AudioSampleFormat.S16LE
    end_of_utterance: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalAudioChunk:
    session_id: str
    turn_id: str
    utterance_id: str
    sequence: int
    start_sample: int
    samples: bytes
    sample_rate: int = 48_000
    channels: int = 1
    end_of_utterance: bool = False
    is_silence: bool = False

    @property
    def samples_per_channel(self) -> int:
        return len(self.samples) // (2 * self.channels)

    @property
    def end_sample(self) -> int:
        return self.start_sample + self.samples_per_channel


@dataclass(frozen=True, slots=True)
class LipSyncRenderRequest:
    session_id: str
    utterance_id: str
    frame_index: int
    target_pts_ns: int
    audio_start_sample: int
    audio_end_sample: int
    audio_samples: bytes
    sample_rate: int


@dataclass(frozen=True, slots=True)
class TimedVideoFrame:
    session_id: str
    utterance_id: str
    frame_index: int
    pts_ns: int
    width: int
    height: int
    pixel_format: PixelFormat
    data: bytes
