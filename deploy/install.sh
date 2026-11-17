#!/usr/bin/env bash
# One-shot installer for Ubuntu 22.04 / 24.04.
#   sudo bash deploy/install.sh contracts.example.com you@example.com
# Sets up: Python venv, a systemd service (auto start / restart), Nginx reverse proxy,
# a free Let's Encrypt HTTPS certificate (auto renew) and the UFW firewall.
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=contract-register
APP_USER=cms
PORT=8000

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[ERROR] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/install.sh 你的域名 你的邮箱"
[ -n "$DOMAIN" ] || die "用法：sudo bash deploy/install.sh 你的域名 [你的邮箱]   例如 contracts.example.com"
[ -f "$APP_DIR/app/main.py" ] || die "没有在 $APP_DIR 找到程序代码"

say "1/7 安装系统组件 (Python, Nginx, Certbot)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx \
  ufw libgl1 libglib2.0-0 curl ca-certificates >/dev/null

say "2/7 创建运行账号 $APP_USER 和数据目录"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR/data" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data/files"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"

say "3/7 安装 Python 依赖（约 2-4 分钟）"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"
# warm up the OCR engine once so the first upload is fast and model files are in place
"$APP_DIR/.venv/bin/python" -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()" >/dev/null 2>&1 || true

say "4/7 注册系统服务 $SERVICE（开机自启、崩溃自动重启）"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Contract Register (合同管理系统)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $PORT --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null
sleep 3
systemctl is-active --quiet "$SERVICE" || { journalctl -u "$SERVICE" -n 40 --no-pager; die "服务启动失败，日志见上方"; }
curl -fsS "http://127.0.0.1:$PORT/api/auth/state" >/dev/null || die "程序没有响应，运行 journalctl -u $SERVICE -n 50 查看日志"

say "5/7 配置 Nginx"
LISTEN6=""
[ -s /proc/net/if_inet6 ] && LISTEN6="listen [::]:80;"
cat > "/etc/nginx/sites-available/$SERVICE" <<EOF
server {
    listen 80;
    $LISTEN6
    server_name $DOMAIN;

    client_max_body_size 200m;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header Referrer-Policy same-origin always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
EOF
ln -sf "/etc/nginx/sites-available/$SERVICE" "/etc/nginx/sites-enabled/$SERVICE"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

say "6/7 防火墙：只开放 SSH(22)、HTTP(80)、HTTPS(443)"
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

say "7/7 申请 HTTPS 证书（Let's Encrypt，自动续期）"
SERVER_IP="$(curl -fsS -4 https://api.ipify.org 2>/dev/null || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
if [ -n "$SERVER_IP" ] && [ "$SERVER_IP" != "$DNS_IP" ]; then
  echo "提示：域名 $DOMAIN 解析到 '${DNS_IP:-无}'，本机公网 IP 是 $SERVER_IP。"
  echo "      如果证书申请失败，请先把域名的 A 记录指向 $SERVER_IP，等几分钟后重新运行本脚本。"
fi
if [ -n "$EMAIL" ]; then CERT_MAIL=(-m "$EMAIL"); else CERT_MAIL=(--register-unsafely-without-email); fi
if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${CERT_MAIL[@]}"; then
  URL="https://$DOMAIN"
else
  URL="http://$DOMAIN"
  echo
  echo "[注意] HTTPS 证书暂时没申请成功（通常是域名还没解析到这台服务器，或云厂商安全组没放行 80/443）。"
  echo "       系统已经可以通过 $URL 访问；解决后重新运行本脚本即可自动开启 HTTPS。"
fi

printf '\n\033[1;32m部署完成！\033[0m\n'
echo "  访问地址：$URL"
echo "  第一次打开会要求创建管理员账号，然后到“设置”里填写 AI 的 API Key。"
echo
echo "  常用命令："
echo "    查看状态   systemctl status $SERVICE"
echo "    查看日志   journalctl -u $SERVICE -f"
echo "    重启       systemctl restart $SERVICE"
echo "    更新版本   sudo bash $APP_DIR/deploy/update.sh"
echo "    数据目录   $APP_DIR/data   （合同、数据库、API Key，请定期备份）"
