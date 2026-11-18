#!/data/data/com.termux/files/usr/bin/bash
# Why is my win rate low?   bash termux/autopsy.sh
set -u
source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
cd "$DESK_DIR" && python scripts/trade_autopsy.py "$@"
