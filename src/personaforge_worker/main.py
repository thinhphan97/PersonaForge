import asyncio

from personaforge_observability import configure_logging
from personaforge_worker.child.runtime import CompositeChildRuntime
from personaforge_worker.consumer import RedisRenderWorker
from personaforge_worker.settings import WorkerSettings


def main() -> None:
    settings = WorkerSettings.from_env()
    configure_logging("personaforge-worker", process_role=settings.role)
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
