import asyncio
from dataclasses import replace

from personaforge_worker.application.media_ports import (
    AudioCanonicalizerPort,
    AudioTimelinePort,
    AudioTimelineReaderPort,
    LipSyncSessionRuntimePort,
    MediaPublisherPort,
    TtsSessionRuntimePort,
)
from personaforge_worker.domain.avatar import AudioWindow, MotionFrameFlags, TimedMotionFrame
from personaforge_worker.domain.media import (
    CanonicalAudioChunk,
    LipSyncRenderRequest,
    SilenceSegment,
    SpeechSegment,
)


class CompositeSpeechPipeline:
    """Stream TTS PCM once into independent audio-publish and lip-sync readers."""

    def __init__(
        self,
        tts: TtsSessionRuntimePort,
        canonicalizer: AudioCanonicalizerPort,
        timeline: AudioTimelinePort,
        lipsync: LipSyncSessionRuntimePort,
        publisher: MediaPublisherPort,
        video_fps: float,
        sample_rate: int = 48_000,
        avatar_runtime: object | None = None,
    ) -> None:
        if video_fps <= 0 or sample_rate <= 0:
            raise ValueError("Media cadence must be positive")
        self._tts = tts
        self._canonicalizer = canonicalizer
        self._timeline = timeline
        self._lipsync = lipsync
        self._publisher = publisher
        self._video_fps = video_fps
        self._sample_rate = sample_rate
        self._avatar_runtime = avatar_runtime
        self._speech: asyncio.Queue[
            SpeechSegment | SilenceSegment | None
        ] = asyncio.Queue(maxsize=100)
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._ready = asyncio.Event()
        self._stopped = False
        self._audio_done: set[str] = set()
        self._video_done: set[str] = set()
        self._playback_events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        if self._tasks:
            return
        audio_reader = self._timeline.reader("media-publisher")
        lipsync_reader = self._timeline.reader("lip-sync")
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(self._run_tts(), name="persona-tts"),
            asyncio.create_task(
                self._publish_audio(audio_reader),
                name="persona-audio-publisher",
            ),
            asyncio.create_task(
                self._render_video(lipsync_reader),
                name="persona-lipsync",
            ),
        ]
        if self._avatar_runtime is not None:
            motion_reader = self._timeline.reader("avatar-motion")
            tasks.append(
                asyncio.create_task(
                    self.render_motion(motion_reader),
                    name="persona-avatar-motion",
                )
            )
        self._tasks = tuple(tasks)
        self._ready.set()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def speak(self, segment: SpeechSegment) -> None:
        if not self._tasks or self._stopped:
            raise RuntimeError("Composite speech pipeline is not running")
        if not segment.text.strip():
            raise ValueError("Speech segment text cannot be empty")
        self._playback_events.setdefault(segment.utterance_id, asyncio.Event())
        await self._speech.put(segment)

    async def append_silence(
        self,
        *,
        session_id: str,
        turn_id: str,
        utterance_id: str,
        duration_ms: int = 500,
    ) -> None:
        """Queue silence without bypassing the single-writer timeline invariant."""
        if not self._tasks or self._stopped:
            raise RuntimeError("Composite speech pipeline is not running")
        await self._speech.put(
            SilenceSegment(
                session_id=session_id,
                turn_id=turn_id,
                utterance_id=utterance_id,
                duration_ms=max(20, duration_ms),
                sample_rate=self._sample_rate,
            )
        )

    async def wait_playback(self, utterance_id: str, timeout: float | None = None) -> None:
        event = self._playback_events.setdefault(utterance_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=timeout)
        self._playback_events.pop(utterance_id, None)
        self._audio_done.discard(utterance_id)
        self._video_done.discard(utterance_id)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._tasks:
            await self._speech.put(None)
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = ()
            error = next((item for item in results if isinstance(item, BaseException)), None)
        else:
            error = None
            await self._timeline.close()
        await self._lipsync.close()
        if self._avatar_runtime is not None:
            await self._avatar_runtime.close()
        await self._tts.close()
        await self._publisher.close()
        if error is not None:
            raise error

    async def _run_tts(self) -> None:
        try:
            while True:
                segment = await self._speech.get()
                if segment is None:
                    return
                if isinstance(segment, SilenceSegment):
                    frame_samples = round(self._sample_rate * 0.02)
                    total_samples = max(
                        frame_samples,
                        round(segment.duration_ms / 1000 * self._sample_rate / frame_samples)
                        * frame_samples,
                    )
                    zeroes = b"\x00" * (frame_samples * 2)
                    for sequence, _start in enumerate(range(0, total_samples, frame_samples)):
                        await self._timeline.append(
                            CanonicalAudioChunk(
                                session_id=segment.session_id,
                                turn_id=segment.turn_id,
                                utterance_id=segment.utterance_id,
                                sequence=sequence,
                                start_sample=0,
                                samples=zeroes,
                                sample_rate=self._sample_rate,
                                channels=1,
                                end_of_utterance=sequence == total_samples // frame_samples - 1,
                                is_silence=True,
                            )
                        )
                    continue
                async for native in self._tts.synthesize(segment):
                    for canonical in self._canonicalizer.convert(segment, native):
                        await self._timeline.append(canonical)
        finally:
            await self._timeline.close()

    async def _publish_audio(self, reader: AudioTimelineReaderPort) -> None:
        try:
            while True:
                chunk = await reader.read()
                if chunk is None:
                    return
                await self._publisher.publish_audio(chunk)
                if chunk.end_of_utterance:
                    self._audio_done.add(chunk.utterance_id)
                    self._complete_if_drained(chunk.utterance_id)
        finally:
            await reader.close()

    async def _render_video(self, reader: AudioTimelineReaderPort) -> None:
        samples_per_video_frame = round(self._sample_rate / self._video_fps)
        next_video_sample = 0
        frame_index = 0
        try:
            while True:
                chunk = await reader.read()
                if chunk is None:
                    return
                while next_video_sample < chunk.end_sample:
                    if next_video_sample >= chunk.start_sample:
                        frame = await self._lipsync.render(
                            LipSyncRenderRequest(
                                session_id=chunk.session_id,
                                utterance_id=chunk.utterance_id,
                                frame_index=frame_index,
                                target_pts_ns=round(
                                    next_video_sample / self._sample_rate * 1_000_000_000
                                ),
                                audio_start_sample=chunk.start_sample,
                                audio_end_sample=chunk.end_sample,
                                audio_samples=chunk.samples,
                                sample_rate=chunk.sample_rate,
                            )
                        )
                        await self._publisher.publish_video(frame)
                        frame_index += 1
                    next_video_sample += samples_per_video_frame
                if chunk.end_of_utterance:
                    self._video_done.add(chunk.utterance_id)
                    self._complete_if_drained(chunk.utterance_id)
        finally:
            await reader.close()

    async def render_motion(self, reader: AudioTimelineReaderPort) -> None:
        """Forward canonical timeline windows to a client-render avatar runtime."""
        if self._avatar_runtime is None:
            return
        motion_fps = int(
            getattr(getattr(self._avatar_runtime, "_settings", None), "motion_fps", 30)
        )
        motion_schema = str(
            getattr(getattr(self._avatar_runtime, "_settings", None), "schema", "arkit52-v1")
        )
        motion_samples = max(1, round(self._sample_rate / motion_fps))
        next_motion_sample = 0
        motion_sequence = 0
        try:
            while True:
                chunk = await reader.read()
                if chunk is None:
                    return
                if chunk.is_silence or not any(chunk.samples):
                    # Idle/thinking silence keeps the shared audio clock alive,
                    # but must not spend GPU time in LAM-A2E. Emit neutral
                    # expression frames directly instead.
                    while next_motion_sample < chunk.end_sample:
                        if next_motion_sample >= chunk.start_sample:
                            await self._publisher.publish_motion(
                                TimedMotionFrame(
                                    session_id=chunk.session_id,
                                    utterance_id=chunk.utterance_id,
                                    generation=0,
                                    sequence=motion_sequence,
                                    pts_sample=next_motion_sample,
                                    duration_samples=motion_samples,
                                    sample_rate=self._sample_rate,
                                    schema=motion_schema,
                                    values=(0.0,) * 52,
                                    flags=(
                                        MotionFrameFlags.END_OF_UTTERANCE
                                        if chunk.end_of_utterance
                                        and next_motion_sample + motion_samples >= chunk.end_sample
                                        else MotionFrameFlags.NONE
                                    ),
                                )
                            )
                            motion_sequence += 1
                        next_motion_sample += motion_samples
                    continue
                window = AudioWindow(
                    session_id=chunk.session_id,
                    utterance_id=chunk.utterance_id,
                    start_sample=chunk.start_sample,
                    samples=chunk.samples,
                    sample_rate=chunk.sample_rate,
                    channels=chunk.channels,
                )
                async for frame in self._avatar_runtime.process(window):
                    await self._publisher.publish_motion(
                        replace(frame, sequence=motion_sequence)
                    )
                    motion_sequence += 1
                next_motion_sample = max(next_motion_sample, chunk.end_sample)
                if chunk.end_of_utterance:
                    async for frame in self._avatar_runtime.flush(chunk.utterance_id):
                        await self._publisher.publish_motion(
                            replace(frame, sequence=motion_sequence)
                        )
                        motion_sequence += 1
        finally:
            await reader.close()

    def _complete_if_drained(self, utterance_id: str) -> None:
        if utterance_id in self._audio_done and utterance_id in self._video_done:
            self._playback_events.setdefault(utterance_id, asyncio.Event()).set()
