#!/data/data/com.termux/files/usr/bin/bash
# Which of my settings are hurting results?   bash termux/check-settings.sh
set -u
source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
BASE="http://127.0.0.1:${PORT:-8000}"
COOKIE=""
if [ -f "$HOME/.desk_key" ]; then
  KEY="$(tr -d '\r\n' < "$HOME/.desk_key")"
  [ -n "$KEY" ] && COOKIE="desk_session=$(python -c 'import hashlib,sys;print(hashlib.sha256(("desk::"+sys.argv[1]).encode()).hexdigest())' "$KEY")"
fi
curl -s --max-time 10 "$BASE/api/settings" ${COOKIE:+-H "Cookie: $COOKIE"} | python "$DESK_DIR/scripts/check_settings.py"
