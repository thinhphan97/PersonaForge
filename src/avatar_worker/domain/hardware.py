from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GpuHardwareSnapshot:
    uuid: str
    name: str
    compute_capability: str
    total_vram_mb: int
    free_vram_mb: int
    used_vram_mb: int
    gpu_utilization: float
    memory_utilization: float
    temperature_c: float | None
    power_watts: float | None
    source: str
    sampled_at: datetime
    probe_error: str | None = None
