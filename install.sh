#!/usr/bin/env bash
# NetCEC installer / updater for the SteamOS machine (run it in a Desktop Mode terminal or over SSH).
#   curl -fsSL https://raw.githubusercontent.com/MSPortnov/NetCEC/main/install.sh | bash
# Downloads the latest GitHub release, installs it to ~/homebrew/plugins/NetCEC and restarts Decky.
# Running it again updates the plugin. Settings and pairing are kept (stored outside the plugin folder).
set -euo pipefail

BASE="${NETCEC_BASE:-https://github.com/MSPortnov/NetCEC/releases/latest/download}"
DEST="$HOME/homebrew/plugins/NetCEC"

command -v curl >/dev/null    || { echo "curl not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> Downloading the latest NetCEC release"
curl -fsSL "$BASE/NetCEC.zip" -o "$TMP/NetCEC.zip"
curl -fsSL "$BASE/NetCEC.zip.sha256" -o "$TMP/NetCEC.zip.sha256"
(cd "$TMP" && sha256sum -c NetCEC.zip.sha256 >/dev/null) || { echo "Checksum mismatch, aborting."; exit 1; }

python3 - "$TMP" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1] + "/NetCEC.zip").extractall(sys.argv[1] + "/x")
PY
[ -f "$TMP/x/NetCEC/plugin.json" ] || { echo "Unexpected archive layout, aborting."; exit 1; }

echo ">> Installing to $DEST (the sudo password may be asked)"
sudo rm -rf "$DEST"
sudo mkdir -p "$(dirname "$DEST")"
sudo cp -r "$TMP/x/NetCEC" "$DEST"
sudo systemctl restart plugin_loader
echo ">> Done. Installed version: $(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$DEST/package.json" | head -1)"
