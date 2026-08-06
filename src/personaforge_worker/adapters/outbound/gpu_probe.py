import asyncio
import csv
import ctypes
import logging
from datetime import datetime, timezone
from typing import Any, cast

from personaforge_worker.domain.hardware import (
    GpuHardwareSnapshot,
    HostHardwareSnapshot,
    ProcessHardwareSnapshot,
    WorkerResourceSnapshot,
)

logger = logging.getLogger(__name__)

_QUERY_FIELDS = (
    "uuid",
    "name",
    "compute_cap",
    "memory.total",
    "memory.free",
    "memory.used",
    "utilization.gpu",
    "utilization.memory",
    "temperature.gpu",
    "power.draw",
)


class ConfiguredGpuProbe:
    def __init__(
        self,
        uuid: str,
        name: str,
        compute_capability: str,
        total_vram_mb: int,
    ) -> None:
        self._uuid = uuid
        self._name = name
        self._compute_capability = compute_capability
        self._total_vram_mb = total_vram_mb

    async def sample(self) -> GpuHardwareSnapshot:
        return self.snapshot()

    async def sample_resources(self, pids: tuple[int, ...]) -> WorkerResourceSnapshot:
        del pids
        gpu = self.snapshot()
        return WorkerResourceSnapshot(
            host=None,
            gpus=(gpu,),
            processes=(),
            source=gpu.source,
            sampled_at=gpu.sampled_at,
            probe_errors=((gpu.probe_error,) if gpu.probe_error else ()),
        )

    def snapshot(self, error: str | None = None) -> GpuHardwareSnapshot:
        return GpuHardwareSnapshot(
            uuid=self._uuid,
            name=self._name,
            compute_capability=self._compute_capability,
            total_vram_mb=self._total_vram_mb,
            free_vram_mb=self._total_vram_mb,
            used_vram_mb=0,
            gpu_utilization=0,
            memory_utilization=0,
            temperature_c=None,
            power_watts=None,
            source="configured" if error is None else "configured-fallback",
            sampled_at=datetime.now(timezone.utc),
            probe_error=error,
        )


class NvidiaSmiGpuProbe:
    """Read one GPU without linking the worker core to NVML or CUDA libraries."""

    def __init__(
        self,
        fallback: ConfiguredGpuProbe,
        selected_uuid: str | None,
        timeout_seconds: float,
        strict: bool = False,
        executable: str = "nvidia-smi",
    ) -> None:
        self._fallback = fallback
        self._selected_uuid = selected_uuid if selected_uuid != "GPU-none" else None
        self._timeout_seconds = timeout_seconds
        self._strict = strict
        self._executable = executable
        self._fallback_logged = False

    async def sample(self) -> GpuHardwareSnapshot:
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                f"--query-gpu={','.join(_QUERY_FIELDS)}",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._timeout_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            if process.returncode != 0:
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(detail or f"nvidia-smi exited {process.returncode}")
            snapshots = parse_nvidia_smi(stdout.decode())
            if not snapshots:
                raise RuntimeError("nvidia-smi returned no GPU")
            if self._selected_uuid is None:
                return snapshots[0]
            selected = next(
                (item for item in snapshots if item.uuid == self._selected_uuid),
                None,
            )
            if selected is None:
                raise RuntimeError(f"Configured GPU UUID not found: {self._selected_uuid}")
            return selected
        except (FileNotFoundError, TimeoutError, RuntimeError, ValueError) as exc:
            if self._strict:
                raise RuntimeError("GPU hardware probe failed") from exc
            if not self._fallback_logged:
                logger.warning("GPU probe fallback to configuration: %s", exc)
                self._fallback_logged = True
            return self._fallback.snapshot(str(exc))

    async def sample_resources(self, pids: tuple[int, ...]) -> WorkerResourceSnapshot:
        del pids
        gpu = await self.sample()
        return WorkerResourceSnapshot(
            host=None,
            gpus=(gpu,),
            processes=(),
            source=gpu.source,
            sampled_at=gpu.sampled_at,
            probe_errors=((gpu.probe_error,) if gpu.probe_error else ()),
        )


