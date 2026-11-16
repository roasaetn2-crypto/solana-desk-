#!/data/data/com.termux/files/usr/bin/bash
# Turn the fast-scalp lane on or off in Termux, persistently, and restart the desk.
#
#   bash ~/solana-desk-/termux/scalp.sh on        # enable the 1-10 min scalper
#   bash ~/solana-desk-/termux/scalp.sh off       # back to the normal desk
#   bash ~/solana-desk-/termux/scalp.sh status    # show what is set + what is live
#   bash ~/solana-desk-/termux/scalp.sh on TP1=1.025 STOP=0.03   # override values
#
# Why a file and not just `export`: the supervisor respawns uvicorn (and Termux
# may restart it at boot). An export in your shell dies with that shell. We write
# $DESK_DIR/.desk_env, which termux/start-desk.sh sources on every start.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
ENV_FILE="$DESK_DIR/.desk_env"
PORT="${PORT:-8000}"
ACTION="${1:-status}"
shift 2>/dev/null || true

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }

# Keys we allow as KEY=VALUE overrides on the command line -> env var names.
env_name() {
  case "$1" in
    TP1)       echo SCALP_TP1_AT ;;
    TP2)       echo SCALP_TP2_AT ;;
    STOP)      echo SCALP_STOP_PCT ;;
    TP1FRAC)   echo SCALP_TP1_FRAC ;;
    HOLD)      echo SCALP_MAX_HOLD_MINUTES ;;
    TIMEOUT)   echo SCALP_TIMEOUT_MINUTES ;;
    ENTRY)     echo SCALP_ENTRY_MODE ;;
    MINAGE)    echo SCALP_MIN_AGE_MINUTES ;;
    MAXAGE)    echo SCALP_MAX_AGE_MINUTES ;;
    MINMCAP)   echo SCALP_MIN_MCAP_USD ;;
    MAXMCAP)   echo SCALP_MAX_MCAP_USD ;;
    MINLIQ)    echo SCALP_MIN_LIQUIDITY_USD ;;
    MINVOL)    echo SCALP_MIN_VOLUME_M5_USD ;;
    TRAIL)     echo SCALP_TRAIL_PCT ;;
    *)         echo "" ;;
  esac
}

write_env() {
  # $1 = on|off ; rest = KEY=VALUE overrides
  local mode="$1"; shift
  {
    echo "# written by termux/scalp.sh $(date -u +%FT%TZ)"
    if [ "$mode" = "on" ]; then
      echo "SCALP_ENABLED=1"
    else
      echo "SCALP_ENABLED=0"
    fi
    for kv in "$@"; do
      local k="${kv%%=*}" v="${kv#*=}" name
      name="$(env_name "$k")"
      if [ -z "$name" ]; then
        say "  skip: unknown key '$k' (try TP1 STOP TP2 HOLD TIMEOUT ENTRY MINAGE MAXAGE MINMCAP MAXMCAP MINLIQ MINVOL TRAIL)"
        continue
      fi
      echo "$name=$v"
    done
  } > "$ENV_FILE"
  say "  wrote $ENV_FILE"
  sed 's/^/    /' "$ENV_FILE"
}

restart() {
  say "  restarting the desk..."
  bash termux/stop-desk.sh >/dev/null 2>&1 || true
  sleep 1
  PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
  local i=0
  while [ "$i" -lt 40 ]; do
    sleep 2; i=$((i + 2))
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
  done
  if curl -s -m 3 "http://127.0.0.1:$PORT/api/health" | grep -q '"ok": *true'; then
    say "  desk is up after ${i}s"
  else
    say "  WARN: desk did not answer on $PORT; check: tail -40 $DESK_DIR/desk.log"
  fi
}

status() {
  say "env file: $ENV_FILE"
  if [ -f "$ENV_FILE" ]; then sed 's/^/  /' "$ENV_FILE"; else say "  (none - lane is OFF)"; fi
  say ""
  say "live health:"
  curl -s -m 5 "http://127.0.0.1:$PORT/api/health" \
    | python -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  (desk not answering on '"$PORT"')"); raise SystemExit
e=d.get("exits") or {}
print("  ok:",d.get("ok"),"| exits profile:",e.get("name"),"| requested:",e.get("requested"))
s=d.get("scalp")
print("  scalp:",json.dumps(s) if s else "(not reported by health)")' 2>/dev/null \
    || say "  (desk not answering on $PORT)"
}

case "$ACTION" in
  on)
    say "== enabling fast-scalp lane =="
    write_env on "$@"
    restart
    say ""
    say "open: http://127.0.0.1:$PORT/?v=2"
    ;;
  off)
    say "== disabling fast-scalp lane =="
    write_env off "$@"
    restart
    say ""
    say "open: http://127.0.0.1:$PORT/?v=2"
    ;;
  status)
    status
    ;;
  *)
    say "usage: bash termux/scalp.sh {on|off|status} [TP1=1.03 STOP=0.03 HOLD=10 ...]"
    exit 2
    ;;
esac
