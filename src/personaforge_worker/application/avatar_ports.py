"""Stable public APIs consumed by the PersonaForge avatar pipeline.

Implementations belong in adapters and may use Torch, CUDA or an upstream SDK. These protocols
must remain dependency-free so the CompositeSpeechPipeline can be tested with fakes.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from personaforge_worker.domain.avatar import (
    AudioWindow,
    AvatarOutputKind,
    ExpressionFrame,
    TimedMotionFrame,
)


class Audio2ExpressionEngine(Protocol):
    @property
    def sample_rate(self) -> int: ...

    @property
    def motion_fps(self) -> int: ...

    @property
    def schema(self) -> str: ...

    async def warmup(self) -> None: ...

    def process(self, audio: AudioWindow) -> AsyncIterator[ExpressionFrame]: ...

    def flush(self, utterance_id: str) -> AsyncIterator[ExpressionFrame]: ...

    async def close(self) -> None: ...


class AvatarSessionRuntimePort(Protocol):
    @property
    def output_kind(self) -> AvatarOutputKind: ...

    def process(self, audio: AudioWindow) -> AsyncIterator[TimedMotionFrame]: ...

    def flush(self, utterance_id: str) -> AsyncIterator[TimedMotionFrame]: ...

    async def close(self) -> None: ...


class AvatarOutputPublisherPort(Protocol):
    async def publish_motion(self, frame: TimedMotionFrame) -> None: ...
