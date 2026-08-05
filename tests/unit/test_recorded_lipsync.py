from av import VideoFrame

from personaforge_chat.domain.models import AvatarState
from personaforge_worker.adapters.outbound.recorded_lipsync import (
    RecordedClipLipSyncRuntime,
)
from personaforge_worker.domain.media import LipSyncRenderRequest, PixelFormat


class FakeRecordedSource:
    def __init__(self) -> None:
        self.calls: list[tuple[AvatarState, int, bool]] = []
        self.closed = False

    def next_frame(
        self,
        state: AvatarState,
        frame_number: int,
        sync_flash: bool,
    ) -> VideoFrame:
        self.calls.append((state, frame_number, sync_flash))
        frame = VideoFrame(8, 4, "rgba")
        frame.planes[0].update(bytes((17,)) * frame.planes[0].buffer_size)
        return frame

    def close(self) -> None:
        self.closed = True


async def test_recorded_lipsync_uses_speaking_clip_pixels() -> None:
    source = FakeRecordedSource()
    runtime = RecordedClipLipSyncRuntime(source)

    output = await runtime.render(
        LipSyncRenderRequest(
            session_id="session-1",
            utterance_id="utterance-1",
            frame_index=4,
            target_pts_ns=160_000_000,
            audio_start_sample=7_680,
            audio_end_sample=8_640,
            audio_samples=bytes(1_920),
            sample_rate=48_000,
        )
    )

    assert source.calls == [(AvatarState.SPEAKING, 5, False)]
    assert output.pixel_format is PixelFormat.RGBA
    assert output.width == 8
    assert output.height == 4
    assert output.data == bytes((17,)) * 8 * 4 * 4

    await runtime.close()
    assert source.closed
