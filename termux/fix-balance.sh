#!/data/data/com.termux/files/usr/bin/bash
# One command to repair a corrupted paper balance on the phone.
#
#   bash ~/solana-desk/termux/fix-balance.sh
#
# It does four things, in order, and stops if any step fails:
#   1. git pull            -> get the sell-price clamp + per-cycle repair
#   2. show the ledger     -> what the balance is BEFORE
#   3. repair in place     -> drop impossible closed rows, rebuild cash, reset bad marks
#   4. restart the desk    -> so the running process uses the new code
#
# It never touches your trade history beyond the corrupt rows, and it never
# resets the book to the config bankroll (that is what Reset does).
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
PORT="${PORT:-8000}"

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }


say "=== 1. pulling the latest code ==="
git pull --ff-only || { say "FAIL: git pull failed (network or local changes). Fix that first."; exit 1; }
say "  HEAD: $(git rev-parse --short HEAD)"

say ""
say "=== 2. balance BEFORE repair ==="
python scripts/check_ledger.py || true

say ""
say "=== 3. repairing in place ==="
python scripts/check_ledger.py --fix || { say "FAIL: repair failed"; exit 1; }

say ""
say "=== 4. restarting the desk ==="
# stop-desk.sh kills the supervisor BEFORE uvicorn, so nothing respawns behind us.
bash termux/stop-desk.sh || true
sleep 1
PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
say "  launched termux/start-desk.sh on port $PORT"

i=0
while [ "$i" -lt 30 ]; do
  sleep 2; i=$((i + 2))
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
done
if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
  say "  desk is up after ${i}s"
else
  say "  WARNING: the desk did not answer on port $PORT in 30s"
  say "           check: bash termux/verify-update.sh --log"
fi

say ""
say "=== done. open the dashboard and HARD REFRESH (the JS changed). ==="
say "  http://127.0.0.1:$PORT/?v=2"
say ""
say "If the equity is still wrong, paste the output of:"
say "  python scripts/check_ledger.py"
