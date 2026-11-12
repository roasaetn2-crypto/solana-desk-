#!/usr/bin/env bash
# Put the desk behind a free Cloudflare Tunnel so you get a real HTTPS URL
# WITHOUT opening any inbound port on your VM.
#
# Run after oracle-setup.sh:
#
#   bash deploy/oracle/cloudflared-setup.sh
#
# No Cloudflare account or domain needed for quick-tunnel mode below. The URL
# is random and changes if the service restarts. For a stable URL see the
# "named tunnel" section of ORACLE-ALWAYS-FREE.md.
set -euo pipefail

PORT="${PORT:-8000}"
SERVICE="solana-desk-tunnel"
LOG="$HOME/desk-data/tunnel.log"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

mkdir -p "$HOME/desk-data"

if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64) CF=cloudflared-linux-arm64 ;;
    x86_64|amd64)  CF=cloudflared-linux-amd64 ;;
    *) echo "Unsupported arch: $ARCH"; exit 1 ;;
  esac
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" -o /tmp/cloudflared
  sudo install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared
  rm -f /tmp/cloudflared
fi
cloudflared --version

say "Installing tunnel service (quick tunnel, random URL)"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Cloudflare quick tunnel for the Solana Desk
After=network-online.target solana-desk.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$PORT
Restart=always
RestartSec=5
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"

say "Waiting for the tunnel URL"
URL=""
for i in $(seq 1 30); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "$URL" ]; then
  echo "No URL yet. Check: sudo journalctl -u $SERVICE -n 50"
  exit 1
fi

cat <<DONE

$(printf '\033[1;32m')============================================================
  Public HTTPS URL is live
============================================================$(printf '\033[0m')

  $URL

  Log in with your key:  cat ~/.desk_key

  NOTE: this URL is public. Anyone who finds it reaches the login page,
  so DESK_TOKEN is the only guard. It is long and random — fine — just
  do not share the URL.

  The URL CHANGES if the tunnel restarts. For a permanent URL you need
  a Cloudflare account + domain (named tunnel) — see the guide.

DONE
