#!/usr/bin/env bash
# NetCEC: build and install/update the plugin, then restart Decky.
# Usage:  bash update.sh   (from this folder or anywhere)
cd "$(dirname "$0")" || exit 1
DEST="$HOME/homebrew/plugins/NetCEC"

# node/pnpm come from nvm (sourced before 'set -e': nvm.sh is not strict-mode safe)
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"
set -eo pipefail
command -v pnpm >/dev/null || { echo "pnpm not found. Install it first: nvm install 20 && npm i -g pnpm@9"; exit 1; }

pnpm i
pnpm build
sudo mkdir -p "$DEST"
sudo cp -r dist main.py plugin.json package.json "$DEST/"
sudo systemctl restart plugin_loader
echo "Done. Installed: $(grep -m1 '"version"' package.json)"
