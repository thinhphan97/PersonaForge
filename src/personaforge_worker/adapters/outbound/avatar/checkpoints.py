"""Pinned Hugging Face checkpoint cache for avatar model adapters."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def ensure_huggingface_snapshot(
    *,
    repo_id: str,
    revision: str,
    cache_dir: Path,
    allow_download: bool = True,
) -> Path:
    """Resolve a pinned snapshot once and reuse the external HF cache.

    The import is lazy so CPU/recorded workers do not need model dependencies at import time.
    ``snapshot_download`` performs its own file locking, making concurrent Child startup safe.
    """

    if not repo_id or not revision:
        raise ValueError("A pinned Hugging Face repo_id and revision are required")
    cache_dir.mkdir(parents=True, exist_ok=True)

    def resolve() -> str:
        from huggingface_hub import snapshot_download

        return snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=str(cache_dir),
            local_files_only=not allow_download,
            resume_download=True,
        )

    try:
        path = Path(await asyncio.to_thread(resolve))
    except Exception:
        logger.exception(
            "Avatar checkpoint resolution failed",
            extra={
                "event": "worker.avatar.checkpoint.failed",
                "model_repo": repo_id,
                "model_revision": revision,
                "cache_dir": str(cache_dir),
            },
        )
        raise
    logger.info(
        "Avatar checkpoint ready",
        extra={
            "event": "worker.avatar.checkpoint.ready",
            "model_repo": repo_id,
            "model_revision": revision,
            "checkpoint_path": str(path),
            "downloaded": allow_download,
        },
    )
    return path
