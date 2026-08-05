from typing import Protocol

from avatar_worker.domain.hardware import GpuHardwareSnapshot


class GpuHardwareProbePort(Protocol):
    async def sample(self) -> GpuHardwareSnapshot: ...
