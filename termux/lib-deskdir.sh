#!/data/data/com.termux/files/usr/bin/bash
# Find the desk checkout without assuming what it is called.
#
# Why this exists: every Termux helper used to hardcode $HOME/solana-desk, but
# the GitHub repo is named `solana-desk-` (trailing dash), so `git clone` creates
# $HOME/solana-desk- and every helper died with "No desk dir at ~/solana-desk".
# The clone name is not something the operator should have to remember, so we
# look for the thing that actually identifies a checkout: config.py next to app.py.
#
# Sourced by the other termux/*.sh scripts:
#   source "$(dirname "$0")/lib-deskdir.sh"
#   DESK_DIR="$(desk_dir)" || { echo "no desk checkout found"; exit 1; }
#
# DESK_DIR in the environment still wins, so a custom location keeps working.

desk_dir() {
  local c self

  # 1. explicit override, but only if it really is a checkout
  if [ -n "${DESK_DIR:-}" ] && [ -f "$DESK_DIR/config.py" ] && [ -f "$DESK_DIR/app.py" ]; then
    printf '%s' "$DESK_DIR"
    return 0
  fi

  # 2. the usual places, in order. Both spellings, because the repo name and the
  #    docs have disagreed since the beginning.
  #
  #    If MORE THAN ONE of these is a real checkout we warn on stderr. Silently
  #    picking the first one is how you end up pulling into ~/solana-desk- and
  #    then running a stale script out of ~/solana-desk, which looks exactly
  #    like "the fix did not work".
  local found=""
  for c in \
    "$HOME/solana-desk" \
    "$HOME/solana-desk-" \
    "$HOME/desk" \
    "$HOME/storage/shared/solana-desk" \
    "$HOME/storage/shared/solana-desk-" \
    "$HOME/storage/downloads/solana-desk" \
    "$HOME/storage/downloads/solana-desk-"
  do
    if [ -f "$c/config.py" ] && [ -f "$c/app.py" ]; then
      found="$found $c"
    fi
  done

  if [ -n "$found" ]; then
    set -- $found
    if [ "$#" -gt 1 ]; then
      printf 'WARNING: %s desk checkouts found:%s\n' "$#" "$found" >&2
      printf '         using %s\n' "$1" >&2
      printf '         the others are stale copies and will NOT be updated.\n' >&2
      printf '         to remove the confusion:  DESK_DIR=%s bash <script>\n' "$1" >&2
      printf '         or delete the spare:      rm -rf %s\n' "$2" >&2
    fi
    printf '%s' "$1"
    return 0
  fi

  # 3. the checkout this script lives inside (BASH_SOURCE[1] is the caller)
  self="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")/.." 2>/dev/null && pwd)" || self=""
  if [ -n "$self" ] && [ -f "$self/config.py" ] && [ -f "$self/app.py" ]; then
    printf '%s' "$self"
    return 0
  fi

  return 1
}

# Print a short, copy-pasteable explanation instead of a bare failure.
desk_dir_hint() {
  cat <<'EOF'
No desk checkout found.

Looked for config.py + app.py in:
  $HOME/solana-desk, $HOME/solana-desk-, $HOME/desk,
  $HOME/storage/shared/solana-desk{,-}, $HOME/storage/downloads/solana-desk{,-}

If you have not cloned it yet:
  pkg install -y git
  git clone https://github.com/roasaetn2-crypto/solana-desk-.git ~/solana-desk-

If it lives somewhere else, say so explicitly:
  DESK_DIR=/path/to/checkout bash ~/solana-desk-/termux/start-desk.sh
EOF
}
