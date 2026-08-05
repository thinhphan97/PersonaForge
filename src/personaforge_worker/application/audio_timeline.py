import asyncio
from collections import deque
from dataclasses import replace

from personaforge_worker.domain.media import CanonicalAudioChunk


class AudioTimelineClosed(RuntimeError):
    pass


class BoundedAudioTimeline:
    """Single-writer, multi-reader bounded PCM timeline with independent cursors."""

    def __init__(self, maximum_buffer_samples: int = 96_000) -> None:
        if maximum_buffer_samples <= 0:
            raise ValueError("Audio timeline capacity must be positive")
        self._maximum_buffer_samples = maximum_buffer_samples
        self._chunks: deque[tuple[int, CanonicalAudioChunk]] = deque()
        self._next_sequence = 0
        self._next_sample = 0
        self._readers: dict[str, int] = {}
        self._closed = False
        self._condition = asyncio.Condition()

    def reader(self, consumer: str) -> "AudioTimelineReader":
        if not consumer or consumer in self._readers:
            raise ValueError(f"Audio timeline consumer already exists: {consumer}")
        self._readers[consumer] = self._next_sequence
        return AudioTimelineReader(self, consumer)

    async def append(self, chunk: CanonicalAudioChunk) -> CanonicalAudioChunk:
        async with self._condition:
            if self._closed:
                raise AudioTimelineClosed("Cannot append to a closed audio timeline")
            if not self._readers:
                raise RuntimeError("Audio timeline requires at least one reader")
            sample_count = chunk.samples_per_channel
            if sample_count <= 0 or sample_count > self._maximum_buffer_samples:
                raise ValueError("Invalid canonical audio chunk size")
            await self._condition.wait_for(
                lambda: self._closed
                or self._buffered_samples() + sample_count
                <= self._maximum_buffer_samples
            )
            if self._closed:
                raise AudioTimelineClosed("Audio timeline closed during append")
            assigned = replace(chunk, start_sample=self._next_sample)
            self._chunks.append((self._next_sequence, assigned))
            self._next_sequence += 1
            self._next_sample += sample_count
            self._condition.notify_all()
            return assigned

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def _read(self, consumer: str) -> CanonicalAudioChunk | None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._closed or self._has_chunk(consumer)
            )
            sequence = self._readers.get(consumer)
            if sequence is None:
                return None
            if not self._has_chunk(consumer):
                return None
            first_sequence = self._chunks[0][0]
            chunk = self._chunks[sequence - first_sequence][1]
            self._readers[consumer] = sequence + 1
            self._prune()
            self._condition.notify_all()
            return chunk

    async def _close_reader(self, consumer: str) -> None:
        async with self._condition:
            self._readers.pop(consumer, None)
            self._prune()
            self._condition.notify_all()

    def _has_chunk(self, consumer: str) -> bool:
        sequence = self._readers.get(consumer)
        return sequence is not None and sequence < self._next_sequence

    def _prune(self) -> None:
        minimum = min(self._readers.values(), default=self._next_sequence)
        while self._chunks and self._chunks[0][0] < minimum:
            self._chunks.popleft()

    def _buffered_samples(self) -> int:
        return sum(chunk.samples_per_channel for _sequence, chunk in self._chunks)


class AudioTimelineReader:
    def __init__(self, timeline: BoundedAudioTimeline, consumer: str) -> None:
        self._timeline = timeline
        self._consumer = consumer
        self._closed = False

    async def read(self) -> CanonicalAudioChunk | None:
        if self._closed:
            return None
        return await self._timeline._read(self._consumer)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._timeline._close_reader(self._consumer)
