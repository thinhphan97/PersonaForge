import asyncio

from personaforge_observability import configure_logging
from personaforge_worker.child.runtime import CompositeChildRuntime
from personaforge_worker.settings import WorkerSettings


def main() -> None:
    settings = WorkerSettings.from_env()
    configure_logging("personaforge-worker", process_role="child")
    asyncio.run(CompositeChildRuntime(settings).run())


if __name__ == "__main__":
    main()
