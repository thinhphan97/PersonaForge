from pathlib import Path
from typing import Protocol

from avatar_chat.domain.models import AvatarState


class ClipCatalog(Protocol):
    def path_for(self, state: AvatarState) -> Path: ...


class RecordedClipCatalog:
    def __init__(self, clips: dict[AvatarState, Path]) -> None:
        self._clips = clips

    def path_for(self, state: AvatarState) -> Path:
        path = self._clips.get(state) or self._clips.get(AvatarState.IDLE)
        if path is None:
            raise RuntimeError("No idle avatar clip is configured")
        if not path.is_file():
            raise FileNotFoundError(
                f"Avatar clip does not exist: {path}. "
                "Run scripts/generate_demo_videos.sh or configure AVATAR_*_VIDEO."
            )
        return path