class NvitopResourceProbe:
    """Collect immutable host, GPU and managed-PID snapshots via nvitop public APIs."""

    def __init__(
        self,
        fallback: ConfiguredGpuProbe,
        selected_uuid: str | None,
        timeout_seconds: float,
        strict: bool = False,
    ) -> None:
        self._fallback = fallback
        self._selected_uuid = selected_uuid if selected_uuid != "GPU-none" else None
        self._timeout_seconds = timeout_seconds
        self._strict = strict
        self._fallback_logged = False
        self._disabled_error: str | None = None
        self._probe_lock = asyncio.Lock()

    async def sample(self) -> GpuHardwareSnapshot:
        resources = await self.sample_resources(())
        if self._selected_uuid is not None:
            selected = next(
                (gpu for gpu in resources.gpus if gpu.uuid == self._selected_uuid),
                None,
            )
            if selected is not None:
                return selected
            error = f"Configured GPU UUID not visible through nvitop: {self._selected_uuid}"
            if self._strict:
                raise RuntimeError(error)
            return self._fallback.snapshot(error)
        if resources.gpus:
            return resources.gpus[0]
        return self._fallback.snapshot("nvitop returned no visible GPU")

    async def sample_resources(self, pids: tuple[int, ...]) -> WorkerResourceSnapshot:
        if self._disabled_error is not None:
            return self._fallback_resources(self._disabled_error)
        async with self._probe_lock:
            if self._disabled_error is not None:
                return self._fallback_resources(self._disabled_error)
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._sample_sync, pids),
                    timeout=self._timeout_seconds,
                )
            except (ImportError, TimeoutError, RuntimeError, ValueError) as exc:
                if self._strict:
                    raise RuntimeError("nvitop resource probe failed") from exc
                error = str(exc)
                # Auto mode is intentionally fail-sticky for the process lifetime. NVML and
                # container device visibility cannot normally appear without restarting the
                # worker; retrying every heartbeat only emits nvitop CRITICAL logs and burns a
                # thread. A worker restart performs discovery again.
                self._disabled_error = error
                if not self._fallback_logged:
                    logger.warning("nvitop probe fallback to configuration: %s", exc)
                    self._fallback_logged = True
                return self._fallback_resources(error)

    def _fallback_resources(self, error: str) -> WorkerResourceSnapshot:
        gpu = self._fallback.snapshot(error)
        return WorkerResourceSnapshot(
            host=None,
            gpus=(gpu,),
            processes=(),
            source="configured-fallback",
            sampled_at=gpu.sampled_at,
            probe_errors=(error,),
        )

    def _sample_sync(self, pids: tuple[int, ...]) -> WorkerResourceSnapshot:
        _require_nvml_library()
        from nvitop import Device, HostProcess, host

        sampled_at = datetime.now(timezone.utc)
        errors: list[str] = []
        try:
            devices = tuple(Device.all())
        except Exception as exc:
            raise RuntimeError(f"Could not enumerate nvitop devices: {exc}") from exc
        if not devices:
            raise RuntimeError("nvitop returned no visible GPU")
        gpu_snapshots: list[GpuHardwareSnapshot] = []
        gpu_processes: dict[int, list[object]] = {}
        for device in devices:
            try:
                gpu_snapshots.append(_nvitop_gpu_snapshot(device, sampled_at))
                for pid, process in device.processes().items():
                    gpu_processes.setdefault(pid, []).append(process)
            except Exception as exc:
                errors.append(f"device:{_safe_call(device, 'uuid')}:{exc}")
        if not gpu_snapshots:
            raise RuntimeError("nvitop could not sample any visible GPU")
        process_snapshots: list[ProcessHardwareSnapshot] = []
        for pid in pids:
            try:
                host_process = HostProcess(pid)
                process_snapshots.append(
                    _nvitop_process_snapshot(
                        pid,
                        host_process,
                        gpu_processes.get(pid, []),
                    )
                )
            except Exception as exc:
                errors.append(f"pid:{pid}:{exc}")
        return WorkerResourceSnapshot(
            host=_nvitop_host_snapshot(host, errors),
            gpus=tuple(gpu_snapshots),
            processes=tuple(process_snapshots),
            source="nvitop",
            sampled_at=sampled_at,
            probe_errors=tuple(errors),
        )


