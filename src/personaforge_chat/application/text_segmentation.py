import re


class StreamingTextSegmenter:
    """Turn irreversible Agent deltas into immutable TTS clauses."""

    _hard_boundary = re.compile(r"^(.+?[.!?]+)(?:\s+|$)", re.DOTALL)
    _soft_boundary = re.compile(r"^(.+?[,;:]+)(?:\s+|$)", re.DOTALL)

    def __init__(
        self,
        minimum_characters: int = 20,
        target_characters: int = 60,
        maximum_characters: int = 160,
    ) -> None:
        self._minimum = minimum_characters
        self._target = target_characters
        self._maximum = maximum_characters
        self._buffer = ""

    def feed(self, delta: str) -> tuple[str, ...]:
        self._buffer += delta
        emitted: list[str] = []
        while segment := self._next_segment():
            emitted.append(segment)
        return tuple(emitted)

    def flush(self) -> str | None:
        segment = self._buffer.strip()
        self._buffer = ""
        return segment or None

    def _next_segment(self) -> str | None:
        stripped = self._buffer.lstrip()
        self._buffer = stripped
        hard = self._hard_boundary.match(self._buffer)
        if hard:
            return self._consume(hard.end())
        soft = self._soft_boundary.match(self._buffer)
        if soft and len(soft.group(1)) >= self._minimum:
            return self._consume(soft.end())
        if len(self._buffer) < self._target:
            return None
        limit = min(len(self._buffer), self._maximum)
        boundary = self._buffer.rfind(" ", self._minimum, limit + 1)
        if boundary < self._minimum and len(self._buffer) < self._maximum:
            return None
        end = boundary + 1 if boundary >= self._minimum else limit
        return self._consume(end)

    def _consume(self, end: int) -> str:
        segment = self._buffer[:end].strip()
        self._buffer = self._buffer[end:].lstrip()
        return segment
