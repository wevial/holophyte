#!/usr/bin/env bash
# Bundle the landing page's header effect into landing/public/effect.js.
# The site is served without a build step, so the bundle is committed.
# Usage: scripts/landing_effect.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
bun build --minify --format=iife --target=browser \
  "$root/landing/src/effect.ts" --outfile "$root/landing/public/effect.js"
