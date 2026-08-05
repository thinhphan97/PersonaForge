import asyncio
import logging

from personaforge_worker.child.runtime import CompositeChildRuntime
from personaforge_worker.consumer import RedisRenderWorker
from personaforge_worker.settings import WorkerSettings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = WorkerSettings.from_env()
    runtime: RedisRenderWorker | CompositeChildRuntime
    if settings.role == "master":
        runtime = RedisRenderWorker(settings)
    elif settings.role == "child":
        runtime = CompositeChildRuntime(settings)
    else:
        raise RuntimeError(f"Unsupported PERSONA_WORKER_ROLE: {settings.role}")
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
