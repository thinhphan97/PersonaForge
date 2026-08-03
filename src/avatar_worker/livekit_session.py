import asyncio
import contextlib
import logging
import math
import struct

from av import VideoFrame as AvVideoFrame
from livekit import rtc

from avatar_chat.adapters.outbound.media.recorded_source import RecordedClipCatalog
from avatar_chat.domain.models import AvatarState
from avatar_worker.recorded_frames import LoopingRecordedFrameSource
from avatar_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


class LiveKitRenderSession:
    """One stable LiveKit publisher with a synchronized audio/video timeline."""

    def __init__(
        self,
        session_id: str,
        livekit_url: str,
        publisher_token: str,
        settings: WorkerSettings,
    ) -> None:
        self.session_id = session_id
        self._livekit_url = livekit_url
        self._publisher_token = publisher_token
        self._settings = settings
        self._state = AvatarState.IDLE
        self._stop = asyncio.Event()

    def set_state(self, state: AvatarState) -> None:
        if state not in {
            AvatarState.IDLE,
            AvatarState.THINKING,
            AvatarState.SPEAKING,
        }:
            raise ValueError(f"Unsupported render state: {state.value}")
        self._state = state
        logger.info("Avatar state session=%s state=%s", self.session_id, state.value)

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        settings = self._settings
        room = rtc.Room()
        audio_source = rtc.AudioSource(settings.audio_sample_rate, 1, queue_size_ms=50)
        video_source = rtc.VideoSource(settings.video_width, settings.video_height)
        synchronizer = rtc.AVSynchronizer(
            audio_source=audio_source,
            video_source=video_source,
            video_fps=settings.video_fps,
            video_queue_size_ms=settings.video_queue_size_ms,
        )
        recorded = LoopingRecordedFrameSource(
            RecordedClipCatalog(settings.avatar_clips),
            settings.video_width,
            settings.video_height,
        )
        try:
            await room.connect(self._livekit_url, self._publisher_token)
            audio_track = rtc.LocalAudioTrack.create_audio_track(
                "avatar-audio", audio_source
            )
            video_track = rtc.LocalVideoTrack.create_video_track(
                "avatar-video", video_source
            )
            await room.local_participant.publish_track(
                audio_track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            await room.local_participant.publish_track(
                video_track,
                _video_publish_options(settings),
            )
            logger.info("LiveKit media ready session=%s", self.session_id)
            await self._publish_timeline(synchronizer, recorded)
        finally:
            recorded.close()
            await synchronizer.aclose()
            with contextlib.suppress(Exception):
                await room.disconnect()
            await audio_source.aclose()
            await video_source.aclose()

    async def _publish_timeline(
        self,
        synchronizer: rtc.AVSynchronizer,
        recorded: LoopingRecordedFrameSource,
    ) -> None:
        settings = self._settings
        samples_per_audio_frame = round(
            settings.audio_sample_rate * settings.audio_frame_duration_seconds
        )
        if samples_per_audio_frame <= 0:
            raise ValueError("WORKER_AUDIO_FRAME_DURATION produces an empty audio frame")

        audio_sample = 0
        video_frame_number = 0
        next_metrics_at = 5.0
        while not self._stop.is_set():
            audio_start = audio_sample / settings.audio_sample_rate
            next_video_time = video_frame_number / settings.video_fps
            while next_video_time <= audio_start + 1e-9:
                av_frame = recorded.next_frame(
                    self._state,
                    video_frame_number + 1,
                    self._is_sync_pulse(next_video_time),
                )
                await synchronizer.push(
                    _to_livekit_video_frame(av_frame),
                    timestamp=next_video_time,
                )
                video_frame_number += 1
                next_video_time = video_frame_number / settings.video_fps

            audio_frame = _audio_frame(
                sample_rate=settings.audio_sample_rate,
                start_sample=audio_sample,
                samples_per_channel=samples_per_audio_frame,
                debug_enabled=settings.av_sync_debug,
                sync_interval=settings.av_sync_interval_seconds,
                sync_duration=settings.av_sync_pulse_duration_seconds,
            )
            audio_sample += samples_per_audio_frame
            await synchronizer.push(
                audio_frame,
                timestamp=audio_sample / settings.audio_sample_rate,
            )
            media_time = audio_sample / settings.audio_sample_rate
            if media_time >= next_metrics_at:
                drift_ms = (
                    synchronizer.last_video_time - synchronizer.last_audio_time
                ) * 1_000
                logger.info(
                    "Media cadence session=%s fps=%.2f av_drift_ms=%.2f",
                    self.session_id,
                    synchronizer.actual_fps,
                    drift_ms,
                )
                next_metrics_at += 5.0

    def _is_sync_pulse(self, media_time: float) -> bool:
        settings = self._settings
        return (
            settings.av_sync_debug
            and settings.av_sync_interval_seconds > 0
            and media_time % settings.av_sync_interval_seconds
            < settings.av_sync_pulse_duration_seconds
        )


def _to_livekit_video_frame(frame: AvVideoFrame) -> rtc.VideoFrame:
    rgba = frame.reformat(format="rgba")
    plane = rgba.planes[0]
    raw = bytes(plane)
    row_size = rgba.width * 4
    if plane.line_size != row_size:
        raw = b"".join(
            raw[row * plane.line_size : row * plane.line_size + row_size]
            for row in range(rgba.height)
        )
    return rtc.VideoFrame(
        rgba.width,
        rgba.height,
        rtc.VideoBufferType.RGBA,
        raw,
    )


def _video_publish_options(settings: WorkerSettings) -> rtc.TrackPublishOptions:
    """Publish exactly one layer and keep the configured source resolution.

    ``simulcast`` is an optional protobuf field. Leaving it unset allows the
    LiveKit native SDK to choose multiple layers. An adaptive subscriber can
    then switch to a smaller layer after the first frame, which makes the
    configured WORKER_VIDEO_WIDTH/HEIGHT appear to be ignored. Setting false
    explicitly preserves one stable-resolution stream.
    """

    return rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        video_encoding=rtc.VideoEncoding(
            max_framerate=settings.video_fps,
            max_bitrate=settings.video_bitrate,
        ),
        simulcast=False,
    )


def _audio_frame(
    sample_rate: int,
    start_sample: int,
    samples_per_channel: int,
    debug_enabled: bool,
    sync_interval: float,
    sync_duration: float,
) -> rtc.AudioFrame:
    pcm = bytearray(samples_per_channel * 2)
    if debug_enabled and sync_interval > 0:
        peak = int(32_767 * 0.25)
        for offset in range(samples_per_channel):
            sample_number = start_sample + offset
            media_time = sample_number / sample_rate
            if media_time % sync_interval < sync_duration:
                value = int(peak * math.sin(2 * math.pi * 1_000 * media_time))
                struct.pack_into("<h", pcm, offset * 2, value)
    return rtc.AudioFrame(
        data=pcm,
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=samples_per_channel,
    )