def _require_nvml_library() -> None:
    """Fail quietly before importing nvitop when the container has no NVIDIA driver."""
    try:
        ctypes.CDLL("libnvidia-ml.so.1")
    except OSError as exc:
        raise RuntimeError("NVML library is not available in this container") from exc


def parse_nvidia_smi(output: str) -> tuple[GpuHardwareSnapshot, ...]:
    snapshots: list[GpuHardwareSnapshot] = []
    for row in csv.reader(line for line in output.splitlines() if line.strip()):
        if len(row) != len(_QUERY_FIELDS):
            raise ValueError("Unexpected nvidia-smi CSV field count")
        values = tuple(value.strip() for value in row)
        snapshots.append(
            GpuHardwareSnapshot(
                uuid=values[0],
                name=values[1],
                compute_capability=values[2],
                total_vram_mb=_integer(values[3]),
                free_vram_mb=_integer(values[4]),
                used_vram_mb=_integer(values[5]),
                gpu_utilization=_number(values[6]),
                memory_utilization=_number(values[7]),
                temperature_c=_optional_number(values[8]),
                power_watts=_optional_number(values[9]),
                source="nvidia-smi",
                sampled_at=datetime.now(timezone.utc),
            )
        )
    return tuple(snapshots)


def build_gpu_probe(
    provider: str,
    uuid: str,
    name: str,
    compute_capability: str,
    total_vram_mb: int,
    timeout_seconds: float,
) -> ConfiguredGpuProbe | NvidiaSmiGpuProbe | NvitopResourceProbe:
    configured = ConfiguredGpuProbe(uuid, name, compute_capability, total_vram_mb)
    if provider == "configured":
        return configured
    if provider in {"auto", "nvitop"}:
        return NvitopResourceProbe(
            configured,
            uuid,
            timeout_seconds,
            strict=provider == "nvitop",
        )
    if provider != "nvidia-smi":
        raise ValueError(f"Unsupported AVATAR_GPU_PROBE_PROVIDER: {provider}")
    return NvidiaSmiGpuProbe(
        configured,
        uuid,
        timeout_seconds,
        strict=provider == "nvidia-smi",
    )


def _nvitop_gpu_snapshot(device: object, sampled_at: datetime) -> GpuHardwareSnapshot:
    total = _optional_int_value(_safe_call(device, "memory_total")) or 0
    free = _optional_int_value(_safe_call(device, "memory_free")) or 0
    used = _optional_int_value(_safe_call(device, "memory_used")) or 0
    capability = _safe_call(device, "cuda_compute_capability")
    if isinstance(capability, tuple) and len(capability) >= 2:
        capability_text = f"{capability[0]}.{capability[1]}"
    else:
        capability_text = _optional_string_value(capability) or "0.0"
    return GpuHardwareSnapshot(
        uuid=_optional_string_value(_safe_call(device, "uuid")) or "GPU-unknown",
        name=_optional_string_value(_safe_call(device, "name")) or "Unknown NVIDIA GPU",
        compute_capability=capability_text,
        total_vram_mb=total // (1024 * 1024),
        free_vram_mb=free // (1024 * 1024),
        used_vram_mb=used // (1024 * 1024),
        gpu_utilization=_optional_float_value(_safe_call(device, "gpu_utilization")) or 0,
        memory_utilization=(
            _optional_float_value(_safe_call(device, "memory_utilization")) or 0
        ),
        temperature_c=_optional_float_value(_safe_call(device, "temperature")),
        power_watts=_divide_optional(_safe_call(device, "power_usage"), 1000),
        source="nvitop",
        sampled_at=sampled_at,
        encoder_utilization=_optional_float_value(
            _safe_call(device, "encoder_utilization")
        ),
        decoder_utilization=_optional_float_value(
            _safe_call(device, "decoder_utilization")
        ),
        fan_speed_percent=_optional_float_value(_safe_call(device, "fan_speed")),
        sm_clock_mhz=_optional_float_value(_safe_call(device, "sm_clock")),
        memory_clock_mhz=_optional_float_value(_safe_call(device, "memory_clock")),
        pcie_tx_bytes_per_second=_throughput_bytes(_safe_call(device, "pcie_tx_throughput")),
        pcie_rx_bytes_per_second=_throughput_bytes(_safe_call(device, "pcie_rx_throughput")),
    )


