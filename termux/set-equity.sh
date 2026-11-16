#!/data/data/com.termux/files/usr/bin/bash
# Set the paper book's TOTAL equity to a number, WITHOUT closing any trade.
#
#   bash termux/set-equity.sh 46        # make equity read $46, keep every position
#   bash termux/set-equity.sh 46 --dry    # show the plan, change nothing
#   bash termux/set-equity.sh 46 --flat   # also mark positions to entry
#   bash termux/set-equity.sh 46 --scale  # shrink positions so they fit in $46
#
# The book is double-entry:  equity = cash + held
# Your example: total $10, buy a meme for $6 -> cash $4, held $6, equity $10.
# So this moves CASH; it never deletes a position or a closed trade.
#
# The ledger is archived first (paper_ledger.<stamp>.json), so nothing is lost.
# The server is stopped before the edit and restarted after, because the running
# process holds the ledger in memory and would overwrite the change on its next
# save.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
PORT="${PORT:-8000}"
TARGET="${1:-}"
shift 2>/dev/null || true
EXTRA="$*"          # --dry / --flat / --scale pass straight through
DRY=""
case " $EXTRA " in *" --dry "*) DRY="--dry" ;; esac

if [ -z "$TARGET" ]; then
  echo "usage: bash termux/set-equity.sh <target_total_usd> [--dry] [--flat] [--scale]"
  exit 2
fi

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

echo "=== 1. stopping the desk (it holds the ledger in memory) ==="
bash termux/stop-desk.sh || true
sleep 1

echo ""
echo "=== 2. re-basing total equity to \$$TARGET ==="
python scripts/set_equity.py "$TARGET" $EXTRA
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "FAIL: set_equity.py exited $rc -- restarting the desk unchanged"
  PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
  exit "$rc"
fi

if [ -n "$DRY" ]; then
  echo ""
  echo "--dry: desk left stopped. Start it with: bash termux/start-desk.sh"
  exit 0
fi

echo ""
echo "=== 3. restarting the desk ==="
PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &

i=0
while [ "$i" -lt 40 ]; do
  sleep 2; i=$((i + 2))
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
done

if ! curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
  echo "  WARNING: desk did not answer on port $PORT after ${i}s"
  echo "  check: bash termux/verify-update.sh --log"
  exit 1
fi
echo "  desk is up after ${i}s"

echo ""
echo "=== 4. what the desk now reports ==="
JAR="$(mktemp 2>/dev/null || echo /tmp/desk_c.$$)"
KEY="${DESK_TOKEN:-}"
[ -z "$KEY" ] && [ -f "$HOME/.desk_key" ] && KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
if [ -n "$KEY" ]; then
  curl -s -m 5 -c "$JAR" -o /dev/null -X POST \
       --data-urlencode "token=$KEY" "http://127.0.0.1:$PORT/login" || true
  AUTH="-b $JAR"
else
  AUTH=""
fi
curl -s -m 8 $AUTH "http://127.0.0.1:$PORT/api/state" | python -c '
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
'
rm -f "$JAR" 2>/dev/null || true

echo ""
echo "done. open the dashboard and HARD REFRESH:  http://127.0.0.1:$PORT/?v=2"
