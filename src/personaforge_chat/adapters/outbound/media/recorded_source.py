from pathlib import Path
from typing import Protocol

from personaforge_chat.application.dto import CharacterSummary
from personaforge_chat.domain.models import AvatarState


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


class RecordedCharacterCatalog:
    """Filesystem-backed character metadata and recorded clip resolver."""

    def __init__(
        self,
        root: Path,
        default_clips: dict[AvatarState, Path] | None = None,
    ) -> None:
        clips: dict[str, dict[AvatarState, Path]] = {}
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                candidate = {
                    state: directory / f"{state.value}.mp4"
                    for state in AvatarState
                }
                if all(path.is_file() for path in candidate.values()):
                    clips[directory.name] = candidate
        if default_clips is not None and all(
            path.is_file() for path in default_clips.values()
        ):
            clips["default"] = default_clips
        self._clips = clips

    def get(self, character_id: str) -> CharacterSummary | None:
        if character_id not in self._clips:
            return None
        return CharacterSummary(
            character_id=character_id,
            display_name=character_id.replace("-", " ").replace("_", " ").title(),
        )

    def list(self) -> tuple[CharacterSummary, ...]:
        return tuple(
            profile
            for character_id in sorted(
                self._clips,
                key=lambda item: (item != "default", item),
            )
            if (profile := self.get(character_id)) is not None
        )

    def clips_for(self, character_id: str) -> RecordedClipCatalog:
        clips = self._clips.get(character_id)
        if clips is None:
            raise ValueError(f"Unknown recorded character: {character_id}")
        return RecordedClipCatalog(clips)
