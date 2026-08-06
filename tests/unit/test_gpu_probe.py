from datetime import datetime, timezone

import pytest

from personaforge_worker.adapters.outbound.gpu_probe import (
    ConfiguredGpuProbe,
    NvidiaSmiGpuProbe,
    NvitopResourceProbe,
    _nvitop_gpu_snapshot,
    _nvitop_process_snapshot,
    parse_nvidia_smi,
)


def test_parse_nvidia_smi_hardware_and_live_utilization() -> None:
    snapshots = parse_nvidia_smi(
        "GPU-a, NVIDIA L4, 8.9, 23034, 18000, 5034, 47, 22, 63, 71.5\n"
    )

    assert len(snapshots) == 1
    gpu = snapshots[0]
    assert gpu.uuid == "GPU-a"
    assert gpu.compute_capability == "8.9"
    assert gpu.total_vram_mb == 23_034
    assert gpu.free_vram_mb == 18_000
    assert gpu.used_vram_mb == 5_034
    assert gpu.gpu_utilization == 47
    assert gpu.temperature_c == 63
    assert gpu.sampled_at.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_auto_gpu_probe_falls_back_to_config_when_nvidia_smi_is_missing() -> None:
    configured = ConfiguredGpuProbe("GPU-config", "Configured GPU", "8.0", 12_000)
    probe = NvidiaSmiGpuProbe(
        configured,
        selected_uuid="GPU-config",
        timeout_seconds=0.1,
        executable="personaforge-nvidia-smi-does-not-exist",
    )

    snapshot = await probe.sample()

    assert snapshot.uuid == "GPU-config"
    assert snapshot.source == "configured-fallback"
    assert snapshot.probe_error


@pytest.mark.asyncio
async def test_auto_nvitop_probe_stops_retrying_after_nvml_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = ConfiguredGpuProbe("GPU-config", "Configured GPU", "0.0", 0)
    probe = NvitopResourceProbe(
        configured,
        selected_uuid=None,
        timeout_seconds=0.1,
    )
    attempts = 0

    def fail_probe(pids: tuple[int, ...]) -> None:
        nonlocal attempts
        del pids
        attempts += 1
        raise RuntimeError("NVML library is not available in this container")

    monkeypatch.setattr(probe, "_sample_sync", fail_probe)

    first = await probe.sample_resources(())
    second = await probe.sample_resources((123,))

    assert attempts == 1
    assert first.source == "configured-fallback"
    assert second.source == "configured-fallback"
    assert second.probe_errors == ("NVML library is not available in this container",)


def test_nvitop_adapter_maps_device_and_managed_process_metrics() -> None:
    sampled_at = datetime.now(timezone.utc)
    device = _FakeDevice()
    gpu = _nvitop_gpu_snapshot(device, sampled_at)
    process = _nvitop_process_snapshot(42, _FakeHostProcess(), [_FakeGpuProcess(device)])

    assert gpu.uuid == "GPU-test"
    assert gpu.total_vram_mb == 24_000
    assert gpu.gpu_utilization == 67
    assert gpu.temperature_c == 64
    assert gpu.power_watts == 125
    assert gpu.encoder_utilization == 21
    assert process.pid == 42
    assert process.host_rss_bytes == 2_000_000_000
    assert process.cpu_utilization == 75
    assert process.gpu_uuid == "GPU-test"
    assert process.gpu_memory_bytes == 6_000_000_000
    assert process.gpu_sm_utilization == 44


class _FakeDevice:
    def uuid(self) -> str:
        return "GPU-test"

    def name(self) -> str:
        return "NVIDIA Test"

    def cuda_compute_capability(self) -> tuple[int, int]:
        return (8, 9)

    def memory_total(self) -> int:
        return 24_000 * 1024 * 1024

    def memory_free(self) -> int:
        return 16_000 * 1024 * 1024

    def memory_used(self) -> int:
        return 8_000 * 1024 * 1024

    def gpu_utilization(self) -> int:
        return 67

    def memory_utilization(self) -> int:
        return 31

    def temperature(self) -> int:
        return 64

    def power_usage(self) -> int:
        return 125_000

    def encoder_utilization(self) -> int:
        return 21

    def decoder_utilization(self) -> int:
        return 3

    def fan_speed(self) -> int:
        return 40

    def sm_clock(self) -> int:
        return 1_800

    def memory_clock(self) -> int:
        return 9_000

    def pcie_tx_throughput(self) -> int:
        return 100

    def pcie_rx_throughput(self) -> int:
        return 200


class _FakeHostProcess:
    def create_time(self) -> float:
        return 123.5

    def status(self) -> str:
        return "running"

    def cpu_percent(self) -> float:
        return 75

    def rss_memory(self) -> int:
        return 2_000_000_000

    def memory_percent(self) -> float:
        return 4.2


class _FakeGpuProcess:
    def __init__(self, device: _FakeDevice) -> None:
        self.device = device

    def gpu_memory(self) -> int:
        return 6_000_000_000

    def gpu_memory_percent(self) -> float:
        return 12.5

    def gpu_sm_utilization(self) -> int:
        return 44

    def gpu_memory_utilization(self) -> int:
        return 20

    def gpu_encoder_utilization(self) -> int:
        return 8

    def gpu_decoder_utilization(self) -> int:
        return 1
