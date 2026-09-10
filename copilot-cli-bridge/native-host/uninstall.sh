#!/usr/bin/env bash
# Remove the Copilot CLI Bridge native messaging host manifests.
set -euo pipefail
HOST_NAME="com.pinyridgelabs.copilot_clibridge"

remove_from() {
  local f="$1/$HOST_NAME.json"
  if [ -f "$f" ]; then rm -f "$f"; echo "removed $f"; fi
}

case "$(uname -s)" in
  Darwin)
    remove_from "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    remove_from "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts"
    remove_from "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
    ;;
  Linux)
    remove_from "$HOME/.config/google-chrome/NativeMessagingHosts"
    remove_from "$HOME/.config/microsoft-edge/NativeMessagingHosts"
    remove_from "$HOME/.config/chromium/NativeMessagingHosts"
    ;;
esac
echo "Done. (Config at ~/.copilot-cli-bridge/ was left in place; delete it manually if desired.)"
