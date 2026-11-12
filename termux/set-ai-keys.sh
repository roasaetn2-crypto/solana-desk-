#!/data/data/com.termux/files/usr/bin/bash
# Write the desk's AI keys into ~/solana-desk/.env, then restart the server.
#
# Why a script: pasting a multi-line block into Termux shreds it. The clipboard
# splits on newlines, each fragment gets pasted separately, and the shell ends up
# running garbage. One short command line is atomic and always survives.
#
# Usage:
#   bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [GEMINI_MODEL]
#
# Both keys are optional in the sense that you can pass "-" to leave one alone:
#   bash termux/set-ai-keys.sh - sk-or-v1-xxxx
#   bash termux/set-ai-keys.sh AIzaSyxxxx -
#
# .env is gitignored (.gitignore:27) so nothing here is ever committed.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
ENV_FILE="$DESK_DIR/.env"
PORT="${PORT:-8000}"
MODEL="${3:-gemini-3.5-flash-lite}"

GEMINI="${1:-}"
OPENROUTER="${2:-}"

cd "$DESK_DIR" || { echo "No desk dir at $DESK_DIR"; exit 1; }

# ---------- validate before touching anything ----------
if [ -z "$GEMINI" ] && [ -z "$OPENROUTER" ]; then
  echo "Usage: bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [MODEL]"
  echo "       pass - to leave a key unchanged"
  exit 2
fi

if [ "$GEMINI" != "-" ] && [ "$GEMINI" != "" ] && [ "${#GEMINI}" -lt 20 ]; then
  echo "That Gemini key looks too short (${#GEMINI} chars). Nothing changed."
  exit 2
fi
if [ "$OPENROUTER" != "-" ] && [ "$OPENROUTER" != "" ] && [ "${#OPENROUTER}" -lt 20 ]; then
  echo "That OpenRouter key looks too short (${#OPENROUTER} chars). Nothing changed."
  exit 2
fi

# ---------- remember what is already there ----------
touch "$ENV_FILE"
OLD_GEMINI="$(grep '^GEMINI_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_OPENROUTER="$(grep '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"

# ---------- back up, then rewrite only our three lines ----------
cp "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null
chmod 600 "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null

# grep -v keeps every other line, including DESK_TOKEN. Overwriting the file
# would wipe the login key and lock the owner out of their own desk.
grep -v -e '^GEMINI_API_KEY=' -e '^OPENROUTER_API_KEY=' -e '^BRAIN_GEMINI_MODEL=' \
  "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null
mv "$ENV_FILE.new" "$ENV_FILE"

# Keep a key that was already there when the caller passes "-".
if [ "$GEMINI" = "-" ] && [ -n "${OLD_GEMINI:-}" ]; then GEMINI="$OLD_GEMINI"; fi
if [ "$OPENROUTER" = "-" ] && [ -n "${OLD_OPENROUTER:-}" ]; then OPENROUTER="$OLD_OPENROUTER"; fi

if [ -n "$GEMINI" ] && [ "$GEMINI" != "-" ]; then
  echo "GEMINI_API_KEY=$GEMINI" >> "$ENV_FILE"
fi
if [ -n "$OPENROUTER" ] && [ "$OPENROUTER" != "-" ]; then
  echo "OPENROUTER_API_KEY=$OPENROUTER" >> "$ENV_FILE"
fi
echo "BRAIN_GEMINI_MODEL=$MODEL" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "=== .env (values masked) ==="
sed -E 's/^([A-Z_]+=)(.{6}).*/\1\2…/' "$ENV_FILE"
echo

# ---------- restart so load_env() picks the new values up ----------
# load_env() runs once at import, so a restart is required. Dashboard files are
# served fresh from disk and do not need one.
echo "=== restarting ==="
pkill -f "[u]vicorn app:app" 2>/dev/null
sleep 3

if pgrep -f "[s]tart-desk.sh" >/dev/null 2>&1; then
  echo "supervisor is running; it will respawn the server within 10s"
else
  echo "no supervisor; starting it now"
  nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
fi

# ---------- wait for it, then report ----------
i=0
while [ "$i" -lt 20 ]; do
  sleep 2
  i=$((i + 2))
  if curl -s --max-time 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    break
  fi
done

echo "=== health ==="
curl -s --max-time 8 "http://127.0.0.1:$PORT/api/health" | head -c 700
echo
echo
echo "Look for \"keys_present\":true. Then reload http://127.0.0.1:$PORT/?v=2#ai"
