#!/bin/bash
# Запускается на MacBook (или любом Linux/macOS). Собирает плагин локально и ставит на SteamOS-ПК по SSH.
#   bash deploy.sh [host]          сборка + установка
#   bash deploy.sh [host] logs     диагностика: логи Decky и плагина -> ~/Downloads/netcec-diag.txt
# host: IP или имя ПК. Если не указан, берётся из $DECK_HOST, затем из ~/.netcec_host, затем steamdeck.local
# Пользователь на ПК: $DECK_USER (по умолчанию deck).
set -e
cd "$(dirname "$0")"
HOST="${1:-${DECK_HOST:-$(cat "$HOME/.netcec_host" 2>/dev/null || echo steamdeck.local)}}"
MODE="${2:-deploy}"
RUSER="${DECK_USER:-deck}"
TARGET="$RUSER@$HOST"
SSHO="-o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPath=/tmp/netcec-ssh-%C -o ControlPersist=120"
echo ">> ПК: $TARGET"

if [ "$MODE" = "logs" ]; then
  OUT="$HOME/Downloads/netcec-diag.txt"
  ssh -t $SSHO "$TARGET" 'echo "### plugin_loader"; sudo journalctl -u plugin_loader -n 150 --no-pager | grep -iE -B2 -A12 "netcec|traceback|error" | tail -n 150; echo; echo "### logs dir"; ls -la ~/homebrew/logs/ ~/homebrew/logs/NetCEC/ 2>&1; echo; echo "### plugin log"; tail -n 80 ~/homebrew/logs/NetCEC/*.log 2>&1; echo; echo "### plugin dir"; ls -laR ~/homebrew/plugins/NetCEC/ 2>&1 | head -40' | tee "$OUT"
  echo ">> Сохранено: $OUT"
  exit 0
fi

# --- Node.js (системный, ранее скачанный или скачать в ~/.local/node)
if ! command -v npm >/dev/null 2>&1; then export PATH="$HOME/.local/node/bin:$PATH"; fi
if ! command -v npm >/dev/null 2>&1; then
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) NP="darwin-arm64" ;;
    Darwin-*)     NP="darwin-x64" ;;
    Linux-*)      NP="linux-x64" ;;
  esac
  echo ">> Node.js не найден, скачиваю в ~/.local/node ($NP)"
  mkdir -p "$HOME/.local/node"
  curl -fsSL "https://nodejs.org/dist/v20.18.0/node-v20.18.0-$NP.tar.gz" | tar -xz -C "$HOME/.local/node" --strip-components=1
fi
echo ">> node $(node -v)"

# --- pnpm (системный или временный через npx, ничего глобально не ставится)
if command -v pnpm >/dev/null 2>&1; then PNPM="pnpm"; else PNPM="npx --yes pnpm@9"; fi
echo ">> pnpm $($PNPM -v)"

# node_modules, установленные через npm, несовместимы с pnpm: пересоздаём
if [ -d node_modules ] && [ ! -d node_modules/.pnpm ]; then rm -rf node_modules; fi
if [ ! -d node_modules ] || [ package.json -nt node_modules ] || [ pnpm-lock.yaml -nt node_modules ]; then
  if [ -f pnpm-lock.yaml ]; then
    echo ">> pnpm install --frozen-lockfile"; $PNPM install --frozen-lockfile
  else
    echo ">> pnpm install (создаст pnpm-lock.yaml, его нужно закоммитить)"; $PNPM install
  fi
fi
echo ">> pnpm build"; $PNPM build
[ -f dist/index.js ] || { echo "Сборка не создала dist/index.js"; exit 1; }

echo ">> отправка файлов"
FILES="plugin.json package.json main.py dist py_modules"
for f in LICENSE THIRD_PARTY_NOTICES.md; do [ -f "$f" ] && FILES="$FILES $f"; done
ssh $SSHO "$TARGET" 'rm -rf /tmp/netcec-deploy && mkdir -p /tmp/netcec-deploy'
rsync -az -e "ssh $SSHO" $FILES "$TARGET:/tmp/netcec-deploy/"

echo ">> установка (спросит пароль sudo на ПК)"
ssh -t $SSHO "$TARGET" 'D="$HOME/homebrew/plugins/NetCEC"; sudo rm -rf "$D" && sudo mkdir -p "$D" && sudo cp -r /tmp/netcec-deploy/. "$D/" && sudo systemctl restart plugin_loader && echo OK'
echo "$HOST" > "$HOME/.netcec_host"
echo ">> Готово. Quick Access Menu (кнопка ...) -> NetCEC."
