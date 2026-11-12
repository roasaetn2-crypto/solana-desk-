#!/data/data/com.termux/files/usr/bin/bash
# Termux supervisor for the Solana Desk.
#
# Runs the desk 24/7 with:
#   - a wake lock so Android does not freeze it
#   - automatic restart if the process ever dies
#   - logs to ~/solana-desk/desk.log (rotated at 5 MB)
#   - the SAME login key every time (read from ~/.desk_key)
#
# Usage:
#   bash ~/solana-desk/termux/start-desk.sh
#
# Stop it with:  pkill -f start-desk.sh ; pkill -f uvicorn
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
KEY_FILE="$HOME/.desk_key"
LOG="$DESK_DIR/desk.log"
PORT="${PORT:-8000}"

cd "$DESK_DIR" || { echo "No desk dir at $DESK_DIR"; exit 1; }

# ---- login key: create once, reuse forever ----
if [ ! -s "$KEY_FILE" ]; then
  python -c 'import secrets; print(secrets.token_urlsafe(18))' > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi
export DESK_TOKEN="$(cat "$KEY_FILE")"

# ---- keep the CPU awake ----
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

# ---- rotate log if it grows past 5 MB ----
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv "$LOG" "$LOG.old"
fi

echo "==== $(date -u +%FT%TZ) supervisor start (port $PORT) ====" >> "$LOG"

# ---- restart loop ----
while true; do
  echo "---- $(date -u +%FT%TZ) launching uvicorn ----" >> "$LOG"
  python -m uvicorn app:app --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1
  code=$?
  echo "---- $(date -u +%FT%TZ) uvicorn exited (code $code); restarting in 10s ----" >> "$LOG"
  sleep 10
done
