import asyncio
import logging

from personaforge_worker.child.runtime import CompositeChildRuntime
from personaforge_worker.settings import WorkerSettings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(CompositeChildRuntime(WorkerSettings.from_env()).run())


if __name__ == "__main__":
    main()
