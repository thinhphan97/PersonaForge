import asyncio
import contextlib
import logging
import math
import struct

from av import VideoFrame as AvVideoFrame
from livekit import rtc

from personaforge_chat.adapters.outbound.media.recorded_source import RecordedCharacterCatalog
from personaforge_chat.adapters.outbound.media.switchable_track import (
    draw_debug_overlays_rgba,
    draw_frame_number,
    draw_sync_flash,
)
from personaforge_chat.domain.models import AvatarState
from personaforge_worker.adapters.outbound.queued_media import (
    QueuedMediaPublisher,
    acknowledge,
    reject,
)
from personaforge_worker.adapters.outbound.recorded_lipsync import (
    RecordedClipLipSyncRuntime,
)
from personaforge_worker.adapters.outbound.scripted_media import (
    PcmS16MonoCanonicalizer,
    ScriptedToneTtsRuntime,
)
from personaforge_worker.application.audio_timeline import BoundedAudioTimeline
from personaforge_worker.application.speech_pipeline import CompositeSpeechPipeline
from personaforge_worker.domain.media import SpeechSegment, TimedVideoFrame
from personaforge_worker.recorded_frames import LoopingRecordedFrameSource
from personaforge_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


class LiveKitRenderSession:
    """One stable LiveKit publisher with a synchronized audio/video timeline."""

    def __init__(
        self,
        session_id: str,
        character_id: str,
        livekit_url: str,
        publisher_token: str,
        settings: WorkerSettings,
    ) -> None:
        self.session_id = session_id
        self.character_id = character_id
        self._livekit_url = livekit_url
        self._publisher_token = publisher_token
        self._settings = settings
        self._state = AvatarState.IDLE
        self._state_version = -1
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self._startup_error: Exception | None = None
        self._media_publisher = QueuedMediaPublisher()
        character_clips = RecordedCharacterCatalog(
            settings.characters_root,
            settings.avatar_clips,
        ).clips_for(character_id)
        self._speech_pipeline = CompositeSpeechPipeline(
            ScriptedToneTtsRuntime(
                sample_rate=settings.audio_sample_rate,
                frame_samples=round(
                    settings.audio_sample_rate * settings.audio_frame_duration_seconds
                ),
                milliseconds_per_character=settings.scripted_tts_ms_per_character,
            ),
            PcmS16MonoCanonicalizer(sample_rate=settings.audio_sample_rate),
            BoundedAudioTimeline(
                maximum_buffer_samples=round(
                    settings.audio_sample_rate * settings.audio_buffer_max_ms / 1_000
                )
            ),
            RecordedClipLipSyncRuntime(
                LoopingRecordedFrameSource(
                    character_clips,
                    settings.video_width,
                    settings.video_height,
                )
            ),
            self._media_publisher,
            video_fps=settings.video_fps,
            sample_rate=settings.audio_sample_rate,
        )
        self._pipeline_started = False
        self._pipeline_stopped = False
        self._speech_lock = asyncio.Lock()
        self._observed_fps = 0.0
        self._av_drift_ms = 0.0

    def media_metrics(self) -> tuple[float, float]:
        return self._observed_fps, self._av_drift_ms

    def set_state(self, state: AvatarState, state_version: int) -> None:
        if state not in {
            AvatarState.IDLE,
            AvatarState.THINKING,
            AvatarState.SPEAKING,
        }:
            raise ValueError(f"Unsupported render state: {state.value}")
        if state_version <= self._state_version:
            logger.info(
                "Ignoring stale avatar state session=%s version=%s current_version=%s",
                self.session_id,
                state_version,
                self._state_version,
            )
            return
        self._state = state
        self._state_version = state_version
        logger.info(
            "Avatar state session=%s state=%s version=%s",
            self.session_id,
            state.value,
            state_version,
        )

    async def stop(self) -> None:
        if self._pipeline_started and not self._pipeline_stopped:
            await self._speech_pipeline.stop()
            self._pipeline_stopped = True
        self._stop.set()

    async def speak(
        self,
        turn_id: str,
        utterance_id: str,
        text: str,
        sequence: int,
        is_final: bool,
    ) -> None:
        if not text.strip():
            raise ValueError("Speech text cannot be empty")
        async with self._speech_lock:
            await self._speech_pipeline.speak(
                SpeechSegment(
                    session_id=self.session_id,
                    turn_id=turn_id,
                    utterance_id=utterance_id,
                    sequence=sequence,
                    text=text,
                    is_final=is_final,
                )
            )
            await self._speech_pipeline.wait_playback(utterance_id)

    async def wait_ready(self, timeout_seconds: float = 15) -> None:
        ready = asyncio.create_task(self._ready.wait())
        finished = asyncio.create_task(self._finished.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready, finished},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready in done and self._ready.is_set():
                return
            if self._startup_error is not None:
                raise RuntimeError("LiveKit publisher failed to start") from self._startup_error
            if finished in done:
                raise RuntimeError("LiveKit publisher stopped before becoming ready")
            raise TimeoutError(f"LiveKit publisher was not ready after {timeout_seconds}s")
        finally:
            ready.cancel()
            finished.cancel()
            await asyncio.gather(ready, finished, return_exceptions=True)

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
            RecordedCharacterCatalog(
                settings.characters_root,
                settings.avatar_clips,
            ).clips_for(self.character_id),
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
            await self._speech_pipeline.start()
            self._pipeline_started = True
            await self._speech_pipeline.wait_ready()
            first_frame = await asyncio.to_thread(
                recorded.next_frame,
                AvatarState.IDLE,
                None,
                self._is_sync_pulse(0),
            )
            self._ready.set()
            logger.info(
                "LiveKit media ready session=%s character=%s",
                self.session_id,
                self.character_id,
            )
            await self._publish_timeline(synchronizer, recorded, first_frame)
        except Exception as exc:
            self._startup_error = exc
            raise
        finally:
            self._finished.set()
            if self._pipeline_started and not self._pipeline_stopped:
                self._media_publisher.abort(
                    RuntimeError("LiveKit publishing loop stopped")
                )
                with contextlib.suppress(Exception):
                    await self._speech_pipeline.stop()
                self._pipeline_stopped = True
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
        first_frame: AvVideoFrame,
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
                video_delivery = self._media_publisher.pop_video()
                try:
                    sync_pulse = self._is_sync_pulse(next_video_time)
                    if video_delivery is not None:
                        video_frame = _to_livekit_timed_video_frame(
                            video_delivery.frame,
                            video_frame_number + 1,
                            sync_pulse,
                        )
                    elif video_frame_number == 0:
                        output = first_frame
                        output = draw_frame_number(output, video_frame_number + 1)
                        if sync_pulse:
                            output = draw_sync_flash(output)
                        video_frame = _to_livekit_video_frame(output)
                    else:
                        output = await asyncio.to_thread(
                            recorded.next_frame,
                            self._state,
                            None,
                            False,
                        )
                        output = draw_frame_number(output, video_frame_number + 1)
                        if sync_pulse:
                            output = draw_sync_flash(output)
                        video_frame = _to_livekit_video_frame(output)
                    await synchronizer.push(video_frame, timestamp=next_video_time)
                except BaseException as exc:
                    if video_delivery is not None:
                        reject(video_delivery, exc)
                    raise
                if video_delivery is not None:
                    acknowledge(video_delivery)
                video_frame_number += 1
                next_video_time = video_frame_number / settings.video_fps

            audio_delivery = self._media_publisher.pop_audio()
            audio_frame = _audio_frame(
                sample_rate=settings.audio_sample_rate,
                start_sample=audio_sample,
                samples_per_channel=samples_per_audio_frame,
                debug_enabled=settings.av_sync_debug,
                sync_interval=settings.av_sync_interval_seconds,
                sync_duration=settings.av_sync_pulse_duration_seconds,
                pcm=(
                    audio_delivery.frame.samples
                    if audio_delivery is not None
                    else None
                ),
            )
            audio_sample += samples_per_audio_frame
            try:
                await synchronizer.push(
                    audio_frame,
                    timestamp=audio_sample / settings.audio_sample_rate,
                )
            except BaseException as exc:
                if audio_delivery is not None:
                    reject(audio_delivery, exc)
                raise
            if audio_delivery is not None:
                acknowledge(audio_delivery)
            media_time = audio_sample / settings.audio_sample_rate
            if media_time >= next_metrics_at:
                drift_ms = (
                    synchronizer.last_video_time - synchronizer.last_audio_time
                ) * 1_000
                self._observed_fps = synchronizer.actual_fps
                self._av_drift_ms = drift_ms
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


