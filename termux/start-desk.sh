#!/data/data/com.termux/files/usr/bin/bash
# Termux supervisor for the Solana Desk.
#
# Runs the desk 24/7 with:
#   - ONE supervisor only (a lock dir; a second launch exits instead of fighting)
#   - a wake lock so Android does not freeze it
#   - automatic restart if the process ever dies, with backoff
#   - never spawning a second uvicorn onto a port that is already serving
#   - logs to ~/solana-desk/desk.log (rotated at 5 MB)
#   - the SAME login key every time (read from ~/.desk_key)
#
# Usage:
#   bash ~/solana-desk/termux/start-desk.sh
#
# Stop it with:  bash ~/solana-desk/termux/stop-desk.sh
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
KEY_FILE="$HOME/.desk_key"
LOG="$DESK_DIR/desk.log"
PORT="${PORT:-8000}"
LOCKDIR="$DESK_DIR/.supervisor.lock"

cd "$DESK_DIR" || { echo "No desk dir at $DESK_DIR"; exit 1; }

# ---- singleton: only ONE supervisor may run ----
# Without this, Termux:Boot + update-all.sh + reset-balance.sh each started a
# supervisor. They all ran `uvicorn --port 8000`, so all but one died with
# "[Errno 98] address already in use", and each death was respawned 10s later.
# That is the bind storm in desk.log. One lock, one supervisor.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  other="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    echo "supervisor already running (pid $other); not starting a second one"
    exit 0
  fi
  rm -rf "$LOCKDIR" 2>/dev/null || true
  mkdir "$LOCKDIR" 2>/dev/null || { echo "could not take the supervisor lock"; exit 0; }
fi
echo $$ > "$LOCKDIR/pid" 2>/dev/null || true
cleanup() { rm -rf "$LOCKDIR" 2>/dev/null || true; }
# TERM/INT/HUP must EXIT. A trap that only cleans up keeps the loop running, so
# `stop-desk.sh` had to escalate to SIGKILL after 5s.
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM HUP

# ---- login key: create once, reuse forever ----
if [ ! -s "$KEY_FILE" ]; then
  python -c 'import secrets; print(secrets.token_urlsafe(18))' > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi
export DESK_TOKEN="$(cat "$KEY_FILE")"

# ---- dependency preflight: warn once, never block the desk ----
python - <<'PY' 2>/dev/null || true
try:
    import websocket
    if not hasattr(websocket, "create_connection"):
        raise ImportError("wrong package")
except Exception:
    print("WARNING: websocket-client is missing or wrong -> the PumpPortal feed "
          "will be off (the REST scanner still works).")
    print("         fix: bash termux/fix-deps.sh")
PY

# ---- keep the CPU awake ----
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

# ---- rotate log if it grows past 5 MB ----
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv "$LOG" "$LOG.old"
fi

# `sleep` in the foreground makes bash DEFER the TERM trap until it finishes, so
# `stop-desk.sh` could wait up to 15s for the supervisor to die. Sleeping in the
# background and `wait`ing is interruptible: the trap runs immediately.
nap() { sleep "$1" & wait $! 2>/dev/null || true; }

# Is something already answering on the port? If so it is a healthy desk and we
# must NOT launch a second uvicorn at it.
port_busy() {
  python - "$1" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    print("busy")
except Exception:
    print("free")
finally:
    s.close()
PY
}

echo "==== $(date -u +%FT%TZ) supervisor start (port $PORT, pid $$) ====" >> "$LOG"

# ---- restart loop ----
fails=0
while true; do
  if [ "$(port_busy "$PORT")" = "busy" ]; then
    # Another uvicorn owns the port (a leftover, or a second supervisor from an
    # older build). Do not fight it: the desk is serving, which is the goal.
    echo "---- $(date -u +%FT%TZ) port $PORT already serving; waiting ----" >> "$LOG"
    nap 15
    continue
  fi

  echo "---- $(date -u +%FT%TZ) launching uvicorn ----" >> "$LOG"
  started=$(date +%s)
  python -m uvicorn app:app --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1
  code=$?
  ran=$(( $(date +%s) - started ))

  # A run that lasted a while was healthy; reset the backoff.
  if [ "$ran" -ge 60 ]; then
    fails=0
  else
    fails=$((fails + 1))
  fi

  # 2, 4, 8, 16, 32, 60, 60... seconds. A crash loop must not hammer the CPU.
  delay=$(( 2 ** (fails > 5 ? 5 : fails) ))
  [ "$delay" -gt 60 ] && delay=60
  [ "$delay" -lt 2 ] && delay=2

  echo "---- $(date -u +%FT%TZ) uvicorn exited (code $code) after ${ran}s; restart in ${delay}s (fail #$fails) ----" >> "$LOG"
  nap "$delay"
done
