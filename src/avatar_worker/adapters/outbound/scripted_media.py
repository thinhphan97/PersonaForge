import math
import struct
from collections.abc import AsyncIterator, Iterable

from avatar_worker.domain.media import (
    CanonicalAudioChunk,
    LipSyncRenderRequest,
    NativeAudioChunk,
    PixelFormat,
    SpeechSegment,
    TimedVideoFrame,
)


class ScriptedToneTtsRuntime:
    """Deterministic streaming TTS stand-in; replace through the runtime port."""

    def __init__(
        self,
        sample_rate: int = 48_000,
        frame_samples: int = 960,
        milliseconds_per_character: int = 40,
    ) -> None:
        self._sample_rate = sample_rate
        self._frame_samples = frame_samples
        self._milliseconds_per_character = milliseconds_per_character
        self.closed = False

    async def synthesize(self, segment: SpeechSegment) -> AsyncIterator[NativeAudioChunk]:
        duration_ms = max(200, len(segment.text) * self._milliseconds_per_character)
        total_samples = max(
            self._frame_samples,
            math.ceil(duration_ms / 1_000 * self._sample_rate / self._frame_samples)
            * self._frame_samples,
        )
        peak = round(32_767 * 0.12)
        for sequence, start in enumerate(range(0, total_samples, self._frame_samples)):
            pcm = bytearray(self._frame_samples * 2)
            for offset in range(self._frame_samples):
                sample_number = start + offset
                value = round(
                    peak * math.sin(2 * math.pi * 440 * sample_number / self._sample_rate)
                )
                struct.pack_into("<h", pcm, offset * 2, value)
            yield NativeAudioChunk(
                utterance_id=segment.utterance_id,
                sequence=sequence,
                samples=bytes(pcm),
                sample_rate=self._sample_rate,
                channels=1,
                end_of_utterance=segment.is_final and start + self._frame_samples >= total_samples,
            )

    async def close(self) -> None:
        self.closed = True

class PcmS16MonoCanonicalizer:
    def __init__(self, sample_rate: int = 48_000) -> None:
        self._sample_rate = sample_rate

    def convert(
        self,
        segment: SpeechSegment,
        chunk: NativeAudioChunk,
    ) -> Iterable[CanonicalAudioChunk]:
        if chunk.sample_rate != self._sample_rate or chunk.channels != 1:
            raise ValueError("A resampling adapter is required for non-canonical TTS audio")
        yield CanonicalAudioChunk(
            session_id=segment.session_id,
            turn_id=segment.turn_id,
            utterance_id=segment.utterance_id,
            sequence=chunk.sequence,
            start_sample=0,
            samples=chunk.samples,
            sample_rate=chunk.sample_rate,
            channels=chunk.channels,
            end_of_utterance=chunk.end_of_utterance,
        )


class SolidColorLipSyncRuntime:
    """Deterministic lip-sync stand-in whose color reacts to PCM energy."""

    def __init__(self, width: int = 64, height: int = 36) -> None:
        self._width = width
        self._height = height
        self.closed = False

    async def render(self, request: LipSyncRenderRequest) -> TimedVideoFrame:
        has_signal = any(request.audio_samples)
        color = bytes((90, 190, 255, 255) if has_signal else (30, 40, 60, 255))
        return TimedVideoFrame(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            frame_index=request.frame_index,
            pts_ns=request.target_pts_ns,
            width=self._width,
            height=self._height,
            pixel_format=PixelFormat.RGBA,
            data=color * self._width * self._height,
        )

    async def close(self) -> None:
        self.closed = True