def _to_livekit_timed_video_frame(
    frame: TimedVideoFrame,
    frame_number: int,
    sync_flash: bool,
) -> rtc.VideoFrame:
    expected = frame.width * frame.height * 4
    if len(frame.data) != expected:
        raise ValueError("Lip-sync RGBA frame does not match its declared resolution")
    decorated = draw_debug_overlays_rgba(
        frame.data,
        frame.width,
        frame.height,
        frame_number,
        sync_flash,
    )
    return rtc.VideoFrame(
        frame.width,
        frame.height,
        rtc.VideoBufferType.RGBA,
        decorated,
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
    pcm: bytes | None = None,
) -> rtc.AudioFrame:
    if pcm is not None and len(pcm) != samples_per_channel * 2:
        raise ValueError("TTS PCM frame does not match configured audio cadence")
    frame_data = bytearray(pcm) if pcm is not None else bytearray(samples_per_channel * 2)
    if pcm is None and debug_enabled and sync_interval > 0:
        peak = int(32_767 * 0.25)
        for offset in range(samples_per_channel):
            sample_number = start_sample + offset
            media_time = sample_number / sample_rate
            if media_time % sync_interval < sync_duration:
                value = int(peak * math.sin(2 * math.pi * 1_000 * media_time))
                struct.pack_into("<h", frame_data, offset * 2, value)
    return rtc.AudioFrame(
        data=frame_data,
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=samples_per_channel,
    )
