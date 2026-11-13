#!/data/data/com.termux/files/usr/bin/bash
# Re-base the paper book to a clean bankroll, WITHOUT throwing the old ledger away.
#
#   bash ~/solana-desk/termux/reset-balance.sh          # re-base to $50
#   bash ~/solana-desk/termux/reset-balance.sh 65       # re-base to $65
#
# Why: the paper book is at $13.01 after a -80% day that the old code allowed
# (the daily stop blocked new buys but never closed open positions). The code is
# fixed now, but a real paper loss cannot be un-lost by editing numbers. So this
# archives the current ledger to paper_ledger.<timestamp>.json and starts a fresh
# book at the amount you name. Nothing is deleted: the old file is still there if
# you want to inspect it.
#
# It does NOT change config.py. If you want the new bankroll to be the default
# for a brand-new install too, edit PAPER["starting_equity_usd"] in config.py.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
PORT="${PORT:-8000}"
AMOUNT="${1:-50}"

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }

# Kill ONLY the real uvicorn process. A bare `pkill -f "uvicorn app:app"` also
# matches any shell/log viewer whose command line merely mentions that string
# (and can kill the very script that is running), so filter on the process name.
stop_desk() {
  local stopped=0 pid comm
  for pid in $(pgrep -f "uvicorn app:app" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    comm="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    case "$comm" in
      python*|uvicorn*|python3*)
        kill "$pid" 2>/dev/null && { say "  stopped uvicorn (pid $pid)"; stopped=1; } ;;
    esac
  done
  [ "$stopped" -eq 0 ] && say "  no running server"
  return 0
}

case "$AMOUNT" in
  ''|*[!0-9.]*) say "FAIL: amount must be a number, got '$AMOUNT'"; exit 1 ;;
esac

say "=== 1. pulling the latest code ==="
git pull --ff-only || { say "FAIL: git pull failed (network or local changes)."; exit 1; }
say "  HEAD: $(git rev-parse --short HEAD)"

say ""
say "=== 2. balance BEFORE reset ==="
python scripts/check_ledger.py || true

say ""
say "=== 3. archiving the current ledger and re-basing to \$$AMOUNT ==="
python - "$AMOUNT" <<'PY'
import json, shutil, sys, time
from datetime import date
from pathlib import Path
sys.path.insert(0, ".")
from desk.ops import data_file

amount = float(sys.argv[1])
if amount <= 0:
    print("FAIL: amount must be > 0"); raise SystemExit(1)

path = data_file("paper_ledger.json")
if path.exists():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"paper_ledger.{stamp}.json")
    shutil.copy2(path, backup)
    print(f"  archived old ledger -> {backup}")
else:
    print("  no existing ledger; creating a fresh one")

state = {
    "cash": amount,
    "positions": {},
    "day": str(date.today()),
    "day_start": amount,
    "start_equity": amount,
    "closed": [],
}
path.write_text(json.dumps(state, indent=2))
print(f"  new book: cash ${amount:.2f}, start_equity ${amount:.2f}, no positions")
PY
[ $? -eq 0 ] || { say "FAIL: re-base failed"; exit 1; }

say ""
say "=== 4. restarting the desk ==="
stop_desk
sleep 1
if pgrep -f '[s]tart-desk.sh' >/dev/null 2>&1; then
  say "  supervisor is running; it will respawn uvicorn with the new code"
else
  nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
  say "  launched termux/start-desk.sh"
fi

i=0
while [ "$i" -lt 30 ]; do
  sleep 2; i=$((i + 2))
  pgrep -f '[u]vicorn app:app' >/dev/null 2>&1 && break
done
if pgrep -f '[u]vicorn app:app' >/dev/null 2>&1; then
  say "  uvicorn is up after ${i}s"
else
  say "  WARNING: uvicorn did not come up in 30s -- check: bash termux/verify-update.sh --log"
fi

say ""
say "=== done. open the dashboard and HARD REFRESH. ==="
say "  http://127.0.0.1:$PORT/?v=2"
say "  expected: equity \$$AMOUNT, day P&L \$0.00, 0 positions"
