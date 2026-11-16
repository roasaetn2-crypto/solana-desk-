#!/data/data/com.termux/files/usr/bin/bash
# Apply a settings preset to the LOCAL desk, including the confirm step.
#
#   bash ~/solana-desk-/termux/apply-preset.sh super_legend_scalp
#   bash ~/solana-desk-/termux/apply-preset.sh legend_scalp
#   bash ~/solana-desk-/termux/apply-preset.sh --list
#
# Why this exists: the plain curl one-liner fails silently. Three things go
# wrong and none of them print an error:
#   1. the desk is not running            -> connection refused, -s hides it
#   2. DESK_TOKEN is set                  -> 401 {"detail":"unauthorized"}
#   3. the preset does not exist yet      -> 400, because the clone is old
# This script checks all three, computes the session cookie from ~/.desk_key,
# then POSTs the proposal and confirms it in one go.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
PRESET="${1:-}"

say() { printf '%s\n' "$*"; }
hr()  { say "------------------------------------------------------------"; }

if [ "$PRESET" = "--list" ] || [ "$PRESET" = "list" ]; then
  hr; say "presets available in this checkout"; hr
  python - "$DESK_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from desk.settings import PRESETS
except Exception as e:
    print("  cannot import desk.settings:", e); raise SystemExit(0)
for k, v in PRESETS.items():
    print(f"  {k:<22} {v['label']}")
PY
  exit 0
fi

if [ -z "$PRESET" ]; then
  say "usage: bash termux/apply-preset.sh <preset-name>"
  say "       bash termux/apply-preset.sh --list"
  exit 2
fi

# ---- 1. is the desk actually up? ----
hr; say "1/4  is the desk running on port $PORT?"; hr
if ! curl -s --max-time 5 "$BASE/api/health" >/dev/null 2>&1; then
  say "FAIL: nothing is answering on $BASE"
  say "  start it with:  bash $DESK_DIR/termux/start-desk.sh"
  say "  or check:       tail -40 $DESK_DIR/desk.log"
  exit 1
fi
say "  up."

# ---- 2. does this checkout know the preset? ----
hr; say "2/4  does this checkout have '$PRESET'?"; hr
HAVE="$(python - "$DESK_DIR" "$PRESET" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
try:
    from desk.settings import PRESETS
except Exception:
    print("no"); raise SystemExit(0)
print("yes" if sys.argv[2] in PRESETS else "no")
PY
)"
if [ "$HAVE" != "yes" ]; then
  say "FAIL: '$PRESET' is not in this checkout -- the code is too old."
  say "  update first:  bash $DESK_DIR/termux/update-all.sh"
  say "  then list:     bash $DESK_DIR/termux/apply-preset.sh --list"
  exit 1
fi
say "  yes."

# ---- 3. build the auth cookie (empty DESK_TOKEN = open desk) ----
hr; say "3/4  authenticating"; hr
COOKIE=""
if [ -f "$HOME/.desk_key" ]; then
  KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
  if [ -n "$KEY" ]; then
    SESS="$(python -c 'import hashlib,sys;print(hashlib.sha256(("desk::"+sys.argv[1]).encode()).hexdigest())' "$KEY")"
    COOKIE="desk_session=$SESS"
    say "  using ~/.desk_key"
  fi
fi
[ -z "$COOKIE" ] && say "  no ~/.desk_key -- assuming the desk is open"

# ---- 4. propose, then confirm ----
hr; say "4/4  proposing + confirming '$PRESET'"; hr
RESP="$(curl -s --max-time 20 -X POST "$BASE/api/proposals" \
  -H "Content-Type: application/json" \
  ${COOKIE:+-H "Cookie: $COOKIE"} \
  -d "{\"preset\":\"$PRESET\"}")"

if [ -z "$RESP" ]; then
  say "FAIL: empty response from the desk (see desk.log)"
  exit 1
fi

PID="$(printf '%s' "$RESP" | python -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit(0)
p=d.get("proposal") or {}
print(p.get("id") or d.get("id") or "")
')"

if [ -z "$PID" ]; then
  say "FAIL: no proposal id returned. Raw response:"
  printf '%s\n' "$RESP" | head -c 600; say ""
  exit 1
fi
say "  proposal: $PID"

CONF="$(curl -s --max-time 20 -X POST "$BASE/api/proposals/$PID/confirm" \
  ${COOKIE:+-H "Cookie: $COOKIE"})"

printf '%s' "$CONF" | python -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception:
    print("  FAIL: could not parse confirm response"); raise SystemExit(1)
if not d.get("ok"):
    print("  FAIL:", d.get("error") or d); raise SystemExit(1)
ap=d.get("applied") or []
print("  applied %d changes" % len(ap))
for a in ap[:8]:
    lab = a.get("label"); frm = a.get("from"); to = a.get("to")
    print("    %s: %s -> %s" % (lab, frm, to))
if len(ap)>8: print("    ... and %d more" % (len(ap)-8))
'

hr; say "now live -- verifying"; hr
curl -s --max-time 10 "$BASE/api/health" | python -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: print("  could not read /api/health"); raise SystemExit(0)
g=(d.get("desk") or {}).get("gates") or {}
print("  build        :", d.get("build"))
print("  min age      :", g.get("min_age_minutes"))
print("  mcap min     :", g.get("min_mcap_usd"))
print("  min liq      :", g.get("min_liquidity_usd"))
print("  min trades   :", g.get("min_trades_h1"))
print("  analyst score:", g.get("analyst_min_score"))
print("  scalp on     :", (d.get("scalp") or {}).get("enabled"),
      "stop", (d.get("scalp") or {}).get("stop_pct"))
'
say ""
say "If those match the preset, you are done."
