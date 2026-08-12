"""LAM Audio2Expression adapter.

This module is the only worker adapter allowed to know the upstream A2E API. The model engine is
loaded lazily in the Composite Child, after the scheduler has assigned a GPU.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from personaforge_worker.adapters.outbound.avatar.checkpoints import ensure_huggingface_snapshot
from personaforge_worker.domain.avatar import (
    AudioWindow,
    AvatarOutputKind,
    ExpressionFrame,
    TimedMotionFrame,
)


@dataclass(frozen=True, slots=True)
class LamA2ESettings:
    model_repo: str = "3DAIGC/LAM_audio2exp"
    model_revision: str = "0fe5f4dbb283ec7d9c01688681e6e4b6ac314858"
    cache_dir: Path = Path("/var/cache/personaforge/lam")
    input_sample_rate: int = 16_000
    model_fps: int = 30
    publish_fps: int = 30
    inference_chunk_ms: int = 100
    schema: str = "arkit52-v1"
    generation: int = 0
    allow_download: bool = True
    source_dir: Path = Path("/app/third_party/LAM_Audio2Expression")
    identity_index: int = 0
    device: str = "cuda"


class LamAudio2ExpressionRuntime:
    """Public PersonaForge runtime facade for the pinned LAM-A2E model.

    The upstream inference call is intentionally isolated behind ``_engine``. A concrete engine
    bridge can be added after the checkpoint/API smoke test without changing pipeline contracts.
    """

    output_kind = AvatarOutputKind.MOTION

    def __init__(self, engine: object, settings: LamA2ESettings) -> None:
        self._engine = engine
        self._settings = settings
        self._closed = False
        self._sequence = 0
        self._last_audio_window: AudioWindow | None = None

    @classmethod
    async def create(
        cls,
        settings: LamA2ESettings,
        *,
        warmup: bool = True,
    ) -> LamAudio2ExpressionRuntime:
        if settings.model_fps != 30:
            raise ValueError(
                "LAM-A2E upstream runtime supports 30 FPS; use a model-specific "
                "adapter for another output FPS"
            )
        checkpoint = await ensure_huggingface_snapshot(
            repo_id=settings.model_repo,
            revision=settings.model_revision,
            cache_dir=settings.cache_dir,
            allow_download=settings.allow_download,
        )
        engine = await cls._load_engine(checkpoint, settings)
        runtime = cls(engine, settings)
        if warmup:
            await runtime.warmup()
        return runtime

    @staticmethod
    async def _load_engine(checkpoint: Path, settings: LamA2ESettings) -> object:
        """Load the pinned engine through an optional bridge supplied with the model image."""
        try:
            from personaforge_worker.adapters.outbound.avatar.lam_a2e_engine import (
                load_engine,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "LAM-A2E checkpoint is ready but the pinned engine bridge is not installed; "
                "implement lam_a2e_engine.load_engine before enabling this runtime"
            ) from exc
        return await load_engine(checkpoint, settings)

    async def warmup(self) -> None:
        warmup = getattr(self._engine, "warmup", None)
        if callable(warmup):
            result = warmup()
            if hasattr(result, "__await__"):
                await result

    async def process(self, audio: AudioWindow) -> AsyncIterator[TimedMotionFrame]:
        if self._closed:
            raise RuntimeError("LAM-A2E runtime is closed")
        self._last_audio_window = audio
        native = cast(Any, self._engine).process
        async for frame in native(audio):
            yield self._to_timed_frame(audio, frame)

    async def flush(self, utterance_id: str) -> AsyncIterator[TimedMotionFrame]:
        if self._closed:
            raise RuntimeError("LAM-A2E runtime is closed")
        flush = getattr(self._engine, "flush", None)
        if flush is not None:
            source = self._last_audio_window or AudioWindow(
                utterance_id=utterance_id,
                session_id="",
                start_sample=0,
                samples=b"",
            )
            async for frame in flush(utterance_id):
                yield self._to_timed_frame(source, frame)
        reset = getattr(self._engine, "reset_stream", None)
        if callable(reset):
            reset()
        self._last_audio_window = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._engine, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    def reset_stream(self) -> None:
        """Reset context for state silence/discontinuity without closing the model."""
        reset = getattr(self._engine, "reset_stream", None)
        if callable(reset):
            reset()
        self._last_audio_window = None

    def _to_timed_frame(self, audio: AudioWindow, frame: ExpressionFrame) -> TimedMotionFrame:
        sequence = self._sequence
        self._sequence += 1
        return TimedMotionFrame(
            session_id=audio.session_id,
            utterance_id=audio.utterance_id,
            generation=self._settings.generation,
            sequence=sequence,
            pts_sample=frame.pts_sample,
            duration_samples=frame.duration_samples,
            sample_rate=audio.sample_rate,
            schema=frame.schema or self._settings.schema,
            values=tuple(float(value) for value in frame.values),
            flags=frame.flags,
        )
