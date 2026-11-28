#!/usr/bin/env bash
# NetCEC installer / updater for the SteamOS machine (run it in a Desktop Mode terminal or over SSH).
#   curl -fsSL https://raw.githubusercontent.com/MSPortnov/NetCEC/main/install.sh | bash
# Downloads the latest GitHub release, installs it to ~/homebrew/plugins/NetCEC and restarts Decky.
# Running it again updates the plugin. Settings and pairing are kept (stored outside the plugin folder).
set -euo pipefail

BASE="${NETCEC_BASE:-https://github.com/MSPortnov/NetCEC/releases/latest/download}"
DEST="$HOME/homebrew/plugins/NetCEC"

if [[ ${1:-} == --from-directory && $# == 2 ]]; then
    SOURCE=$2
elif [[ $# == 0 ]]; then
    command -v curl >/dev/null    || { echo "curl not found"; exit 1; }
    command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

    TMP="$(mktemp -d)"
    trap 'rm -rf -- "$TMP"' EXIT

    echo ">> Downloading the latest NetCEC release"
    curl -fsSL "$BASE/NetCEC.zip" -o "$TMP/NetCEC.zip"
    curl -fsSL "$BASE/NetCEC.zip.sha256" -o "$TMP/NetCEC.zip.sha256"
    (cd "$TMP" && sha256sum -c NetCEC.zip.sha256 >/dev/null) || { echo "Checksum mismatch, aborting."; exit 1; }

    python3 - "$TMP" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1] + "/NetCEC.zip").extractall(sys.argv[1] + "/x")
PY
    SOURCE="$TMP/x/NetCEC"
else
    echo "Usage: bash install.sh [--from-directory DIR]" >&2
    exit 2
fi

for file in plugin.json package.json main.py dist/index.js; do
    if [[ ! -s "$SOURCE/$file" ]]; then
        echo "Missing or empty plugin file: $file" >&2
        exit 1
    fi
done
[[ -d "$SOURCE/py_modules" ]] || { echo "Missing py_modules directory." >&2; exit 1; }

echo ">> Installing to $DEST (the sudo password may be asked)"
sudo bash -s -- "$SOURCE" "$DEST" <<'BASH'
set -euo pipefail
source_dir=$1
dest=$2
parent=$(dirname -- "$dest")
mkdir -p -- "$parent"
exec 9<"$parent"
flock 9
stage=$(mktemp -d "$parent/.netcec-stage.XXXXXXXX")

cleanup() {
    if [[ -e "$stage/previous" || -L "$stage/previous" ]] && [[ ! -e "$dest" && ! -L "$dest" ]]; then
        if ! mv -T -- "$stage/previous" "$dest"; then
            echo "Previous installation is available at $stage/previous" >&2
            return 1
        fi
    fi
    rm -rf -- "$stage"
}
trap cleanup EXIT

mkdir -- "$stage/new"
cp -R -- "$source_dir/." "$stage/new/"
if [[ -e "$dest" || -L "$dest" ]]; then
    mv -T -- "$dest" "$stage/previous"
fi
mv -T -- "$stage/new" "$dest"
systemctl restart plugin_loader
BASH
echo ">> Done. Installed version: $(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$DEST/package.json" | head -1)"
