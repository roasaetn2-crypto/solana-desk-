#!/data/data/com.termux/files/usr/bin/bash
# Put the LIVE account on THIS phone, safely, in one command.
#
#   bash ~/solana-desk-/termux/live-arm.sh          # arm live on the phone
#   bash ~/solana-desk-/termux/live-arm.sh --check  # only report what is missing
#   bash ~/solana-desk-/termux/live-arm.sh --off    # go back to paper, keep the key
#   bash ~/solana-desk-/termux/live-arm.sh --wipe   # remove the key from the phone
#
# What it does, in order, and it stops on the first hard failure:
#   1. checks solders + base58 are installed (live signing needs them)
#   2. asks for the wallet key WITHOUT echoing it to the screen
#   3. decodes it and prints ONLY the public address, for you to confirm
#   4. writes LIVE_TRADING_ENABLED=1 + SOLANA_PRIVATE_KEY to .desk_env (chmod 600)
#   5. restarts the desk and reads /api/mode back to prove it armed
#
# The key never goes on a command line (that would put it in shell history and
# in `ps` output for every other process on the phone). It is read with
# `read -s` and written straight to a 0600 file.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
ENV_FILE="$DESK_DIR/.desk_env"
PORT="${PORT:-8000}"
MODE="arm"

case "${1:-}" in
  --check) MODE="check" ;;
  --off)   MODE="off" ;;
  --wipe)  MODE="wipe" ;;
  --help)  sed -n '2,17p' "$0"; exit 0 ;;
  "")      ;;
  *)       echo "unknown option: $1 (try --help)"; exit 2 ;;
esac

say() { printf '%s\n' "$*"; }
hr()  { say "------------------------------------------------------------"; }

cd "$DESK_DIR" || { say "FAIL: no desk dir at $DESK_DIR"; exit 1; }
say "checkout: $DESK_DIR"
say "HEAD    : $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  say "WARNING : this checkout has local edits; a later git pull may refuse."
  say "          run: bash termux/verify-update.sh --fix"
fi

# ---------- .desk_env helpers ----------
# The file is a plain `KEY=value` shell fragment sourced by start-desk.sh with
# `set -a`. We must MERGE, never overwrite: scalp.sh writes SCALP_ENABLED here
# and losing it would silently change the trading lane.
env_drop() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}

env_set() {
  local key="$1" val="$2"
  env_drop "$key"
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

have() { python -c "import $1" >/dev/null 2>&1; }

# ---------- --wipe ----------
if [ "$MODE" = "wipe" ]; then
  hr; say "removing the wallet key from this phone"; hr
  env_drop SOLANA_PRIVATE_KEY
  env_drop LIVE_TRADING_ENABLED
  say "  .desk_env no longer contains a key or the live opt-in."
  say "  Restarting so the running process forgets them."
  bash termux/stop-desk.sh >/dev/null 2>&1 || true
  bash termux/start-desk.sh >/dev/null 2>&1 || true
  say "  done. The phone is paper-only again."
  exit 0
fi

# ---------- --off ----------
if [ "$MODE" = "off" ]; then
  hr; say "back to paper (key kept, live opt-in removed)"; hr
  env_drop LIVE_TRADING_ENABLED
  say "  LIVE_TRADING_ENABLED removed from .desk_env"
  bash termux/stop-desk.sh >/dev/null 2>&1 || true
  bash termux/start-desk.sh >/dev/null 2>&1 || true
  say "  restarted in paper."
  exit 0
fi

# ---------- dependency gate ----------
hr; say "1/5  live signing dependencies"; hr
MISSING=""
have solders || MISSING="$MISSING solders"
have base58  || MISSING="$MISSING base58"
if [ -n "$MISSING" ]; then
  say "  MISSING:$MISSING"
  say ""
  say "  Live signing needs these. They are NOT in requirements.txt on purpose:"
  say "  solders is an ~12 MB Rust extension with no Android wheel, so installing"
  say "  it compiles from source on the phone (5-20 minutes, needs rust + clang)."
  say ""
  say "  Install them with:"
  say "    pkg install -y rust clang binutils libffi openssl"
  say "    bash termux/fix-deps.sh --live"
  say ""
  say "  Then re-run this script."
  exit 1
fi
say "  solders: ok    base58: ok"

# ---------- --check stops here ----------
if [ "$MODE" = "check" ]; then
  hr; say "2/5  current state"; hr
  if grep -q '^SOLANA_PRIVATE_KEY=' "$ENV_FILE" 2>/dev/null; then
    say "  key: present in .desk_env"
  else
    say "  key: NOT set"
  fi
  if grep -q '^LIVE_TRADING_ENABLED=' "$ENV_FILE" 2>/dev/null; then
    say "  live opt-in: ON"
  else
    say "  live opt-in: off (paper)"
  fi
  exit 0
fi

# ---------- key entry ----------
hr; say "2/5  wallet key"; hr
say "  Paste your Phantom/Solflare base58 secret key."
say "  It will NOT appear on screen, and it is not stored in shell history."
say ""
printf '  key: '
IFS= read -rs RAW_KEY
say ""
RAW_KEY="$(printf '%s' "$RAW_KEY" | tr -d '[:space:]')"
if [ -z "$RAW_KEY" ]; then
  say "  FAIL: nothing entered."; exit 1
fi

# ---------- decode + derive the address ----------
hr; say "3/5  checking the key"; hr
ADDR="$(RAW_KEY="$RAW_KEY" python - <<'PY'
import os, sys
raw = os.environ.get("RAW_KEY", "").strip()
try:
    import base58
except ImportError:
    print("ERR:base58 missing"); sys.exit(0)
try:
    b = base58.b58decode(raw)
except Exception:
    print("ERR:not valid base58"); sys.exit(0)
if len(b) != 64:
    print(f"ERR:decoded to {len(b)} bytes, expected 64"); sys.exit(0)
print(base58.b58encode(b[32:]).decode())
PY
)"
case "$ADDR" in
  ERR:*) say "  FAIL: ${ADDR#ERR:}"; exit 1 ;;
