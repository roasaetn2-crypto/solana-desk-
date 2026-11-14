#!/data/data/com.termux/files/usr/bin/bash
# collector-loop.sh - keep growing the candle store while the phone is awake.
#
# Why a loop and not cron: Termux's crond is unreliable on Android (the OS
# suspends it when the screen sleeps), and a missed run is invisible. A simple
# sleep loop you start yourself is easy to reason about, easy to stop, and logs
# every pass so you can see whether it is actually working.
#
# What it does each pass (see scripts/collector.py):
#   1. discover  - remember every pool in the ~6-minute new_pools feed
#   2. harvest   - fetch OHLCV for watched pools that have aged into 6-48h
#   3. prune     - drop pools that aged out unharvested
# Each pass is short (~10-30s). A pool seen young becomes harvestable hours
# later, so the loop only has to run often enough not to miss the window.
#
# Usage:
#   bash termux/collector-loop.sh              # run in foreground, Ctrl-C to stop
#   bash termux/collector-loop.sh --daemon     # background, logs to data/collector.log
#   bash termux/collector-loop.sh --once       # a single pass, then exit
#
# Check it is working:
#   python3 scripts/collector.py --status
#   tail -f data/collector.log

set -u

cd "$(dirname "$0")/.." || exit 1

INTERVAL="${COLLECTOR_INTERVAL:-1200}"   # 20 minutes
LOG="data/collector.log"
PROXY="${COLLECTOR_PROXY:-}"
PAGES="${COLLECTOR_PAGES:-2}"
FETCH="${COLLECTOR_FETCH:-12}"

MODE="loop"
case "${1:-}" in
  --daemon) MODE="daemon" ;;
  --once)   MODE="once" ;;
  "")       ;;
  *) echo "unknown option: $1"; exit 2 ;;
esac

if [ ! -f config.py ]; then
  echo "not in the repo root (config.py missing). cd into your solana-desk- clone."
  exit 2
fi

mkdir -p data

# Android suspends background work when the screen sleeps. Without a wake lock
# the loop simply stops and the 6-48h window is missed -- silently.
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  echo "wake lock held (termux-wake-lock)"
else
  echo "note: termux-wake-lock not found; install termux-api or the loop may"
  echo "      pause when the screen sleeps."
fi

ARGS="--discover-pages $PAGES --max-fetch $FETCH"
[ -n "$PROXY" ] && ARGS="$ARGS --proxy $PROXY"

pass() {
  printf '[%s] ' "$(date '+%Y-%m-%d %H:%M:%S')"
  python3 scripts/collector.py $ARGS 2>&1 | tail -3
}

if [ "$MODE" = "once" ]; then
  pass
  exit 0
fi

if [ "$MODE" = "daemon" ]; then
  echo "starting collector in background; log: $LOG"
  nohup bash "$0" >"$LOG" 2>&1 &
  echo "pid $! - stop with: pkill -f collector-loop.sh"
  exit 0
fi

echo "collector loop: every ${INTERVAL}s, proxy=${PROXY:-none}"
echo "stop with Ctrl-C"
while true; do
  pass
  sleep "$INTERVAL"
done
