"""Media publisher test double; never used by production composition roots."""

from avatar_worker.domain.media import CanonicalAudioChunk, TimedVideoFrame


class RecordingMediaPublisher:
    def __init__(self) -> None:
        self.audio: list[CanonicalAudioChunk] = []
        self.video: list[TimedVideoFrame] = []
        self.closed = False

    async def publish_audio(self, frame: CanonicalAudioChunk) -> None:
        self.audio.append(frame)

    async def publish_video(self, frame: TimedVideoFrame) -> None:
        self.video.append(frame)

    async def close(self) -> None:
        self.closed = True
