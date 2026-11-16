#!/data/data/com.termux/files/usr/bin/bash
# Merge duplicate desk checkouts into one, WITHOUT losing the paper ledger.
#
# Why this exists: the repo is `solana-desk-` (trailing dash) but the docs and
# older installs used `solana-desk`. Two checkouts on one phone is a trap --
# you pull the fix into one and the scripts run the other, so the fix looks
# broken. Worse, the $46 paper book lives in whichever checkout was running, so
# a blind `rm -rf` of the "spare" would delete the account.
#
# What this does:
#   1. finds every real checkout (config.py + app.py)
#   2. picks the KEEPER: the one with the newest git commit (the one you pulled)
#   3. copies every runtime data file from the spares into the keeper, but only
#      when the keeper does not already have a newer one
#   4. archives each spare to ~/desk-backups/*.tar.gz
#   5. removes the spare only with --yes (or after you type yes)
#
# Usage:
#   bash termux/consolidate.sh            # report only, changes nothing
#   bash termux/consolidate.sh --yes      # migrate + archive + remove spares
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME"
BACKUPS="$HOME_DIR/desk-backups"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

say() { printf '%s\n' "$*"; }
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ---- 1. find checkouts -------------------------------------------------------
cands=()
for c in \
  "$HOME_DIR/solana-desk" \
  "$HOME_DIR/solana-desk-" \
  "$HOME_DIR/desk" \
  "$HOME_DIR/storage/shared/solana-desk" \
  "$HOME_DIR/storage/shared/solana-desk-" \
  "$HOME_DIR/storage/downloads/solana-desk" \
  "$HOME_DIR/storage/downloads/solana-desk-"
do
  [ -f "$c/config.py" ] && [ -f "$c/app.py" ] && cands+=("$c")
done

if [ "${#cands[@]}" -eq 0 ]; then
  die "no desk checkout found (looked for config.py + app.py in the usual places)"
fi
if [ "${#cands[@]}" -eq 1 ]; then
  say "Only one checkout: ${cands[0]}"
  say "Nothing to consolidate."
  exit 0
fi

say "Found ${#cands[@]} checkouts:"
for c in "${cands[@]}"; do
  commit="$(git -C "$c" log -1 --format='%h %ct %s' 2>/dev/null || echo 'no-git')"
  ledger="$c/paper_ledger.json"
  eq="—"
  if [ -f "$ledger" ]; then
    eq="$(python - "$ledger" <<'PY' 2>/dev/null || echo '?'
import json,sys
try:
    s=json.load(open(sys.argv[1]))
    cash=float(s.get("cash",0)); held=sum(float(p.get("qty",0))*float(p.get("last",0)) for p in (s.get("positions") or {}).values())
    print(f"${cash+held:,.2f}")
except Exception:
    print("unreadable")
PY
)"
  fi
  printf '  %-52s commit=%s  equity=%s\n' "$c" "$commit" "$eq"
done

# ---- 2. pick the keeper: newest commit wins --------------------------------
keeper="${cands[0]}"
best=-1
for c in "${cands[@]}"; do
  ct="$(git -C "$c" log -1 --format='%ct' 2>/dev/null || echo 0)"
  if [ "$ct" -gt "$best" ]; then best="$ct"; keeper="$c"; fi
done
say ""
say "KEEPER (newest commit): $keeper"

# ---- 3. plan the data migration -------------------------------------------
# Every runtime file that holds account state or history.
DATA_FILES=(
  paper_ledger.json live_ledger.json equity_curve.json
  operator_intent.json operator_settings.json proposals.json desk_chat.json
  journal.jsonl desk.db .desk_env KILL
)

spares=()
for c in "${cands[@]}"; do [ "$c" != "$keeper" ] && spares+=("$c"); done

say ""
say "Plan:"
for s in "${spares[@]}"; do
  for f in "${DATA_FILES[@]}"; do
    src="$s/$f"; dst="$keeper/$f"
    [ -f "$src" ] || continue
    if [ -f "$dst" ]; then
      # keep the newer file; never clobber a live account with a stale one
      if [ "$src" -nt "$dst" ]; then
        say "  copy  $s/$f -> keeper (source is newer)"
      else
        say "  keep  keeper/$f (already newer than $s/$f)"
      fi
    else
      say "  copy  $s/$f -> keeper (missing in keeper)"
    fi
  done
  say "  archive + remove $s"
done

if [ "$ASSUME_YES" -ne 1 ]; then
  say ""
  say "Report only. Nothing was changed."
  say "Re-run with --yes to migrate the data, archive the spares, and remove them:"
  say "  bash $HERE/consolidate.sh --yes"
  exit 0
fi

# ---- 4. stop the desk so nothing writes mid-migration ---------------------
say ""
if [ -f "$keeper/termux/stop-desk.sh" ]; then
  say "Stopping the desk..."
  DESK_DIR="$keeper" bash "$keeper/termux/stop-desk.sh" >/dev/null 2>&1 || true
fi

# ---- 5. migrate ------------------------------------------------------------
mkdir -p "$BACKUPS" || die "cannot create $BACKUPS"
stamp="$(date +%Y%m%d-%H%M%S)"

for s in "${spares[@]}"; do
  # archive the WHOLE spare first, so nothing is ever unrecoverable
  name="$(basename "$s")"
  tar -czf "$BACKUPS/${name}.${stamp}.tar.gz" -C "$(dirname "$s")" "$name" \
    || die "could not archive $s -- stopping before any delete"
  say "archived $s -> $BACKUPS/${name}.${stamp}.tar.gz"

  for f in "${DATA_FILES[@]}"; do
    src="$s/$f"; dst="$keeper/$f"
    [ -f "$src" ] || continue
    if [ -f "$dst" ] && [ ! "$src" -nt "$dst" ]; then
      continue          # keeper already newer
    fi
    cp -p "$src" "$dst" || die "could not copy $src -> $dst"
    say "  copied $f"
  done

  rm -rf "$s" || die "could not remove $s"
  say "  removed $s"
done

say ""
say "Done. One checkout remains: $keeper"
say "Backups: $BACKUPS"
say ""
say "Now restart with the fixed resolver:"
say "  bash $keeper/termux/start-desk.sh"
