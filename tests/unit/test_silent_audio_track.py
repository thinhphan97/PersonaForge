import pytest

from avatar_chat.adapters.outbound.media.media_clock import MediaClock
from avatar_chat.adapters.outbound.media.silent_audio_track import (
    AUDIO_SAMPLE_RATE,
    AUDIO_SAMPLES_PER_FRAME,
    SilentAudioTrack,
)


@pytest.mark.asyncio
async def test_silent_audio_track_produces_zero_pcm_frames() -> None:
    track = SilentAudioTrack()
    try:
        first = await track.recv()
        second = await track.recv()
    finally:
        track.stop()

    assert first.sample_rate == AUDIO_SAMPLE_RATE
    assert first.samples == AUDIO_SAMPLES_PER_FRAME
    assert set(bytes(first.planes[0])) == {0}
    assert second.pts - first.pts == AUDIO_SAMPLES_PER_FRAME


@pytest.mark.asyncio
async def test_sync_debug_track_produces_beep_samples() -> None:
    clock = MediaClock(
        sync_debug_enabled=True,
        sync_interval_seconds=2,
        sync_pulse_duration_seconds=2,
    )
    track = SilentAudioTrack(clock)
    try:
        frame = await track.recv()
    finally:
        track.stop()

    assert any(value != 0 for value in bytes(frame.planes[0]))
