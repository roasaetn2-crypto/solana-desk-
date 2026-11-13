#!/data/data/com.termux/files/usr/bin/bash
# One command that does EVERYTHING after a code change: pull, deps, ledger, restart.
#
#   bash ~/solana-desk/termux/update-all.sh          # keep the current balance
#   bash ~/solana-desk/termux/update-all.sh 50       # re-base the book to $50
#
# Steps, in order, and it stops on the first hard failure:
#   1. git pull                 -> get the latest code
#   2. fix-deps.sh              -> websocket-client etc. (feed on)
#   3. check_ledger --fix       -> heal a corrupt mark / impossible cash
#   4. reset-balance.sh <amt>   -> ONLY if you pass an amount
#   5. restart + health check   -> verify the new code is actually running
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
PORT="${PORT:-8000}"
AMOUNT="${1:-}"

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
hr()  { say "------------------------------------------------------------"; }

hr; say "1/5  pulling the latest code"; hr
git pull --ff-only || { say "FAIL: git pull failed. Fix local changes or network first."; exit 1; }
HEAD="$(git rev-parse --short HEAD)"
say "  HEAD: $HEAD"

hr; say "2/5  dependencies (websocket-client)"; hr
bash termux/fix-deps.sh || { say "FAIL: dependency install failed (see above)."; exit 1; }

hr; say "3/5  ledger check + repair"; hr
python scripts/check_ledger.py || true
say ""
python scripts/check_ledger.py --fix || { say "FAIL: ledger repair failed."; exit 1; }

if [ -n "$AMOUNT" ]; then
  hr; say "4/5  re-basing the book to \$$AMOUNT"; hr
  bash termux/reset-balance.sh "$AMOUNT" || { say "FAIL: reset failed."; exit 1; }
else
  hr; say "4/5  keeping the current balance (pass an amount to re-base)"; hr
  say "  e.g. bash termux/update-all.sh 50"
fi

hr; say "5/5  restart + health check"; hr
stop_desk
sleep 1
if pgrep -f '[s]tart-desk.sh' >/dev/null 2>&1; then
  say "  supervisor is running; it will respawn uvicorn"
else
  nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
  say "  launched termux/start-desk.sh"
fi

i=0
while [ "$i" -lt 40 ]; do
  sleep 2; i=$((i + 2))
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
done

if ! curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
  say "  FAIL: the desk did not answer on port $PORT after ${i}s"
  say "  look at: tail -40 $DESK_DIR/desk.log"
  exit 1
fi
say "  desk is up after ${i}s"

say ""
say "  balance now:"
curl -s -m 8 "http://127.0.0.1:$PORT/api/state" | python -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("    (could not read /api/state)"); raise SystemExit(0)
b = d.get("broker") or {}
print("    equity       $" + str(b.get("equity")))
print("    cash         $" + str(b.get("cash")))
print("    day P&L      $" + str(b.get("day_pnl")))
print("    positions    " + str(len(b.get("positions") or [])))
print("    start_equity $" + str(d.get("starting_equity")))
pp = d.get("pumpportal") or {}
if pp:
    print("    pump feed    " + str(pp.get("status")) + " (error: " + str(pp.get("error")) + ")")
'

hr
say "done. HEAD $HEAD is running."
say "open the dashboard and HARD REFRESH:  http://127.0.0.1:$PORT/?v=2"
hr
