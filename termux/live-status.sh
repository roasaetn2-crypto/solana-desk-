#!/data/data/com.termux/files/usr/bin/bash
# Am I trading real money? One command, one verdict.
#
#   bash ~/solana-desk-/termux/live-status.sh
#   bash ~/solana-desk-/termux/live-status.sh --remote   # check Render instead
#
# The ONLY field that proves a live account exists is `wallet`. If it is null
# there is no key, and no key means no live account -- no matter what any other
# flag says. `live_ready: true` only means the GUARDS loaded, not that money
# can move. This script reads the four fields that matter and says plainly
# which rung of the ladder you are on.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
PORT="${PORT:-8000}"
REMOTE="${REMOTE:-https://solana-desk-1.onrender.com}"
TARGET="local"

for a in "$@"; do
  case "$a" in
    --remote) TARGET="remote" ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }
hr()  { say "------------------------------------------------------------"; }

if [ "$TARGET" = "remote" ]; then
  BASE="$REMOTE"
else
  BASE="http://127.0.0.1:$PORT"
fi

# /api/mode sits behind the desk key. Log in once into a temp cookie jar with
# the same key start-desk.sh uses, or the readout is just "unauthorized".
KEY="${DESK_TOKEN:-}"
if [ -z "$KEY" ] && [ -f "$HOME/.desk_key" ]; then
  KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
fi

JAR="$(mktemp 2>/dev/null || echo /tmp/live_status.$$)"
trap 'rm -f "$JAR" 2>/dev/null || true' EXIT

if [ -n "$KEY" ]; then
  curl -s -m 8 -c "$JAR" -o /dev/null -X POST \
       --data-urlencode "token=$KEY" "$BASE/login" 2>/dev/null || true
fi

BODY="$(curl -s -m 10 -b "$JAR" "$BASE/api/mode" 2>/dev/null)"
if [ -z "$BODY" ]; then
  say "FAIL: no answer from $BASE/api/mode"
  say "  is the desk running?   bash termux/verify-update.sh --start"
  say "  or check the log:      tail -40 $DESK_DIR/desk.log"
  exit 1
fi

hr
say "live-account check  ($TARGET: $BASE)"
hr

BODY="$BODY" python - <<'PY'
import json, os

raw = os.environ.get("BODY", "")
try:
    d = json.loads(raw)
except Exception:
    print("  could not parse /api/mode. Raw answer:")
    print("   ", raw[:300])
    raise SystemExit(1)

if d.get("detail") == "unauthorized":
    print("  UNAUTHORIZED -- the desk key did not match.")
    print("  the phone reads it from ~/.desk_key; if that file is missing,")
    print("  start the desk once so it creates one, then re-run.")
    raise SystemExit(1)

mode     = d.get("mode")
wallet   = d.get("wallet")
dry      = d.get("dry_run")
ready    = d.get("live_ready")
reason   = d.get("live_reason") or ""
caps     = d.get("caps") or {}
paper_eq = d.get("paper_equity")
live_pnl = d.get("live_pnl")

def row(k, v):
    print(f"  {k:<14} {v}")

row("mode", mode)
row("wallet", wallet if wallet else "null  <- NO KEY, NO LIVE ACCOUNT")
row("dry_run", dry)
row("live_ready", ready)
row("paper_equity", paper_eq)
row("live_pnl", live_pnl)
if caps:
    row("caps", "$%s/trade, $%s notional, $%s daily loss, %sbps" % (
        caps.get("max_trade"), caps.get("max_notional"),
        caps.get("daily_loss_usd"), caps.get("max_slippage_bps")))
if reason:
    print("  %-14s %s" % ("reason", reason))

print()
print("  VERDICT")

if not wallet:
    print("    PAPER ONLY. There is no live account: no private key is loaded,")
    print("    so nothing can be signed or sent. This is the safe default.")
    print()
    print("    to go live you must, on the host that runs the desk:")
    print("      1. add SOLANA_PRIVATE_KEY   (you do this, never me)")
    print("      2. set LIVE_TRADING_ENABLED=1")
    print("      3. set LIVE_DRY_RUN=0")
    print("      4. restart, then re-run this script")
elif dry:
    print("    LIVE ACCOUNT LOADED, BUT DRY RUN.")
    print("    the wallet %s can sign, but LIVE_DRY_RUN is on, so every" % wallet)
    print("    order is quoted and then refused before signing. No money moves.")
    print()
    print("    to actually trade: set LIVE_DRY_RUN=0 and restart.")
elif mode == "live":
    print("    LIVE AND ARMED. Real money can move.")
    print("    wallet %s signs real transactions through Jupiter." % wallet)
    print("    caps in force: $%s/trade, $%s total, $%s daily stop." % (
        caps.get("max_trade"), caps.get("max_notional"), caps.get("daily_loss_usd")))
    print("    to stop: create a file named KILL in the desk dir, or POST /api/mode paper.")
else:
    print("    LIVE-CAPABLE BUT IN PAPER MODE.")
    print("    the wallet %s is loaded and dry run is off, but the desk is" % wallet)
    print("    still on the paper book, so orders do not touch the chain.")
    print("    arm it from the dashboard (Arm button + the confirm phrase).")

print()
if live_pnl in (0, 0.0, "0", "0.0"):
    print("  note: live_pnl is 0.0 -- this wallet has never traded through the desk.")
PY

hr
