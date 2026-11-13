#!/data/data/com.termux/files/usr/bin/bash
# Why is the desk not opening entries, and is take-profit armed?
#
# Usage:
#   bash ~/solana-desk/termux/why-no-entry.sh
#   bash ~/solana-desk/termux/why-no-entry.sh --hours 6
#   bash ~/solana-desk/termux/why-no-entry.sh --json
#
# Read-only: it never trades and never changes a setting.
# Exit 0 = no hard block, 2 = a hard block (daily stop / breaker / full book / TP off).
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "FAIL: python not found. Run: bash termux/fix-deps.sh"
  exit 1
fi

"$PY" scripts/why_no_entry.py "$@"
rc=$?
echo ""
if [ "$rc" -eq 2 ]; then
  echo "A hard gate is blocking entries. Apply the FIX line above, then re-run this."
elif [ "$rc" -eq 0 ]; then
  echo "No hard block. If entries still do not appear, read the reject tally above."
fi
exit "$rc"
