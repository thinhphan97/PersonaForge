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
    encoder_utilization: float | None = None
    decoder_utilization: float | None = None
    fan_speed_percent: float | None = None
    sm_clock_mhz: float | None = None
    memory_clock_mhz: float | None = None
    pcie_tx_bytes_per_second: int | None = None
    pcie_rx_bytes_per_second: int | None = None


@dataclass(frozen=True, slots=True)
class HostHardwareSnapshot:
    cpu_utilization: float | None
    cpu_count_logical: int | None
    load_average_1m: float | None
    ram_total_bytes: int | None
    ram_used_bytes: int | None
    ram_available_bytes: int | None
    swap_used_bytes: int | None


@dataclass(frozen=True, slots=True)
class ProcessHardwareSnapshot:
    pid: int
    process_create_time: float | None
    status: str | None
    cpu_utilization: float | None
    host_rss_bytes: int | None
    host_memory_percent: float | None
    gpu_uuid: str | None
    gpu_memory_bytes: int | None
    gpu_memory_percent: float | None
    gpu_sm_utilization: float | None
    gpu_memory_utilization: float | None
    gpu_encoder_utilization: float | None
    gpu_decoder_utilization: float | None


@dataclass(frozen=True, slots=True)
class WorkerResourceSnapshot:
    host: HostHardwareSnapshot | None
    gpus: tuple[GpuHardwareSnapshot, ...]
    processes: tuple[ProcessHardwareSnapshot, ...]
    source: str
    sampled_at: datetime
    probe_errors: tuple[str, ...] = ()
