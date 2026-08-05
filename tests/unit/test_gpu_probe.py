from datetime import timezone

import pytest

from avatar_worker.adapters.outbound.gpu_probe import (
    ConfiguredGpuProbe,
    NvidiaSmiGpuProbe,
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
