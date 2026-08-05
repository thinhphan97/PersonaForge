from personaforge_control.application.ports import ClockPort, WorkerInventoryPort
from personaforge_control.domain.models import WorkerHeartbeat, WorkerRegistration


class ListWorkerInventory:
    def __init__(self, inventory: WorkerInventoryPort, clock: ClockPort) -> None:
        self._inventory = inventory
        self._clock = clock

    async def execute(
        self,
    ) -> tuple[tuple[WorkerRegistration, WorkerHeartbeat | None], ...]:
        return await self._inventory.inventory(self._clock.now())
