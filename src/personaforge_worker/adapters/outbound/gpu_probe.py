import asyncio
import csv
import logging
from datetime import datetime, timezone

from personaforge_worker.domain.hardware import GpuHardwareSnapshot

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
) -> ConfiguredGpuProbe | NvidiaSmiGpuProbe:
    configured = ConfiguredGpuProbe(uuid, name, compute_capability, total_vram_mb)
    if provider == "configured":
        return configured
    if provider not in {"auto", "nvidia-smi"}:
        raise ValueError(f"Unsupported AVATAR_GPU_PROBE_PROVIDER: {provider}")
    return NvidiaSmiGpuProbe(
        configured,
        uuid,
        timeout_seconds,
        strict=provider == "nvidia-smi",
    )


def _integer(value: str) -> int:
    return round(_number(value))


def _number(value: str) -> float:
    return float(value)


def _optional_number(value: str) -> float | None:
    return None if value.lower() in {"n/a", "[not supported]", "not supported"} else float(value)
