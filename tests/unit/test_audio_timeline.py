import asyncio

import pytest

from avatar_worker.application.audio_timeline import BoundedAudioTimeline
from avatar_worker.domain.media import CanonicalAudioChunk


def chunk(sequence: int, end: bool = False) -> CanonicalAudioChunk:
    return CanonicalAudioChunk(
        session_id="session-1",
        turn_id="turn-1",
        utterance_id="utterance-1",
        sequence=sequence,
        start_sample=0,
        samples=bytes(960 * 2),
        end_of_utterance=end,
    )


@pytest.mark.asyncio
async def test_audio_timeline_gives_each_consumer_every_chunk_with_global_sample_pts() -> None:
    timeline = BoundedAudioTimeline(maximum_buffer_samples=4_000)
    media = timeline.reader("media")
    lipsync = timeline.reader("lipsync")

    first = await timeline.append(chunk(0))
    second = await timeline.append(chunk(1, end=True))
    await timeline.close()

    assert first.start_sample == 0
    assert second.start_sample == 960
    assert [await media.read(), await media.read()] == [first, second]
    assert [await lipsync.read(), await lipsync.read()] == [first, second]
    assert await media.read() is None
    assert await lipsync.read() is None


@pytest.mark.asyncio
async def test_audio_timeline_backpressures_until_all_readers_advance() -> None:
    timeline = BoundedAudioTimeline(maximum_buffer_samples=960)
    media = timeline.reader("media")
    lipsync = timeline.reader("lipsync")
    await timeline.append(chunk(0))

    blocked_append = asyncio.create_task(timeline.append(chunk(1)))
    await asyncio.sleep(0)
    assert not blocked_append.done()
    await media.read()
    await asyncio.sleep(0)
    assert not blocked_append.done()
    await lipsync.read()

    appended = await asyncio.wait_for(blocked_append, timeout=0.1)
    assert appended.start_sample == 960
    await timeline.close()
