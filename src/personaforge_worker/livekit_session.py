import asyncio
import contextlib
import json
import logging
import math
import struct
import time
from collections import deque

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
from personaforge_worker.application.media_ports import TtsSessionRuntimePort
from personaforge_worker.application.speech_pipeline import CompositeSpeechPipeline
from personaforge_worker.domain.avatar import TimedMotionFrame
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
        tts_runtime: TtsSessionRuntimePort | None = None,
        avatar_runtime: object | None = None,
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
            tts_runtime
            or ScriptedToneTtsRuntime(
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
            avatar_runtime=avatar_runtime,
        )
        self._pipeline_started = False
        self._pipeline_stopped = False
        self._state_audio_task: asyncio.Task[None] | None = None
        self._silence_sequence = 0
        self._speech_lock = asyncio.Lock()
        self._observed_fps = 0.0
        self._motion_observed_fps = 0.0
        self._motion_target_fps = (
            float(settings.lam_a2e_publish_fps) if avatar_runtime is not None else 0.0
        )
        self._motion_base_pts: int | None = None
        self._motion_base_wall: float | None = None
        self._motion_send_times: deque[float] = deque(maxlen=61)
        self._av_drift_ms = 0.0
        self._last_motion_log_at = 0.0
        self._media_log_interval = max(
            1.0,
            settings.media_metrics_log_interval_seconds,
        )

    def media_metrics(self) -> tuple[float, float]:
        return (
            self._motion_target_fps
            or self._motion_observed_fps
            or self._observed_fps,
            self._av_drift_ms,
        )

    def set_state(self, state: AvatarState, state_version: int) -> None:
        if state not in {
            AvatarState.IDLE,
            AvatarState.THINKING,
            AvatarState.SPEAKING,
        }:
            raise ValueError(f"Unsupported render state: {state.value}")
        if state_version <= self._state_version:
            # State updates are replayed by the control plane during reconnects;
            # stale versions are expected and should not flood INFO logs.
            logger.debug(
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
        if self._pipeline_started and not self._pipeline_stopped:
            self._restart_state_audio()

    async def stop(self) -> None:
        if self._state_audio_task is not None:
            self._state_audio_task.cancel()
            await asyncio.gather(self._state_audio_task, return_exceptions=True)
            self._state_audio_task = None
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
            audio_track = rtc.LocalAudioTrack.create_audio_track("avatar-audio", audio_source)
            video_track = rtc.LocalVideoTrack.create_video_track("avatar-video", video_source)
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
            self._restart_state_audio()
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
            motion_task = asyncio.create_task(
                self._publish_motion(room),
                name=f"persona-motion-publisher-{self.session_id}",
            )
            try:
                await self._publish_timeline(room, synchronizer, recorded, first_frame)
            finally:
                motion_task.cancel()
                await asyncio.gather(motion_task, return_exceptions=True)
        except Exception as exc:
            self._startup_error = exc
            raise
        finally:
            self._finished.set()
            if self._pipeline_started and not self._pipeline_stopped:
                self._media_publisher.abort(RuntimeError("LiveKit publishing loop stopped"))
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
        room: rtc.Room,
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
        active_audio_utterance: str | None = None
        audio_underrun_frames = 0
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
                    # Let the bounded producer enqueue the next frame before the
                    # non-blocking media-clock loop polls again. Without this
                    # handoff, every other slot can incorrectly fall back to a
                    # recorded frame even though lip-sync output is ready.
                    await asyncio.sleep(0)
                video_frame_number += 1
                next_video_time = video_frame_number / settings.video_fps

            audio_delivery = self._media_publisher.pop_audio()
            if audio_delivery is not None and active_audio_utterance is None:
                active_audio_utterance = audio_delivery.frame.utterance_id
                audio_underrun_frames = 0
            elif audio_delivery is None and active_audio_utterance is not None:
                audio_underrun_frames += 1
            audio_frame = _audio_frame(
                sample_rate=settings.audio_sample_rate,
                start_sample=audio_sample,
                samples_per_channel=samples_per_audio_frame,
                debug_enabled=settings.av_sync_debug,
                sync_interval=settings.av_sync_interval_seconds,
                sync_duration=settings.av_sync_pulse_duration_seconds,
                pcm=(audio_delivery.frame.samples if audio_delivery is not None else None),
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
                if audio_delivery.frame.end_of_utterance:
                    if not audio_delivery.frame.is_silence:
                        logger.info(
                            "LiveKit utterance playback drained",
                            extra={
                                "event": "worker.media.audio.playback.completed",
                                "utterance_id": audio_delivery.frame.utterance_id,
                                "audio_underrun_frames": audio_underrun_frames,
                                "audio_underrun_ms": round(
                                    audio_underrun_frames
                                    * settings.audio_frame_duration_seconds
                                    * 1_000,
                                    2,
                                ),
                            },
                        )
                    active_audio_utterance = None
                    audio_underrun_frames = 0
                # ``publish_audio`` waits for this acknowledgement before it can
                # enqueue the following 20 ms PCM frame. Yielding here prevents
                # the next non-blocking poll from inserting a false silence frame.
                await asyncio.sleep(0)
            media_time = audio_sample / settings.audio_sample_rate
            if media_time >= next_metrics_at:
                drift_ms = (synchronizer.last_video_time - synchronizer.last_audio_time) * 1_000
                self._observed_fps = synchronizer.actual_fps
                self._av_drift_ms = drift_ms
                logger.info(
                    "Media cadence session=%s fps=%.2f av_drift_ms=%.2f",
                    self.session_id,
                    synchronizer.actual_fps,
                    drift_ms,
                    extra={
                        "event": "worker.media.cadence",
                        "session_id": self.session_id,
                        "media_fps": round(synchronizer.actual_fps, 2),
                        "av_drift_ms": round(drift_ms, 2),
                    },
                )
                next_metrics_at += self._media_log_interval

    async def _publish_motion(self, room: rtc.Room) -> None:
        """Publish lossy motion independently of the audio media clock."""
        while not self._stop.is_set():
            delivery = self._media_publisher.pop_motion()
            if delivery is None:
                await asyncio.sleep(0.001)
                continue
            frame = delivery.frame
            try:
                if self._motion_base_pts is None:
                    self._motion_base_pts = frame.pts_sample
                    self._motion_base_wall = time.monotonic()
                assert self._motion_base_wall is not None
                target_wall = self._motion_base_wall + (
                    frame.pts_sample - self._motion_base_pts
                ) / max(1, frame.sample_rate)
                delay = target_wall - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await room.local_participant.publish_data(
                    _encode_motion_frame(frame),
                    reliable=False,
                    topic="avatar-motion",
                )
                acknowledge(delivery)
                now = time.monotonic()
                self._motion_send_times.append(now)
                if len(self._motion_send_times) >= 2:
                    measured = (
                        (len(self._motion_send_times) - 1)
                        / max(0.001, self._motion_send_times[-1] - self._motion_send_times[0])
                    )
                    configured = frame.sample_rate / max(1, frame.duration_samples)
                    self._motion_observed_fps = min(measured, configured)
                # Motion is published at video cadence (25/30 FPS). Logging every
                # Nth frame created a continuous one-line-per-second stream per
                # session, which is not useful in production. Keep the first
                # packet and emit a periodic health sample instead.
                if (
                    frame.sequence == 0
                    or now - self._last_motion_log_at
                    >= self._media_log_interval
                ):
                    self._last_motion_log_at = now
                    logger.info(
                        "Avatar motion cadence sample",
                        extra={
                            "event": "worker.avatar.motion.cadence",
                            "session_id": self.session_id,
                            "utterance_id": frame.utterance_id,
                            "motion_sequence": frame.sequence,
                            "motion_pts_sample": frame.pts_sample,
                            "motion_schema": frame.schema,
                            "motion_values": len(frame.values),
                            "motion_fps": round(self._motion_observed_fps, 2),
                        },
                    )
            except asyncio.CancelledError:
                reject(delivery, RuntimeError("Motion publisher cancelled"))
                raise
            except BaseException as exc:
                # A transient DataTrack failure must not terminate audio/video.
                reject(delivery, exc)
                logger.warning(
                    "Avatar motion packet dropped",
                    extra={
                        "event": "worker.avatar.motion.dropped",
                        "session_id": self.session_id,
                        "motion_sequence": frame.sequence,
                        "exception_type": type(exc).__name__,
                    },
                )

    def _restart_state_audio(self) -> None:
        if self._state_audio_task is not None:
            self._state_audio_task.cancel()
        if self._state in {AvatarState.IDLE, AvatarState.THINKING}:
            self._state_audio_task = asyncio.create_task(
                self._feed_state_silence(),
                name=f"persona-state-silence-{self.session_id}",
            )
        else:
            self._state_audio_task = None

    async def _feed_state_silence(self) -> None:
        try:
            while not self._stop.is_set() and self._state in {
                AvatarState.IDLE,
                AvatarState.THINKING,
            }:
                self._silence_sequence += 1
                await self._speech_pipeline.append_silence(
                    session_id=self.session_id,
                    turn_id=f"state-{self._state.value}",
                    utterance_id=f"{self.session_id}-silence-{self._silence_sequence}",
                    # Short windows keep the transition to SPEAKING responsive;
                    # any already queued silence is bounded to 100 ms.
                    duration_ms=100,
                )
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, asyncio.QueueFull):
            logger.debug("State silence feeder stopped", exc_info=True)

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


def _encode_motion_frame(frame: TimedMotionFrame) -> bytes:
    """Stable PoC wire format; replace with the versioned binary packet in production."""
    return json.dumps(
        {
            "version": 1,
            "session_id": frame.session_id,
            "utterance_id": frame.utterance_id,
            "generation": frame.generation,
            "sequence": frame.sequence,
            "pts_sample": frame.pts_sample,
            "duration_samples": frame.duration_samples,
            "sample_rate": frame.sample_rate,
            "schema": frame.schema,
            "flags": int(frame.flags),
            "values": frame.values,
        },
        separators=(",", ":"),
    ).encode("utf-8")


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
