#!/data/data/com.termux/files/usr/bin/bash
# Write the desk's AI keys into ~/solana-desk/.env, then restart the server.
#
# Why a script: pasting a multi-line block into Termux shreds it. The clipboard
# splits on newlines, each fragment gets pasted separately, and the shell ends up
# running garbage. One short command line is atomic and always survives.
#
# Usage:
#   bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [GEMINI_MODEL] [CEREBRAS_KEY] [CF_ACCOUNT_ID] [CF_TOKEN]
#
# A fourth free provider (Cerebras) is optional and goes fifth:
#   bash termux/set-ai-keys.sh - - - csk-xxxxxxxx
#
# Cloudflare Workers AI is the biggest free quota (10,000 Neurons/day) and the
# only provider that needs two values, so it goes last:
#   bash termux/set-ai-keys.sh - - - - <account_id> <api_token>
# Account id: dash.cloudflare.com -> Workers & Pages -> Account ID.
# Token: My Profile -> API Tokens -> Create Token -> "Workers AI" template.
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
CEREBRAS="${4:-}"
CF_ACCOUNT="${5:-}"
CF_TOKEN="${6:-}"

cd "$DESK_DIR" || { echo "No desk dir at $DESK_DIR"; exit 1; }

# ---------- validate before touching anything ----------
if [ -z "$GEMINI" ] && [ -z "$OPENROUTER" ] && [ -z "$CEREBRAS" ] && [ -z "$CF_TOKEN" ]; then
  echo "Usage: bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [MODEL] [CEREBRAS_KEY] [CF_ACCOUNT_ID] [CF_TOKEN]"
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
if [ "$CEREBRAS" != "-" ] && [ "$CEREBRAS" != "" ] && [ "${#CEREBRAS}" -lt 20 ]; then
  echo "That Cerebras key looks too short (${#CEREBRAS} chars). Nothing changed."
  exit 2
fi
if [ "$CF_TOKEN" != "-" ] && [ "$CF_TOKEN" != "" ] && [ "${#CF_TOKEN}" -lt 20 ]; then
  echo "That Cloudflare token looks too short (${#CF_TOKEN} chars). Nothing changed."
  exit 2
fi
# A token with no account id cannot build a URL, so refuse the half-configuration
# instead of writing a provider that silently never works.
if [ -n "$CF_TOKEN" ] && [ "$CF_TOKEN" != "-" ] && { [ -z "$CF_ACCOUNT" ] || [ "$CF_ACCOUNT" = "-" ]; }; then
  if ! grep -q '^CLOUDFLARE_ACCOUNT_ID=.' "$ENV_FILE" 2>/dev/null; then
    echo "Cloudflare needs the account id too (arg 5). Nothing changed."
    exit 2
  fi
fi

