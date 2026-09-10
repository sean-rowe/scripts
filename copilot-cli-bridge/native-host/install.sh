#!/usr/bin/env bash
# Install the Copilot CLI Bridge native messaging host for Chrome and Edge.
#
# Usage:  ./install.sh <EXTENSION_ID>
# Get <EXTENSION_ID> from chrome://extensions (or edge://extensions) after you
# load the unpacked extension — it's the long id string under the extension.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <EXTENSION_ID>" >&2
  exit 1
fi

EXT_ID="$1"
HOST_NAME="com.pinyridgelabs.copilot_clibridge"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  echo "error: node not found in PATH. Install Node.js first." >&2
  exit 1
fi

# Wrapper so native messaging launches with the correct node even under nvm.
WRAPPER="$DIR/run-host.sh"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$NODE_BIN" "$DIR/host.js"
EOF
chmod +x "$WRAPPER"
chmod +x "$DIR/host.js"

# Build the manifest from the template.
MANIFEST_JSON="$(sed -e "s#__HOST_PATH__#$WRAPPER#g" -e "s#__EXTENSION_ID__#$EXT_ID#g" "$DIR/manifest.template.json")"

install_to() {
  local target_dir="$1"
  [ -d "$(dirname "$target_dir")" ] || return 0
  mkdir -p "$target_dir"
  printf '%s\n' "$MANIFEST_JSON" > "$target_dir/$HOST_NAME.json"
  echo "installed -> $target_dir/$HOST_NAME.json"
}

case "$(uname -s)" in
  Darwin)
    install_to "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    install_to "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts"
    install_to "$HOME/Library/Application Support/Chromium/NativeMessagingHosts"
    ;;
  Linux)
    install_to "$HOME/.config/google-chrome/NativeMessagingHosts"
    install_to "$HOME/.config/microsoft-edge/NativeMessagingHosts"
    install_to "$HOME/.config/chromium/NativeMessagingHosts"
    ;;
  *)
    echo "Unsupported OS for this script. Use install.ps1 on Windows." >&2
    exit 1
    ;;
esac

echo
echo "Done. Node: $NODE_BIN"
echo "Config will be created at: $HOME/.copilot-cli-bridge/config.json (on first run)"
echo "Reload the extension if it was already running."
