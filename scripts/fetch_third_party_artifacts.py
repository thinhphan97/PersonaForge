#!/usr/bin/env python3
"""Download pinned model artifacts and verify them before making them available to workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "third_party" / "manifests"


def download(url: str, target: Path, expected_size: int, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(url, headers={"User-Agent": "PersonaForge/phase0"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
        while block := response.read(1024 * 1024):
            out.write(block)
            digest.update(block)
            size += len(block)
    actual = digest.hexdigest()
    if size != expected_size or actual != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"artifact verification failed: size={size} sha256={actual} "
            f"expected_size={expected_size} expected_sha256={expected_sha256}"
        )
    temporary.replace(target)
    print(f"Downloaded and verified {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.getenv("PERSONAFORGE_THIRD_PARTY_ROOT", ROOT / "third_party" / "artifacts")
        ),
        help="artifact cache root (mount this outside the container)",
    )
    args = parser.parse_args()
    for manifest_path in sorted(MANIFEST_DIR.glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = manifest["name"]
        for model in manifest.get("models", []):
            repository = model["repository"]
            revision = model["revision"]
            for artifact in model.get("files", []):
                target = args.root / name / artifact["path"]
                if target.is_file():
                    print(f"Exists (will verify): {target}")
                url = f"{repository}/resolve/{revision}/{artifact['path']}?download=true"
                try:
                    download(url, target, artifact["size_bytes"], artifact["sha256"])
                except Exception as exc:  # noqa: BLE001 - CLI must report the artifact name
                    print(f"ERROR {name}/{artifact['path']}: {exc}", file=sys.stderr)
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
