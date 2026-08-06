import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class MediaClock:
    sync_debug_enabled: bool = False
    sync_interval_seconds: float = 2.0
    sync_pulse_duration_seconds: float = 0.1
    started_at: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return max(0, time.monotonic() - self.started_at)

    def video_pts(self) -> int:
        return int(self.elapsed() * 90_000)

    def audio_pts(self, sample_rate: int) -> int:
        return int(self.elapsed() * sample_rate)

    def is_sync_pulse(self, elapsed_seconds: float | None = None) -> bool:
        if not self.sync_debug_enabled or self.sync_interval_seconds <= 0:
            return False
        elapsed = self.elapsed() if elapsed_seconds is None else elapsed_seconds
        return elapsed % self.sync_interval_seconds < self.sync_pulse_duration_seconds

