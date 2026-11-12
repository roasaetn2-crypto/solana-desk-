#!/usr/bin/env bash
# One-shot setup for the Solana Desk on an Oracle Cloud Always Free VM.
#
# Run this ONCE on a fresh Ubuntu 22.04/24.04 instance, as a sudo-capable user:
#
#   bash deploy/oracle/oracle-setup.sh
#
# It is idempotent: re-running it upgrades the code and restarts the service
# without touching your token, your data, or your journal.
set -euo pipefail

REPO="${REPO:-https://github.com/roasaetn2-crypto/solana-desk-.git}"
DIR="${DIR:-$HOME/solana-desk}"
SERVICE="solana-desk"
PORT="${PORT:-8000}"
DATA_DIR="$HOME/desk-data"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- packages
say "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Python $PYVER detected"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' || {
  warn "Python 3.10+ required, found $PYVER"; exit 1; }

# ---------------------------------------------------------------- code
if [ -d "$DIR/.git" ]; then
  say "Updating existing checkout at $DIR"
  git -C "$DIR" fetch --quiet origin main
  git -C "$DIR" reset --hard --quiet origin/main
else
  say "Cloning $REPO into $DIR"
  git clone --quiet "$REPO" "$DIR"
fi

# ---------------------------------------------------------------- venv
say "Creating virtualenv and installing dependencies"
cd "$DIR"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip wheel
# On Linux aarch64 pydantic-core ships a prebuilt wheel, so this is fast.
# No Rust toolchain needed (unlike Termux).
./.venv/bin/pip install --quiet --no-cache-dir -r requirements.txt
say "Dependencies installed"

# ---------------------------------------------------------------- token
TOKEN_FILE="$HOME/.desk_key"
if [ ! -s "$TOKEN_FILE" ]; then
  ./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))' > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  say "Generated a new login key at $TOKEN_FILE"
else
  say "Reusing existing login key at $TOKEN_FILE"
fi
DESK_TOKEN="$(cat "$TOKEN_FILE")"

# ---------------------------------------------------------------- data dir
mkdir -p "$DATA_DIR"
say "Runtime data will live in $DATA_DIR (survives redeploys)"

# ---------------------------------------------------------------- systemd
say "Installing systemd service"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Solana Desk (paper trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
Environment=PORT=$PORT
Environment=DESK_TOKEN=$DESK_TOKEN
Environment=DESK_DATA_DIR=$DATA_DIR
Environment=PYTHONUNBUFFERED=1
Environment=SELF_PING=0
Environment=PAPER_AUTOSTART=1
Environment=PATH=$DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/env bash $DIR/start.sh
Restart=always
RestartSec=5
StandardOutput=append:$DATA_DIR/desk.log
StandardError=append:$DATA_DIR/desk.log

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"
say "Service enabled and started"

# ---------------------------------------------------------------- verify
say "Waiting for the desk to come up"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/health" || true)"
  if [ "$code" = "200" ]; then
    say "Health check passed (HTTP 200) after ${i}s"
    break
  fi
  sleep 1
  [ "$i" = "30" ] && warn "No 200 after 30s — check: sudo journalctl -u $SERVICE -n 50"
done

IP="$(curl -s --max-time 5 https://api.ipify.org || echo '<your-vm-ip>')"

cat <<DONE

$(printf '\033[1;32m')============================================================
  Solana Desk is running on Oracle Cloud
============================================================$(printf '\033[0m')

  Local health : http://127.0.0.1:$PORT/api/health
  Public IP    : $IP
  Dashboard    : http://$IP:$PORT   (only if you opened the port — see guide)

  Login key    : $DESK_TOKEN

  Data + logs  : $DATA_DIR
  Service      : sudo systemctl status $SERVICE
  Live logs    : tail -f $DATA_DIR/desk.log

  Useful:
    sudo systemctl restart $SERVICE                        # restart
    sudo systemctl stop    $SERVICE                        # stop
    git -C $DIR pull && sudo systemctl restart $SERVICE    # update

  NEXT: the port is NOT open yet. Either open 8000 in the VCN
  security list, or (safer) run deploy/oracle/cloudflared-setup.sh
  for a free HTTPS URL with no open ports.

DONE
