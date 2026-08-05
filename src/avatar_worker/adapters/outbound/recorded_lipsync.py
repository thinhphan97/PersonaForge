from typing import Protocol

from av import VideoFrame

from avatar_chat.domain.models import AvatarState
from avatar_worker.domain.media import (
    LipSyncRenderRequest,
    PixelFormat,
    TimedVideoFrame,
)


class RecordedFrameSource(Protocol):
    def next_frame(
        self,
        state: AvatarState,
        frame_number: int,
        sync_flash: bool,
    ) -> VideoFrame: ...

    def close(self) -> None: ...


class RecordedClipLipSyncRuntime:
    """Fake lip-sync adapter that preserves the selected character's speaking clip.

    The composite pipeline still exercises the real audio/video timing boundary, but the
    placeholder renderer no longer replaces the avatar with an unrelated solid-color frame.
    A model-backed adapter can replace this port later without changing the pipeline.
    """

    def __init__(self, source: RecordedFrameSource) -> None:
        self._source = source
        self._closed = False

    async def render(self, request: LipSyncRenderRequest) -> TimedVideoFrame:
        if self._closed:
            raise RuntimeError("Recorded lip-sync runtime is closed")
        frame = self._source.next_frame(
            AvatarState.SPEAKING,
            request.frame_index + 1,
            False,
        ).reformat(format="rgba")
        data = _packed_rgba(frame)
        return TimedVideoFrame(
            session_id=request.session_id,
            utterance_id=request.utterance_id,
            frame_index=request.frame_index,
            pts_ns=request.target_pts_ns,
            width=frame.width,
            height=frame.height,
            pixel_format=PixelFormat.RGBA,
            data=data,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._source.close()


def _packed_rgba(frame: VideoFrame) -> bytes:
    plane = frame.planes[0]
    raw = bytes(plane)
    row_size = frame.width * 4
    if plane.line_size == row_size:
        return raw
    return b"".join(
        raw[row * plane.line_size : row * plane.line_size + row_size]
        for row in range(frame.height)
    )