esac
say "  valid 64-byte keypair"
say ""
say "  This key controls this wallet:"
say ""
say "      $ADDR"
say ""
say "  Check it matches the wallet you funded. If it does not, press Ctrl-C now."
printf '  type YES to continue: '
IFS= read -r CONFIRM
[ "$CONFIRM" = "YES" ] || { say "  aborted, nothing written."; exit 1; }

# ---------- write .desk_env ----------
hr; say "4/5  writing .desk_env (chmod 600)"; hr
env_set SOLANA_PRIVATE_KEY "$RAW_KEY"
env_set LIVE_TRADING_ENABLED "1"
unset RAW_KEY
say "  SOLANA_PRIVATE_KEY   written (value hidden)"
say "  LIVE_TRADING_ENABLED = 1"
say "  .desk_env is now readable only by this user."

# ---------- restart + prove it ----------
hr; say "5/5  restart + verify"; hr
bash termux/stop-desk.sh >/dev/null 2>&1 || true
sleep 2
bash termux/start-desk.sh >/dev/null 2>&1 || true

say "  waiting for the desk to answer..."
BODY=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 3
  BODY="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/mode" 2>/dev/null || true)"
  [ -n "$BODY" ] && break
done

if [ -z "$BODY" ]; then
  say "  could not reach http://127.0.0.1:$PORT/api/mode"
  say "  the key IS saved. Check the log:  tail -40 $DESK_DIR/desk.log"
  exit 1
fi

BODY="$BODY" python - <<'PY'
import json, os
try:
    d = json.loads(os.environ["BODY"])
except Exception:
    print("  could not parse /api/mode"); raise SystemExit(0)
w = d.get("wallet")
print(f"  wallet      : {w}")
print(f"  live_ready  : {d.get('live_ready')}")
print(f"  dry_run     : {d.get('dry_run')}")
print(f"  mode        : {d.get('mode')}")
print(f"  reason      : {d.get('live_reason')}")
print("")
if not w:
    print("  VERDICT: no live account. The key did not load.")
    print("           Look at the reason above; usually a missing dependency.")
elif d.get("mode") == "live":
    print("  VERDICT: LIVE AND ARMED on this phone. Real money can move.")
else:
    print("  VERDICT: live account loaded, still in PAPER mode.")
    print("           Tap 'Arm live...' on http://127.0.0.1:8000 to arm it.")
PY

say ""
say "  Note: the desk always boots in paper after a restart. Arming live is a"
say "  separate, deliberate tap -- that is the safety ladder, not a bug."
