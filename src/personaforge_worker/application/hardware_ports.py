from typing import Protocol

from personaforge_worker.domain.hardware import GpuHardwareSnapshot, WorkerResourceSnapshot


class GpuHardwareProbePort(Protocol):
    async def sample(self) -> GpuHardwareSnapshot: ...

    async def sample_resources(self, pids: tuple[int, ...]) -> WorkerResourceSnapshot: ...
