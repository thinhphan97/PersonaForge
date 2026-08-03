import asyncio
import logging

from avatar_worker.consumer import RedisRenderWorker
from avatar_worker.settings import WorkerSettings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(RedisRenderWorker(WorkerSettings.from_env()).run())


if __name__ == "__main__":
    main()
