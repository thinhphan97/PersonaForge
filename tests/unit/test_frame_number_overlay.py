from av import VideoFrame

from avatar_chat.adapters.outbound.media.switchable_track import (
    draw_frame_number,
    draw_sync_flash,
)


def test_frame_number_overlay_changes_luma_pixels_and_preserves_size() -> None:
    source = VideoFrame(width=320, height=180, format="yuv420p")
    source.planes[0].update(bytes([80]) * source.planes[0].buffer_size)

    output = draw_frame_number(source, 123)
    luma = bytes(output.planes[0])

    assert output.width == 320
    assert output.height == 180
    assert 16 in luma
    assert 235 in luma


def test_different_frame_numbers_produce_different_overlays() -> None:
    first = VideoFrame(width=320, height=180, format="yuv420p")
    second = VideoFrame(width=320, height=180, format="yuv420p")

    first_output = draw_frame_number(first, 1)
    second_output = draw_frame_number(second, 2)

    assert bytes(first_output.planes[0]) != bytes(second_output.planes[0])


def test_sync_flash_draws_bright_top_right_marker() -> None:
    source = VideoFrame(width=320, height=180, format="yuv420p")

    output = draw_sync_flash(source)

    assert 235 in bytes(output.planes[0])
