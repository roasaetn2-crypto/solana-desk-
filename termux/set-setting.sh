#!/data/data/com.termux/files/usr/bin/bash
# Change one or more settings from Termux, no dashboard tapping.
#
#   bash termux/set-setting.sh risk.max_daily_loss_pct=0 breaker.cooldown_enabled=false
#
# Values: numbers, true/false, or text. It proposes the change, then confirms it (the same
# two steps the dashboard's Confirm button does), so every change is journaled.
set -u
source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
BASE="http://127.0.0.1:${PORT:-8000}"
[ $# -ge 1 ] || { echo "usage: bash termux/set-setting.sh key=value [key=value ...]"; exit 2; }

COOKIE=""
if [ -f "$HOME/.desk_key" ]; then
  KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
  [ -n "$KEY" ] && COOKIE="desk_session=$(python -c 'import hashlib,sys;print(hashlib.sha256(("desk::"+sys.argv[1]).encode()).hexdigest())' "$KEY")"
fi

BODY="$(python - "$@" <<'PY'
import json, sys
vals = {}
for a in sys.argv[1:]:
    if "=" not in a:
        print("BAD:" + a); raise SystemExit(0)
    k, v = a.split("=", 1)
    lo = v.strip().lower()
    if lo in ("true", "false"):
        vals[k.strip()] = (lo == "true")
    else:
        try:
            vals[k.strip()] = int(v) if v.strip().lstrip("-").isdigit() else float(v)
        except ValueError:
            vals[k.strip()] = v
print(json.dumps({"values": vals, "title": "Termux set-setting"}))
PY
)"
case "$BODY" in BAD:*) echo "not key=value: ${BODY#BAD:}"; exit 2;; esac

curl -s --max-time 5 "$BASE/api/health" >/dev/null 2>&1 || { echo "FAIL: the desk is not answering on $BASE (bash termux/start-desk.sh)"; exit 1; }
RESP="$(curl -s --max-time 20 -X POST "$BASE/api/proposals" -H "Content-Type: application/json" ${COOKIE:+-H "Cookie: $COOKIE"} -d "$BODY")"
PID="$(printf '%s' "$RESP" | python -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: print(""); raise SystemExit(0)
p=d.get("proposal") or {}
print(p.get("id") or d.get("id") or "")')"
if [ -z "$PID" ]; then echo "FAIL: no proposal. Desk said:"; printf '%s\n' "$RESP" | head -c 500; echo; exit 1; fi
CONF="$(curl -s --max-time 20 -X POST "$BASE/api/proposals/$PID/confirm" ${COOKIE:+-H "Cookie: $COOKIE"})"
printf '%s' "$CONF" | python -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: print("unreadable answer"); raise SystemExit(1)
if not d.get("ok"): print("NOT applied:", d.get("error") or d); raise SystemExit(1)
for a in d.get("applied") or []: print(f"  {a.get(\"label\")}: {a.get(\"from\")} -> {a.get(\"to\")}")
for r in d.get("dropped") or d.get("skipped") or []: print("  skipped:", r)
print("done")'
