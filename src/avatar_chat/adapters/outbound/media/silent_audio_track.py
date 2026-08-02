import asyncio
import math
import struct
import time
from fractions import Fraction

from aiortc import MediaStreamTrack
from av import AudioFrame

from avatar_chat.adapters.outbound.media.media_clock import MediaClock

AUDIO_SAMPLE_RATE = 48_000
AUDIO_FRAME_DURATION = 0.02
AUDIO_SAMPLES_PER_FRAME = int(AUDIO_SAMPLE_RATE * AUDIO_FRAME_DURATION)
AUDIO_TIME_BASE = Fraction(1, AUDIO_SAMPLE_RATE)


class SilentAudioTrack(MediaStreamTrack):
    """Continuous mono PCM silence; replaceable later by a TTS-backed audio track."""

    kind = "audio"

    def __init__(
        self,
        clock: MediaClock | None = None,
        beep_frequency_hz: float = 1_000,
        beep_amplitude: float = 0.25,
    ) -> None:
        super().__init__()
        self._clock = clock or MediaClock()
        self._beep_frequency_hz = beep_frequency_hz
        self._beep_amplitude = min(1, max(0, beep_amplitude))
        self._pts: int | None = None

    async def recv(self) -> AudioFrame:
        if self._pts is None:
            current_pts = self._clock.audio_pts(AUDIO_SAMPLE_RATE)
            self._pts = current_pts - current_pts % AUDIO_SAMPLES_PER_FRAME
        target = self._clock.started_at + self._pts / AUDIO_SAMPLE_RATE
        await asyncio.sleep(max(0, target - time.monotonic()))

        frame = AudioFrame(format="s16", layout="mono", samples=AUDIO_SAMPLES_PER_FRAME)
        pcm = bytearray(frame.planes[0].buffer_size)
        peak = int(32_767 * self._beep_amplitude)
        for sample_index in range(AUDIO_SAMPLES_PER_FRAME):
            sample_pts = self._pts + sample_index
            sample_time = sample_pts / AUDIO_SAMPLE_RATE
            value = 0
            if self._clock.is_sync_pulse(sample_time):
                value = int(
                    peak
                    * math.sin(2 * math.pi * self._beep_frequency_hz * sample_time)
                )
            struct.pack_into("<h", pcm, sample_index * 2, value)
        frame.planes[0].update(bytes(pcm))
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.time_base = AUDIO_TIME_BASE
        frame.pts = self._pts
        self._pts += AUDIO_SAMPLES_PER_FRAME
        return frame
