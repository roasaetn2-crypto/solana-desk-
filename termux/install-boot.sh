#!/data/data/com.termux/files/usr/bin/bash
# Install the Termux:Boot hook so the desk comes back after a reboot.
#
# Without this, every reboot (and every time Android kills Termux) leaves the
# desk stopped until you open Termux and run start-desk.sh by hand. That is the
# most common reason "the desk is not running".
#
# Usage:
#   bash ~/solana-desk/termux/install-boot.sh
#   bash ~/solana-desk/termux/install-boot.sh --remove
#
# Needs the "Termux:Boot" app from F-Droid. It must be opened once after
# install so Android registers its boot receiver.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
BOOT_DIR="$HOME/.termux/boot"
HOOK="$BOOT_DIR/10-desk.sh"
SRC="$DESK_DIR/termux/boot-start.sh"

say() { printf '%s\n' "$*"; }

if [ "${1:-}" = "--remove" ]; then
  if [ -f "$HOOK" ]; then
    rm -f "$HOOK" && say "Removed $HOOK. The desk will no longer auto-start on boot."
  else
    say "Nothing to remove: $HOOK does not exist."
  fi
  exit 0
fi

# ---- sanity ----
[ -f "$SRC" ] || { say "FAIL: missing $SRC (run this from the repo)"; exit 1; }
[ -d "$DESK_DIR" ] || { say "FAIL: no desk dir at $DESK_DIR"; exit 1; }

# ---- is Termux:Boot actually installed? ----
# Its package is com.termux.boot. We cannot query pm without root, so check the
# filesystem marker Termux:Boot creates when it has been opened at least once.
BOOT_APP=""
for d in /data/data/com.termux.boot /data/user/0/com.termux.boot; do
  [ -d "$d" ] && BOOT_APP="$d" && break
done

if [ -z "$BOOT_APP" ]; then
  say "WARNING: Termux:Boot does not look installed (or was never opened)."
  say ""
  say "  1. Install 'Termux:Boot' from F-Droid (same source as Termux)."
  say "     https://f-droid.org/packages/com.termux.boot/"
  say "  2. Open it once, then come back and run this script again."
  say ""
  say "Continuing anyway: the hook file is harmless if the app is missing."
fi

# ---- install ----
mkdir -p "$BOOT_DIR" || { say "FAIL: could not create $BOOT_DIR"; exit 1; }
cp "$SRC" "$HOOK" || { say "FAIL: could not copy the hook"; exit 1; }
chmod 700 "$HOOK"
# Termux refuses to run boot scripts that are group/world writable.
chmod 700 "$BOOT_DIR" 2>/dev/null || true

say "Installed: $HOOK"
say "  -> $(head -1 "$HOOK")"
say ""

# ---- verify it will actually run ----
problems=0
[ -x "$HOOK" ] || { say "  MISS  hook is not executable"; problems=1; }
[ -f "$DESK_DIR/termux/start-desk.sh" ] || { say "  MISS  start-desk.sh is missing"; problems=1; }
grep -q "start-desk.sh" "$HOOK" || { say "  MISS  hook does not call start-desk.sh"; problems=1; }
command -v termux-wake-lock >/dev/null 2>&1 || say "  note  termux-wake-lock not found (install termux-api for the wake lock)"

if [ "$problems" -eq 0 ]; then
  say "  OK    hook is executable and points at start-desk.sh"
fi

say ""
say "Test it without rebooting:"
say "  bash $HOOK &"
say "  sleep 5 && pgrep -f '[u]vicorn app:app' && echo UP"
say ""
say "Also stop Android from killing it while the screen is off:"
say "  Settings -> Apps -> Termux -> Battery -> Unrestricted"
say "  (Xiaomi/Samsung/Huawei have an extra 'autostart' toggle: enable it)"
say ""
say "Remove it later with: bash termux/install-boot.sh --remove"
