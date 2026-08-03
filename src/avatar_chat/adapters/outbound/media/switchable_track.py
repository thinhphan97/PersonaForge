import asyncio
import logging
from fractions import Fraction
from typing import cast

from aiortc import MediaStreamError, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer
from av import VideoFrame

from avatar_chat.adapters.outbound.media.media_clock import MediaClock
from avatar_chat.adapters.outbound.media.recorded_source import ClipCatalog
from avatar_chat.domain.models import AvatarState

VIDEO_TIME_BASE = Fraction(1, 90_000)
logger = logging.getLogger(__name__)
FRAME_NUMBER_SCALE = 3
FRAME_NUMBER_X = 12
FRAME_NUMBER_Y = 12
SYNC_FLASH_SIZE = 64
SYNC_FLASH_MARGIN = 12
DIGIT_BITMAPS = {
    "0": ("11111", "10001", "10011", "10101", "11001", "10001", "11111"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("11110", "00001", "00001", "11110", "10000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("10010", "10010", "10010", "11111", "00010", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01111", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "11110"),
}


class SwitchableAvatarVideoTrack(MediaStreamTrack):
    """One stable outbound track whose recorded source can change without renegotiation."""

    kind = "video"

    def __init__(
        self,
        catalog: ClipCatalog,
        clock: MediaClock | None = None,
        source_start_timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._clock = clock or MediaClock()
        self._player: MediaPlayer | None = None
        self._source: MediaStreamTrack | None = None
        self._pending_frame: VideoFrame | None = None
        self._generation = 0
        self._switch_lock = asyncio.Lock()
        self._active_reads: dict[MediaStreamTrack, int] = {}
        self._retired_sources: set[MediaStreamTrack] = set()
        self._last_pts = -1
        self._frame_number = 0
        self._state: AvatarState | None = None
        self._source_start_timeout_seconds = source_start_timeout_seconds

    async def switch(self, state: AvatarState) -> None:
        path = self._catalog.path_for(state)
        player = MediaPlayer(str(path), loop=True)
        source = player.video
        if source is None:
            raise RuntimeError(f"Avatar clip has no video track: {path}")
        try:
            first_frame = cast(
                VideoFrame,
                await asyncio.wait_for(
                    source.recv(),
                    timeout=self._source_start_timeout_seconds,
                ),
            )
        except BaseException:
            source.stop()
            raise

        source_to_stop: MediaStreamTrack | None = None
        async with self._switch_lock:
            previous = self._source
            self._player = player
            self._source = source
            self._pending_frame = first_frame
            self._state = state
            self._generation += 1
        if previous is not None:
            async with self._switch_lock:
                if self._active_reads.get(previous, 0) > 0:
                    self._retired_sources.add(previous)
                else:
                    source_to_stop = previous
        if source_to_stop is not None:
            source_to_stop.stop()

    async def recv(self) -> VideoFrame:
        while True:
            async with self._switch_lock:
                source = self._source
                generation = self._generation
                pending_frame = self._pending_frame
                if pending_frame is not None:
                    self._pending_frame = None
                elif source is not None:
                    self._active_reads[source] = self._active_reads.get(source, 0) + 1
            if source is None:
                await asyncio.sleep(0.01)
                continue

            if pending_frame is not None:
                frame = pending_frame
            else:
                try:
                    frame = cast(VideoFrame, await source.recv())
                except MediaStreamError:
                    await self._release_source_read(source)
                    async with self._switch_lock:
                        source_changed = generation != self._generation
                    if source_changed:
                        continue
                    state = self._state
                    if state is None:
                        raise
                    logger.debug(
                        "Restarting avatar clip after EOF: state=%s generation=%s",
                        self._state,
                        generation,
                    )
                    await self.switch(state)
                    continue
                except Exception:
                    await self._release_source_read(source)
                    async with self._switch_lock:
                        source_changed = generation != self._generation
                    if source_changed:
                        continue
                    logger.exception(
                        "Avatar source failed: state=%s generation=%s",
                        self._state,
                        generation,
                    )
                    raise
                else:
                    await self._release_source_read(source)

            async with self._switch_lock:
                if generation != self._generation:
                    continue

            pts = self._clock.video_pts()
            frame.pts = max(pts, self._last_pts + 1)
            frame.time_base = VIDEO_TIME_BASE
            self._last_pts = frame.pts
            self._frame_number += 1
            output = draw_frame_number(frame, self._frame_number)
            if self._clock.is_sync_pulse():
                output = draw_sync_flash(output)
            return output

    def stop(self) -> None:
        source = self._source
        retired = tuple(self._retired_sources)
        self._source = None
        self._player = None
        self._pending_frame = None
        self._active_reads.clear()
        self._retired_sources.clear()
        if source is not None:
            source.stop()
        for retired_source in retired:
            if retired_source is not source:
                retired_source.stop()
        super().stop()

    async def _release_source_read(self, source: MediaStreamTrack) -> None:
        source_to_stop: MediaStreamTrack | None = None
        async with self._switch_lock:
            remaining = self._active_reads.get(source, 0) - 1
            if remaining > 0:
                self._active_reads[source] = remaining
            else:
                self._active_reads.pop(source, None)
                if source in self._retired_sources:
                    self._retired_sources.remove(source)
                    source_to_stop = source
        if source_to_stop is not None:
            source_to_stop.stop()


def draw_frame_number(frame: VideoFrame, frame_number: int) -> VideoFrame:
    """Draw a high-contrast decimal counter into the top-left luma plane."""

    output = frame.reformat(format="yuv420p")
    plane = output.planes[0]
    pixels = bytearray(plane)
    stride = plane.line_size
    label = f"{frame_number:06d}"
    glyph_width = 5 * FRAME_NUMBER_SCALE
    glyph_height = 7 * FRAME_NUMBER_SCALE
    spacing = FRAME_NUMBER_SCALE
    box_width = len(label) * (glyph_width + spacing) + spacing
    box_height = glyph_height + spacing * 2

    _fill_luma_rect(
        pixels,
        stride,
        output.width,
        output.height,
        FRAME_NUMBER_X,
        FRAME_NUMBER_Y,
        box_width,
        box_height,
        16,
    )
    cursor_x = FRAME_NUMBER_X + spacing
    cursor_y = FRAME_NUMBER_Y + spacing
    for character in label:
        bitmap = DIGIT_BITMAPS[character]
        for row_index, row in enumerate(bitmap):
            for column_index, enabled in enumerate(row):
                if enabled == "1":
                    _fill_luma_rect(
                        pixels,
                        stride,
                        output.width,
                        output.height,
                        cursor_x + column_index * FRAME_NUMBER_SCALE,
                        cursor_y + row_index * FRAME_NUMBER_SCALE,
                        FRAME_NUMBER_SCALE,
                        FRAME_NUMBER_SCALE,
                        235,
                    )
        cursor_x += glyph_width + spacing
    plane.update(bytes(pixels))
    return output


def draw_sync_flash(frame: VideoFrame) -> VideoFrame:
    """Draw the visual half of the shared A/V synchronization marker."""

    output = frame.reformat(format="yuv420p")
    plane = output.planes[0]
    pixels = bytearray(plane)
    _fill_luma_rect(
        pixels,
        plane.line_size,
        output.width,
        output.height,
        output.width - SYNC_FLASH_SIZE - SYNC_FLASH_MARGIN,
        SYNC_FLASH_MARGIN,
        SYNC_FLASH_SIZE,
        SYNC_FLASH_SIZE,
        235,
    )
    plane.update(bytes(pixels))
    return output


def _fill_luma_rect(
    pixels: bytearray,
    stride: int,
    frame_width: int,
    frame_height: int,
    x: int,
    y: int,
    width: int,
    height: int,
    value: int,
) -> None:
    left = max(0, x)
    top = max(0, y)
    right = min(frame_width, x + width)
    bottom = min(frame_height, y + height)
    if left >= right or top >= bottom:
        return
    row_value = bytes([value]) * (right - left)
    for row in range(top, bottom):
        start = row * stride + left
        pixels[start : start + len(row_value)] = row_value
