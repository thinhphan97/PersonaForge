import av

from personaforge_chat.adapters.outbound.media.recorded_source import (
    RecordedCharacterCatalog,
    RecordedClipCatalog,
)
from personaforge_chat.bootstrap.settings import Settings
from personaforge_chat.domain.models import AvatarState
from personaforge_worker.livekit_session import (
    LiveKitRenderSession,
    _audio_frame,
    _to_livekit_video_frame,
    _video_publish_options,
)
from personaforge_worker.recorded_frames import LoopingRecordedFrameSource
from personaforge_worker.settings import WorkerSettings


def test_worker_audio_uses_exact_twenty_millisecond_pcm_frames() -> None:
    frame = _audio_frame(
        sample_rate=48_000,
        start_sample=48_000,
        samples_per_channel=960,
        debug_enabled=False,
        sync_interval=2,
        sync_duration=0.1,
    )

    assert frame.sample_rate == 48_000
    assert frame.samples_per_channel == 960
    assert frame.duration == 0.02
    assert set(bytes(frame.data)) == {0}


def test_demo_characters_have_distinct_video_colors() -> None:
    settings = Settings.from_env()
    catalog = RecordedCharacterCatalog(
        settings.characters_root,
        settings.avatar_clips,
    )
    character_ids = {character.character_id for character in catalog.list()}
    assert character_ids >= {"default", "violet", "coral"}

    colors: set[bytes] = set()
    for character_id in ("default", "violet", "coral"):
        path = catalog.clips_for(character_id).path_for(AvatarState.IDLE)
        container = av.open(str(path))
        try:
            frame = next(container.decode(video=0)).reformat(format="rgb24")
            colors.add(bytes(frame.planes[0])[:3])
        finally:
            container.close()

    assert len(colors) == 3


def test_worker_sync_pulse_generates_beep_and_video_flash() -> None:
    settings = Settings.from_env()
    source = LoopingRecordedFrameSource(
        RecordedClipCatalog(settings.avatar_clips),
        width=640,
        height=360,
    )
    try:
        video = source.next_frame(AvatarState.IDLE, frame_number=1, sync_flash=True)
    finally:
        source.close()
    audio = _audio_frame(
        sample_rate=48_000,
        start_sample=0,
        samples_per_channel=960,
        debug_enabled=True,
        sync_interval=2,
        sync_duration=0.1,
    )
    livekit_video = _to_livekit_video_frame(video)

    assert any(value != 0 for value in bytes(audio.data))
    assert livekit_video.width == 640
    assert livekit_video.height == 360
    assert len(livekit_video.data) == 640 * 360 * 4


def test_worker_publishes_one_fixed_resolution_video_layer() -> None:
    settings = WorkerSettings.from_env()
    options = _video_publish_options(settings)

    assert options.HasField("simulcast")
    assert options.simulcast is False
    assert options.video_encoding.max_framerate == settings.video_fps
    assert options.video_encoding.max_bitrate == settings.video_bitrate


def test_worker_keeps_custom_resolution_after_the_first_frame() -> None:
    settings = Settings.from_env()
    expected_size = (960, 540)
    source = LoopingRecordedFrameSource(
        RecordedClipCatalog(settings.avatar_clips),
        width=expected_size[0],
        height=expected_size[1],
    )
    try:
        frames = [
            _to_livekit_video_frame(
                source.next_frame(
                    AvatarState.IDLE,
                    frame_number=frame_number,
                    sync_flash=False,
                )
            )
            for frame_number in range(1, 7)
        ]
    finally:
        source.close()

    assert {(frame.width, frame.height) for frame in frames} == {expected_size}
    assert all(len(frame.data) == expected_size[0] * expected_size[1] * 4 for frame in frames)


def test_worker_ignores_out_of_order_avatar_state() -> None:
    session = LiveKitRenderSession(
        session_id="session-1",
        character_id="default",
        livekit_url="ws://media.test",
        publisher_token="token",
        settings=WorkerSettings.from_env(),
    )

    session.set_state(AvatarState.SPEAKING, state_version=5)
    session.set_state(AvatarState.IDLE, state_version=4)

    assert session._state is AvatarState.SPEAKING
    assert session._state_version == 5
