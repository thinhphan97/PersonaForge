#!/usr/bin/env python3
"""Verify pinned third-party source and local model artifacts.

The verifier never downloads model data. Fetching is deliberately a separate, explicit step so
container startup cannot silently change the model revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = ROOT / "third_party" / "manifests"
ARTIFACT_ROOT = Path(
    os.getenv("PERSONAFORGE_THIRD_PARTY_ROOT", ROOT / "third_party" / "artifacts")
)
SOURCE_DIRS = {
    "lam": ROOT / "third_party" / "LAM",
    "lam-a2e": ROOT / "third_party" / "LAM_Audio2Expression",
    "lam-web-render": ROOT / "third_party" / "LAM_WebRender",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_head(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def verify_manifest(path: Path, strict: bool) -> tuple[int, int]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    name = manifest["name"]
    expected_commit = manifest["source"]["commit"]
    source = SOURCE_DIRS[name]
    checked = failed = 0
    actual_commit = git_head(source)
    if actual_commit is None:
        message = f"{name}: source is not fetched ({source})"
        (print(f"ERROR {message}") if strict else print(f"SKIP  {message}"))
        return (1 if strict else 0, 1)
    checked += 1
    if actual_commit != expected_commit:
        print(f"ERROR {name}: commit {actual_commit} != {expected_commit}")
        failed += 1
    else:
        print(f"OK    {name}: source {actual_commit}")

    for model in manifest.get("models", []):
        for artifact in model.get("files", []):
            artifact_path = ARTIFACT_ROOT / name / artifact["path"]
            if not artifact_path.is_file():
                message = f"{name}: artifact is not cached ({artifact_path})"
                (print(f"ERROR {message}") if strict else print(f"SKIP  {message}"))
                if strict:
                    failed += 1
                continue
            checked += 1
            actual_size = artifact_path.stat().st_size
            actual_hash = sha256(artifact_path)
            if actual_size != artifact["size_bytes"] or actual_hash != artifact["sha256"]:
                print(
                    f"ERROR {name}: artifact mismatch {artifact['path']} "
                    f"size={actual_size} sha256={actual_hash}"
                )
                failed += 1
            else:
                print(f"OK    {name}: artifact {artifact['path']}")
    return failed, checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when sources or model artifacts have not been fetched",
    )
    args = parser.parse_args()
    total_failed = total_checked = 0
    for manifest in sorted(MANIFEST_DIR.glob("*.json")):
        failed, checked = verify_manifest(manifest, args.strict)
        total_failed += failed
        total_checked += checked
    print(f"Checked {total_checked} pinned item(s); failures={total_failed}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
