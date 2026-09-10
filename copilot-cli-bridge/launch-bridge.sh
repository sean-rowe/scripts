#!/usr/bin/env bash
# Launch a browser with the Copilot CLI Bridge extension already loaded — no
# "Load unpacked" clicks needed. Uses a dedicated, persistent profile so your
# Copilot sign-in is remembered between launches.
#
# Usage:
#   ./launch-bridge.sh          # Edge (default)
#   ./launch-bridge.sh chrome   # Chrome
#
# The extension ID is derived from the extension folder path, so it stays
# mpeidobpgcegkmdgocedjkgodkfnanmd and matches the installed native host.

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT="$DIR/extension"
BROWSER="${1:-edge}"

case "$BROWSER" in
  edge)
    APP="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    PROFILE="$HOME/.copilot-cli-bridge/edge-profile"
    ;;
  chrome)
    APP="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    PROFILE="$HOME/.copilot-cli-bridge/chrome-profile"
    ;;
  *)
    echo "usage: $0 [edge|chrome]" >&2; exit 1 ;;
esac

if [ ! -x "$APP" ]; then echo "browser not found: $APP" >&2; exit 1; fi
mkdir -p "$PROFILE"

# Clear the cached service worker so the latest background.js always loads.
# (Chromium can keep a stale extension service worker across --load-extension
# launches even after a version bump. Cookies/login live elsewhere and survive.)
rm -rf "$PROFILE/Default/Service Worker" 2>/dev/null || true

exec "$APP" \
  --user-data-dir="$PROFILE" \
  --load-extension="$EXT" \
  --no-first-run --no-default-browser-check \
  "https://copilot.microsoft.com"