# ---------- remember what is already there ----------
touch "$ENV_FILE"
OLD_GEMINI="$(grep '^GEMINI_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_OPENROUTER="$(grep '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_CEREBRAS="$(grep '^CEREBRAS_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_CF_ACCOUNT="$(grep '^CLOUDFLARE_ACCOUNT_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_CF_TOKEN="$(grep '^CLOUDFLARE_API_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_MODEL="$(grep '^BRAIN_GEMINI_MODEL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"

# ---------- back up, then rewrite only our three lines ----------
cp "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null
chmod 600 "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null

# grep -v keeps every other line, including DESK_TOKEN. Overwriting the file
# would wipe the login key and lock the owner out of their own desk.
grep -v -e '^GEMINI_API_KEY=' -e '^OPENROUTER_API_KEY=' -e '^CEREBRAS_API_KEY=' -e '^BRAIN_GEMINI_MODEL=' \
       -e '^CLOUDFLARE_ACCOUNT_ID=' -e '^CLOUDFLARE_API_TOKEN=' \
  "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null
mv "$ENV_FILE.new" "$ENV_FILE"

# Keep a key that was already there when the caller passes "-".
if [ "$GEMINI" = "-" ] && [ -n "${OLD_GEMINI:-}" ]; then GEMINI="$OLD_GEMINI"; fi
if [ "$OPENROUTER" = "-" ] && [ -n "${OLD_OPENROUTER:-}" ]; then OPENROUTER="$OLD_OPENROUTER"; fi
if [ "$CEREBRAS" = "-" ] && [ -n "${OLD_CEREBRAS:-}" ]; then CEREBRAS="$OLD_CEREBRAS"; fi
if [ "$CF_ACCOUNT" = "-" ] && [ -n "${OLD_CF_ACCOUNT:-}" ]; then CF_ACCOUNT="$OLD_CF_ACCOUNT"; fi
if [ "$CF_TOKEN" = "-" ] && [ -n "${OLD_CF_TOKEN:-}" ]; then CF_TOKEN="$OLD_CF_TOKEN"; fi

if [ -n "$GEMINI" ] && [ "$GEMINI" != "-" ]; then
  echo "GEMINI_API_KEY=$GEMINI" >> "$ENV_FILE"
fi
if [ -n "$OPENROUTER" ] && [ "$OPENROUTER" != "-" ]; then
  echo "OPENROUTER_API_KEY=$OPENROUTER" >> "$ENV_FILE"
fi
if [ -n "$CEREBRAS" ] && [ "$CEREBRAS" != "-" ]; then
  echo "CEREBRAS_API_KEY=$CEREBRAS" >> "$ENV_FILE"
fi
if [ -n "$CF_ACCOUNT" ] && [ "$CF_ACCOUNT" != "-" ]; then
  echo "CLOUDFLARE_ACCOUNT_ID=$CF_ACCOUNT" >> "$ENV_FILE"
fi
if [ -n "$CF_TOKEN" ] && [ "$CF_TOKEN" != "-" ]; then
  echo "CLOUDFLARE_API_TOKEN=$CF_TOKEN" >> "$ENV_FILE"
fi
# "-" (or nothing) means keep whatever model was already configured.
if [ "$MODEL" = "-" ] || [ -z "$MODEL" ]; then MODEL="${OLD_MODEL:-}"; fi
if [ -n "$MODEL" ] && [ "$MODEL" != "-" ]; then
  echo "BRAIN_GEMINI_MODEL=$MODEL" >> "$ENV_FILE"
fi

# ---------- quota saver ----------
# The trading loop ticks every 30s. Unpaced, the advisor asks for an opinion
# 120 times an hour: 2 coins x 2 providers + a 3-turn debate = 7 calls a cycle,
# which empties a 200/day Gemini budget in about 25 minutes and then sits on
# "AI busy / limited" for the rest of the day. These settings trade a little
# freshness for a budget that lasts the whole day:
#   1 coin x 1 provider, no debate, at most one review every 4 minutes
#   = 15 calls/hour, ~180/day, inside the 200 cap.
# Raise BRAIN_OPINIONS to 2 or BRAIN_DEBATES_PER_CYCLE to 1 if you want the
# two-bot back-and-forth and do not mind it running out sooner.
if [ "${KEEP_QUOTA:-1}" = "1" ]; then
  grep -v -e '^BRAIN_PER_CYCLE=' -e '^BRAIN_OPINIONS=' -e '^BRAIN_DEBATES_PER_CYCLE=' \
       -e '^BRAIN_MIN_INTERVAL_S=' -e '^BRAIN_CACHE_TTL_S=' -e '^BRAIN_ADVISE_ON=' "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null
  mv "$ENV_FILE.new" "$ENV_FILE"
  {
    echo "BRAIN_PER_CYCLE=1"
    echo "BRAIN_OPINIONS=1"
    echo "BRAIN_DEBATES_PER_CYCLE=0"
    echo "BRAIN_MIN_INTERVAL_S=240"
    echo "BRAIN_CACHE_TTL_S=1800"
    echo "BRAIN_ADVISE_ON=exit"
  } >> "$ENV_FILE"
fi
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
echo "The AI quota panel on the dashboard shows calls left per provider."
echo
echo "Quota profile: AI on EXITS only, no debate, 1 review / 4 min."
echo "Want the two-bot debate back?  KEEP_QUOTA=0 bash termux/set-ai-keys.sh - -"
