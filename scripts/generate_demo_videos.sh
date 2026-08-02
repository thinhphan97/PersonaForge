#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-assets/characters/default}"
mkdir -p "$output_dir"

generate_clip() {
  local output="$1"
  local color="$2"
  local frequency="$3"
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=${color}:s=640x360:r=30:d=3" \
    -vf "drawbox=x='(w-100)/2+40*sin(t*${frequency})':y=(h-100)/2:w=100:h=100:color=white@0.9:t=fill" \
    -an -c:v libx264 -preset veryfast -pix_fmt yuv420p -movflags +faststart "$output"
}

generate_clip "$output_dir/idle.mp4" "0x3154a4" "2"
generate_clip "$output_dir/thinking.mp4" "0xb86b18" "5"
generate_clip "$output_dir/speaking.mp4" "0x248a62" "9"

echo "Generated demo clips in $output_dir"

