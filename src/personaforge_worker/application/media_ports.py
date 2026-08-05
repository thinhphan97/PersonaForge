from collections.abc import AsyncIterator, Iterable
from typing import Protocol

from personaforge_worker.domain.media import (
    CanonicalAudioChunk,
    LipSyncRenderRequest,
    NativeAudioChunk,
    SpeechSegment,
    TimedVideoFrame,
)


class TtsSessionRuntimePort(Protocol):
    def synthesize(self, segment: SpeechSegment) -> AsyncIterator[NativeAudioChunk]: ...
    async def close(self) -> None: ...


class AudioCanonicalizerPort(Protocol):
    def convert(
        self,
        segment: SpeechSegment,
        chunk: NativeAudioChunk,
    ) -> Iterable[CanonicalAudioChunk]: ...


class AudioTimelineReaderPort(Protocol):
    async def read(self) -> CanonicalAudioChunk | None: ...
    async def close(self) -> None: ...


class AudioTimelinePort(Protocol):
    async def append(self, chunk: CanonicalAudioChunk) -> CanonicalAudioChunk: ...
    def reader(self, consumer: str) -> AudioTimelineReaderPort: ...
    async def close(self) -> None: ...


class LipSyncSessionRuntimePort(Protocol):
    async def render(self, request: LipSyncRenderRequest) -> TimedVideoFrame: ...
    async def close(self) -> None: ...


class MediaPublisherPort(Protocol):
    async def publish_audio(self, frame: CanonicalAudioChunk) -> None: ...
    async def publish_video(self, frame: TimedVideoFrame) -> None: ...
    async def close(self) -> None: ...