def _nvitop_host_snapshot(host: Any, errors: list[str]) -> HostHardwareSnapshot:
    try:
        memory = host.virtual_memory()
        swap = host.swap_memory()
        load = host.load_average()
        return HostHardwareSnapshot(
            cpu_utilization=_optional_float_value(host.cpu_percent(interval=None)),
            cpu_count_logical=_optional_int_value(host.cpu_count()),
            load_average_1m=(
                _optional_float_value(load[0])
                if isinstance(load, tuple | list) and load
                else None
            ),
            ram_total_bytes=_optional_int_value(memory.total),
            ram_used_bytes=_optional_int_value(memory.used),
            ram_available_bytes=_optional_int_value(memory.available),
            swap_used_bytes=_optional_int_value(swap.used),
        )
    except Exception as exc:
        errors.append(f"host:{exc}")
        return HostHardwareSnapshot(None, None, None, None, None, None, None)


def _nvitop_process_snapshot(
    pid: int,
    host_process: object,
    gpu_processes: list[object],
) -> ProcessHardwareSnapshot:
    gpu_process = gpu_processes[0] if gpu_processes else None
    gpu_uuid = None
    if gpu_process is not None:
        device = getattr(gpu_process, "device", None)
        gpu_uuid = _optional_string_value(_safe_call(device, "uuid"))
    return ProcessHardwareSnapshot(
        pid=pid,
        process_create_time=_optional_float_value(_safe_call(host_process, "create_time")),
        status=_optional_string_value(_safe_call(host_process, "status")),
        cpu_utilization=_optional_float_value(_safe_call(host_process, "cpu_percent")),
        host_rss_bytes=_optional_int_value(_safe_call(host_process, "rss_memory")),
        host_memory_percent=_optional_float_value(
            _safe_call(host_process, "memory_percent")
        ),
        gpu_uuid=gpu_uuid,
        gpu_memory_bytes=(
            _optional_int_value(_safe_call(gpu_process, "gpu_memory"))
            if gpu_process is not None
            else None
        ),
        gpu_memory_percent=(
            _optional_float_value(_safe_call(gpu_process, "gpu_memory_percent"))
            if gpu_process is not None
            else None
        ),
        gpu_sm_utilization=(
            _optional_float_value(_safe_call(gpu_process, "gpu_sm_utilization"))
            if gpu_process is not None
            else None
        ),
        gpu_memory_utilization=(
            _optional_float_value(_safe_call(gpu_process, "gpu_memory_utilization"))
            if gpu_process is not None
            else None
        ),
        gpu_encoder_utilization=(
            _optional_float_value(_safe_call(gpu_process, "gpu_encoder_utilization"))
            if gpu_process is not None
            else None
        ),
        gpu_decoder_utilization=(
            _optional_float_value(_safe_call(gpu_process, "gpu_decoder_utilization"))
            if gpu_process is not None
            else None
        ),
    )


def _safe_call(target: Any | None, method: str) -> object | None:
    if target is None:
        return None
    try:
        value = getattr(target, method)
        return cast(object, value() if callable(value) else value)
    except Exception:
        return None


def _optional_string_value(value: object | None) -> str | None:
    if value is None or type(value).__name__ == "NaType":
        return None
    text = str(value)
    return text if text and text != "N/A" else None


def _optional_float_value(value: object | None) -> float | None:
    if value is None or type(value).__name__ == "NaType":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _optional_int_value(value: object | None) -> int | None:
    number = _optional_float_value(value)
    return round(number) if number is not None else None


def _divide_optional(value: object | None, divisor: float) -> float | None:
    number = _optional_float_value(value)
    return number / divisor if number is not None else None


def _throughput_bytes(value: object | None) -> int | None:
    kilobytes_per_second = _optional_float_value(value)
    return (
        round(kilobytes_per_second * 1024)
        if kilobytes_per_second is not None
        else None
    )


def _integer(value: str) -> int:
    return round(_number(value))


def _number(value: str) -> float:
    return float(value)


def _optional_number(value: str) -> float | None:
    return None if value.lower() in {"n/a", "[not supported]", "not supported"} else float(value)
