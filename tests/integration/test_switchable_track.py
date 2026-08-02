import pytest

from avatar_chat.adapters.outbound.media.recorded_source import RecordedClipCatalog
from avatar_chat.adapters.outbound.media.switchable_track import SwitchableAvatarVideoTrack
from avatar_chat.bootstrap.settings import Settings
from avatar_chat.domain.models import AvatarState


@pytest.mark.asyncio
async def test_clip_switch_keeps_monotonic_timestamp() -> None:
    settings = Settings.from_env()
    track = SwitchableAvatarVideoTrack(RecordedClipCatalog(settings.avatar_clips))
    try:
        await track.switch(AvatarState.IDLE)
        idle_frame = await track.recv()
        await track.switch(AvatarState.THINKING)
        thinking_frame = await track.recv()
    finally:
        track.stop()

    assert idle_frame.width == thinking_frame.width == 640
    assert idle_frame.height == thinking_frame.height == 360
    assert thinking_frame.pts > idle_frame.pts
    assert thinking_frame.time_base == idle_frame.time_base


@pytest.mark.asyncio
async def test_clip_restarts_after_media_player_eof() -> None:
    settings = Settings.from_env()
    track = SwitchableAvatarVideoTrack(RecordedClipCatalog(settings.avatar_clips))
    frames = []
    try:
        await track.switch(AvatarState.SPEAKING)
        for _ in range(100):
            frames.append(await track.recv())
    finally:
        track.stop()

    assert len(frames) == 100
    assert frames[-1].pts > frames[0].pts
