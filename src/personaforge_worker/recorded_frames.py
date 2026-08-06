from collections.abc import Iterator
from pathlib import Path

import av
from av import VideoFrame

from personaforge_chat.adapters.outbound.media.recorded_source import ClipCatalog
from personaforge_chat.adapters.outbound.media.switchable_track import (
    draw_frame_number,
    draw_sync_flash,
)
from personaforge_chat.domain.models import AvatarState


class LoopingRecordedFrameSource:
    """Decode the selected clip frame-by-frame and reopen it on EOF/state changes."""

    def __init__(self, catalog: ClipCatalog, width: int, height: int) -> None:
        self._catalog = catalog
        self._width = width
        self._height = height
        self._state: AvatarState | None = None
        self._container: av.container.InputContainer | None = None
        self._frames: Iterator[VideoFrame] | None = None

    def next_frame(
        self,
        state: AvatarState,
        frame_number: int | None,
        sync_flash: bool,
    ) -> VideoFrame:
        if state is not self._state or self._frames is None:
            self._open(state)
        assert self._frames is not None
        try:
            frame = next(self._frames)
        except StopIteration:
            self._open(state)
            assert self._frames is not None
            frame = next(self._frames)

        output = frame.reformat(width=self._width, height=self._height, format="yuv420p")
        if frame_number is not None:
            output = draw_frame_number(output, frame_number)
        if sync_flash:
            output = draw_sync_flash(output)
        return output

    def close(self) -> None:
        if self._container is not None:
            self._container.close()
        self._container = None
        self._frames = None

    def _open(self, state: AvatarState) -> None:
        self.close()
        path: Path = self._catalog.path_for(state)
        self._container = av.open(str(path))
        self._frames = iter(self._container.decode(video=0))
        self._state = state
