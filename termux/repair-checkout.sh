#!/data/data/com.termux/files/usr/bin/bash
# Force the local Termux checkout onto the canonical repo/branch, KEEPING your data.
#
# Why this exists: the phone was running `build 970c528e38`, a commit that does
# not exist in roasaetn2-crypto/solana-desk- at all. The checkout had drifted
# onto a different lineage (the old Roms098 repo), so `git pull --ff-only`
# could never bring the new code in -- it either said "already up to date" or
# "divergent branches". This script repoints the remote and hard-syncs.
#
#   bash ~/solana-desk-/termux/repair-checkout.sh
#
# It backs up journal/ledger/settings to ~/desk-backup-<timestamp> first, so a
# hard reset can never destroy your paper book.
set -u

source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
CANON="https://github.com/roasaetn2-crypto/solana-desk-.git"
PORT="${PORT:-8000}"

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }
hr()  { say "------------------------------------------------------------"; }

hr; say "0/5  what is here now"; hr
say "  desk dir : $DESK_DIR"
say "  build    : $(git rev-parse --short=10 HEAD 2>/dev/null || echo none)"
say "  remote   : $(git remote get-url origin 2>/dev/null || echo none)"
say "  branch   : $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo none)"

hr; say "1/5  backing up your state"; hr
BK="$HOME/desk-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK" || { say "FAIL: cannot create $BK"; exit 1; }
for f in .desk_env .desk_key operator_settings.json proposals.json \
         journal.jsonl paper_ledger.json live_ledger.json ledger.json desk.db; do
  if [ -e "$DESK_DIR/$f" ]; then
    cp -a "$DESK_DIR/$f" "$BK/" 2>/dev/null && say "  saved $f"
  fi
done
if [ -d "$DESK_DIR/data" ]; then
  cp -a "$DESK_DIR/data" "$BK/" 2>/dev/null && say "  saved data/"
fi
say "  -> $BK"

hr; say "2/5  stopping the desk"; hr
bash termux/stop-desk.sh >/dev/null 2>&1 || true
sleep 1
say "  stopped"

hr; say "3/5  repointing to the canonical repo + hard sync"; hr
git remote set-url origin "$CANON" || { say "FAIL: could not set remote"; exit 1; }
git fetch --prune origin || { say "FAIL: git fetch failed (network or auth)."; exit 1; }
# -B main origin/main moves the local branch onto the canonical history even if
# the old lineage shares no ancestor. -f discards local edits and any untracked
# file that would block the checkout (your data is already backed up above).
git checkout -f -B main origin/main || { say "FAIL: checkout failed."; exit 1; }
git reset --hard origin/main || { say "FAIL: reset failed."; exit 1; }
NEW="$(git rev-parse --short=10 HEAD)"
say "  now at: $NEW  $(git log -1 --format=%s)"

hr; say "4/5  restoring your state"; hr
for f in .desk_env .desk_key operator_settings.json proposals.json \
         journal.jsonl paper_ledger.json live_ledger.json ledger.json desk.db; do
  if [ -e "$BK/$f" ] && [ ! -e "$DESK_DIR/$f" ]; then
    cp -a "$BK/$f" "$DESK_DIR/$f" 2>/dev/null && say "  restored $f"
  fi
done
if [ -d "$BK/data" ] && [ ! -d "$DESK_DIR/data" ]; then
  cp -a "$BK/data" "$DESK_DIR/data" 2>/dev/null && say "  restored data/"
fi
say "  (existing files were left untouched by the reset)"

hr; say "5/5  restart + verify the running build"; hr
PORT="$PORT" nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
i=0
while [ "$i" -lt 60 ]; do
  sleep 2; i=$((i + 2))
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
done
LIVE="$(curl -s -m 5 "http://127.0.0.1:$PORT/api/health" \
        | python -c 'import json,sys
try: print(json.load(sys.stdin).get("build") or "?")
except Exception: print("?")' 2>/dev/null)"
say "  disk build : $NEW"
say "  live build : $LIVE"
if [ "$LIVE" = "$NEW" ]; then
  say "  OK - the running desk is the new code"
else
  say "  WARN - live build does not match disk. Check: tail -40 $DESK_DIR/desk.log"
fi

hr
say "next:"
say "  bash termux/scalp.sh on      # turn the 1-10 min scalper on"
say "  open http://127.0.0.1:$PORT/?v=2"
hr
