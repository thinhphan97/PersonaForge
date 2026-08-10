#!/usr/bin/env bash
set -euo pipefail

# Fetches only pinned source repositories. Large model artifacts are intentionally not downloaded
# by this helper; put them in third_party/artifacts/<manifest-name>/ and run verify_third_party.py.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT_DIR/third_party"

fetch_repo() {
  local name="$1"
  local url="$2"
  local commit="$3"
  local target="$ROOT_DIR/third_party/$name"
  if [[ ! -d "$target/.git" ]]; then
    git clone --no-checkout "$url" "$target"
  fi
  git -C "$target" fetch --quiet --depth 1 origin "$commit"
  git -C "$target" checkout --detach --quiet "$commit"
}

fetch_repo LAM \
  https://github.com/aigc3d/LAM.git \
  339573649dd93df4cba8093a964e85a80d1b61f3
fetch_repo LAM_Audio2Expression \
  https://github.com/aigc3d/LAM_Audio2Expression.git \
  02a703c3ea7d8e360eb43098eca85ee98a083529
fetch_repo LAM_WebRender \
  https://github.com/aigc3d/LAM_WebRender.git \
  12174e944957fc6445f235c5e0ff7e3e16ba209a

echo "Pinned third-party source repositories are ready."
