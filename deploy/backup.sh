#!/usr/bin/env bash
# Daily backup of the data folder (database + original contracts + settings).
#   sudo bash deploy/backup.sh            -> one backup now
#   sudo bash deploy/backup.sh --install  -> also schedule it every night at 02:30, keep 14 days
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/var/backups/contract-register
KEEP_DAYS=14
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
mkdir -p "$DEST"
chmod 700 "$DEST"
STAMP="$(date +%Y%m%d_%H%M)"
TMP="$(mktemp -d)"
# copy everything except the live database files, then add a consistent snapshot of the database
(cd "$APP_DIR" && tar -cf - --exclude='data/contracts.db*' data) | tar -xf - -C "$TMP"
"$APP_DIR/.venv/bin/python" - "$APP_DIR/data/contracts.db" "$TMP/data/contracts.db" <<'PY'
import sqlite3, sys, os
if os.path.exists(sys.argv[1]):
    s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()
PY
tar -czf "$DEST/data_$STAMP.tar.gz" -C "$TMP" data
chmod 600 "$DEST/data_$STAMP.tar.gz"
rm -rf "$TMP"
find "$DEST" -name 'data_*.tar.gz' -mtime +$KEEP_DAYS -delete
echo "备份完成：$DEST/data_$STAMP.tar.gz"
if [ "${1:-}" = "--install" ]; then
  echo "30 2 * * * root bash $APP_DIR/deploy/backup.sh >/dev/null 2>&1" > /etc/cron.d/contract-register-backup
  echo "已设置每天 02:30 自动备份，保留 $KEEP_DAYS 天，位置 $DEST"
fi
