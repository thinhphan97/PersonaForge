import asyncio

import pytest

from avatar_worker.adapters.outbound.queued_media import (
    QueuedMediaPublisher,
    acknowledge,
)
from avatar_worker.adapters.outbound.scripted_media import (
    PcmS16MonoCanonicalizer,
    ScriptedToneTtsRuntime,
    SolidColorLipSyncRuntime,
)
from avatar_worker.application.audio_timeline import BoundedAudioTimeline
from avatar_worker.application.speech_pipeline import CompositeSpeechPipeline
from avatar_worker.domain.media import SpeechSegment

from ..support.avatar_media import RecordingMediaPublisher


@pytest.mark.asyncio
async def test_speech_pipeline_fans_out_same_audio_timeline_and_completes_after_video() -> None:
    tts = ScriptedToneTtsRuntime(milliseconds_per_character=10)
    lipsync = SolidColorLipSyncRuntime()
    publisher = RecordingMediaPublisher()
    pipeline = CompositeSpeechPipeline(
        tts,
        PcmS16MonoCanonicalizer(),
        BoundedAudioTimeline(maximum_buffer_samples=48_000),
        lipsync,
        publisher,
        video_fps=25,
    )
    await pipeline.start()
    await pipeline.wait_ready()
    await pipeline.speak(
        SpeechSegment(
            session_id="session-1",
            turn_id="turn-1",
            utterance_id="utterance-1",
            sequence=1,
            text="Xin chao",
            is_final=True,
        )
    )

    await pipeline.wait_playback("utterance-1", timeout=1)

    assert publisher.audio
    assert publisher.video
    assert publisher.audio[-1].end_of_utterance
    assert [frame.start_sample for frame in publisher.audio] == sorted(
        frame.start_sample for frame in publisher.audio
    )
    assert [frame.pts_ns for frame in publisher.video] == sorted(
        frame.pts_ns for frame in publisher.video
    )
    for video in publisher.video:
        expected_sample = round(video.pts_ns / 1_000_000_000 * 48_000)
        assert any(
            audio.start_sample <= expected_sample < audio.end_sample
            for audio in publisher.audio
        )

    await pipeline.stop()
    assert tts.closed
    assert lipsync.closed
    assert publisher.closed


@pytest.mark.asyncio
async def test_queued_media_pipeline_completes_only_after_livekit_acknowledges_frames() -> None:
    publisher = QueuedMediaPublisher()
    pipeline = CompositeSpeechPipeline(
        ScriptedToneTtsRuntime(milliseconds_per_character=1),
        PcmS16MonoCanonicalizer(),
        BoundedAudioTimeline(maximum_buffer_samples=48_000),
        SolidColorLipSyncRuntime(),
        publisher,
        video_fps=25,
    )
    await pipeline.start()
    utterance_id = "utterance-queued"
    await pipeline.speak(
        SpeechSegment(
            session_id="session-1",
            turn_id="turn-1",
            utterance_id=utterance_id,
            sequence=1,
            text="Xin chao",
            is_final=True,
        )
    )
    playback = asyncio.create_task(
        pipeline.wait_playback(utterance_id, timeout=1)
    )
    acknowledged_audio = 0
    acknowledged_video = 0
    while not playback.done():
        audio = publisher.pop_audio()
        if audio is not None:
            acknowledge(audio)
            acknowledged_audio += 1
        video = publisher.pop_video()
        if video is not None:
            acknowledge(video)
            acknowledged_video += 1
        await asyncio.sleep(0)

    await playback
    assert acknowledged_audio > 0
    assert acknowledged_video > 0
    await pipeline.stop()
