#!/usr/bin/env bash
# Upload the media the landing page references to R2 (bucket holophyte-media,
# prefix demo/, served at https://media.holophyte.ai/demo/).
# Usage: scripts/landing_media.sh CAPTURE_DIR
# CAPTURE_DIR is the capture output directory; the repository keeps no media.
set -euo pipefail

src=${1:?usage: scripts/landing_media.sh CAPTURE_DIR}
page="$(cd "$(dirname "$0")/.." && pwd)/landing/public/index.html"
names=$(grep -oE 'https://media\.holophyte\.ai/demo/[A-Za-z0-9._-]+' "$page" \
  | sed 's#.*/##' | sort -u)

for name in $names; do
  [ -f "$src/$name" ] || { echo "missing: $src/$name" >&2; exit 1; }
done

for name in $names; do
  case "$name" in
    *.png) type=image/png ;;
    *.svg) type=image/svg+xml ;;
    *.mp4) type=video/mp4 ;;
    *.webm) type=video/webm ;;
    *) echo "no content type for $name" >&2; exit 1 ;;
  esac
  npx -y wrangler@4 r2 object put "holophyte-media/demo/$name" \
    --file "$src/$name" --content-type "$type" --remote
done
