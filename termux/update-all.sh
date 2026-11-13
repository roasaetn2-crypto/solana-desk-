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
  # NO_RESTART: step 5 does the single restart. Restarting here too meant the
  # server was killed and respawned twice, and the second kill could race the
  # supervisor's 10s backoff and leave the desk down.
  NO_RESTART=1 bash termux/reset-balance.sh "$AMOUNT" || { say "FAIL: reset failed."; exit 1; }
else
  hr; say "4/5  keeping the current balance (pass an amount to re-base)"; hr
  say "  e.g. bash termux/update-all.sh 50"
fi

hr; say "5/5  restart + health check"; hr
# Stop the supervisor AND uvicorn, then start a fresh supervisor. After a pull
# the old supervisor is still running the OLD script, and the old script had no
# singleton lock and no port check -- several of them would fight over port 8000
# and storm "[Errno 98] address already in use". A fresh start is the only way
# to be sure the new code is what is serving.
bash termux/stop-desk.sh || true
sleep 1
PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
say "  launched termux/start-desk.sh on port $PORT"

# A supervisor started earlier may be bound to a different port than $PORT.
# Rather than fail, read the port the server actually announced in desk.log.
probe() { curl -s -m 2 -o /dev/null "http://127.0.0.1:$1/api/health"; }
i=0
while [ "$i" -lt 40 ]; do
  sleep 2; i=$((i + 2))
  probe "$PORT" && break
done
if ! probe "$PORT"; then
  alt="$(grep -o 'Uvicorn running on http://[0-9.]*:[0-9]*' "$DESK_DIR/desk.log" 2>/dev/null \
        | tail -1 | sed 's/.*://')"
  if [ -n "$alt" ] && [ "$alt" != "$PORT" ] && probe "$alt"; then
    say "  note: the running server is on port $alt, not $PORT (stale supervisor)"
    PORT="$alt"
  fi
fi

if ! probe "$PORT"; then
  say "  FAIL: the desk did not answer on port $PORT after ${i}s"
  say "  look at: tail -40 $DESK_DIR/desk.log"
  exit 1
fi
say "  desk is up after ${i}s"

say ""
say "  balance now:"
# /api/state is behind the desk key. Log in once into a temp cookie jar with the
# same key start-desk.sh uses, otherwise the readout is just "unauthorized".
JAR="$(mktemp 2>/dev/null || echo /tmp/desk_cookies.$$)"
KEY="${DESK_TOKEN:-}"
if [ -z "$KEY" ] && [ -f "$HOME/.desk_key" ]; then
  KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
fi
if [ -n "$KEY" ]; then
  curl -s -m 5 -c "$JAR" -o /dev/null -X POST \
       --data-urlencode "token=$KEY" "http://127.0.0.1:$PORT/login" || true
  CURL_AUTH="-b $JAR"
else
  CURL_AUTH=""
fi
curl -s -m 8 $CURL_AUTH "http://127.0.0.1:$PORT/api/state" | python -c '
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
pp = ((d.get("integrations") or {}).get("pumpportal")) or d.get("pumpportal") or {}
if pp:
    print("    pump feed    " + str(pp.get("status")) + " (error: " + str(pp.get("error")) + ")")
'

rm -f "$JAR" 2>/dev/null || true

hr
say "done. HEAD $HEAD is running."
say "open the dashboard and HARD REFRESH:  http://127.0.0.1:$PORT/?v=2"
hr
