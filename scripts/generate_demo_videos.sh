#!/usr/bin/env bash
set -euo pipefail

characters_dir="${1:-assets/characters}"

generate_clip() {
  local output="$1"
  local color="$2"
  local frequency="$3"
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=${color}:s=640x360:r=30:d=3" \
    -vf "drawbox=x='(w-100)/2+40*sin(t*${frequency})':y=(h-100)/2:w=100:h=100:color=white@0.9:t=fill" \
    -an -c:v libx264 -preset veryfast -pix_fmt yuv420p -movflags +faststart "$output"
}

generate_character() {
  local character_id="$1"
  local idle_color="$2"
  local thinking_color="$3"
  local speaking_color="$4"
  local output_dir="$characters_dir/$character_id"
  mkdir -p "$output_dir"
  generate_clip "$output_dir/idle.mp4" "$idle_color" "2"
  generate_clip "$output_dir/thinking.mp4" "$thinking_color" "5"
  generate_clip "$output_dir/speaking.mp4" "$speaking_color" "9"
}

generate_character "default" "0x3154a4" "0xb86b18" "0x248a62"
generate_character "violet" "0x5b3f95" "0x7f4ab2" "0xa33c91"
generate_character "coral" "0x9c3f52" "0xd1644f" "0xd93868"

echo "Generated demo characters in $characters_dir"
