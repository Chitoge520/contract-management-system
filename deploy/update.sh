#!/usr/bin/env bash
# Pull the latest code from GitHub and restart.  sudo bash deploy/update.sh
# The data folder (contracts, users, settings) is never touched.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=contract-register
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash deploy/update.sh"; exit 1; }
cd "$APP_DIR"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
before="$(git rev-parse --short HEAD)"
git pull --ff-only origin main
after="$(git rev-parse --short HEAD)"
"$APP_DIR/.venv/bin/python" -m pip install -q -r requirements.txt
systemctl restart "$SERVICE"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
  echo "更新完成：$before -> $after，服务已重启。"
else
  journalctl -u "$SERVICE" -n 40 --no-pager
  echo "[ERROR] 服务没有正常启动，日志见上方。"
  exit 1
fi
