#!/data/data/com.termux/files/usr/bin/bash
# Write the desk's AI keys into ~/solana-desk/.env, then restart the server.
#
# Why a script: pasting a multi-line block into Termux shreds it. The clipboard
# splits on newlines, each fragment gets pasted separately, and the shell ends up
# running garbage. One short command line is atomic and always survives.
#
# Usage:
#   bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [GEMINI_MODEL] [CEREBRAS_KEY] [CF_ACCOUNT_ID] [CF_TOKEN] [OLLAMA_KEY] [OLLAMA_MODEL] [GROQ_KEY]
#
# Keys are also routed by their prefix, so you cannot put one in the wrong slot:
#   gsk_...      -> GROQ_API_KEY      (Groq keys start gsk_)
#   sk-or-v1-... -> OPENROUTER_API_KEY
#   AIza...      -> GEMINI_API_KEY
#   csk-...      -> CEREBRAS_API_KEY
# So this does the right thing even though Groq is arg 9:
#   bash termux/set-ai-keys.sh - gsk_xxxxxxxx
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
# Ollama Cloud is the sixth bucket and the only PAID one (credits or a plan).
# It answers 402 until you top up at https://ollama.com/plans, so it sits last
# in the fallback order and can never displace a free provider:
#   bash termux/set-ai-keys.sh - - - - - - 49368adc01be...Pf8kxyGoXPODNW7sbyULVimz
# Optional 8th arg overrides the model (default deepseek-v4.1-flash):
#   bash termux/set-ai-keys.sh - - - - - - <key> glm-5.3-flash
#
# LOCAL Ollama is free: no key, no credits, no bill. Pass the word "local" as
# the 7th arg and the script writes OLLAMA_BASE_URL instead of a key, so the
# second opinion runs on your own machine:
#   bash termux/set-ai-keys.sh - - - - - - local
#   bash termux/set-ai-keys.sh - - - - - - local qwen2.5     # pick the model
# Start it first with `ollama serve` and `ollama pull llama3.2`. A running
# localhost:11434 is detected even with no .env line at all.
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
OLLAMA="${7:-}"
OLLAMA_MODEL="${8:-}"
GROQ="${9:-}"

# Route by prefix. A Groq key pasted into the OpenRouter slot used to be written
# as OPENROUTER_API_KEY, which then failed with 401 at the wrong endpoint and
# looked like a broken key. The prefix is unambiguous, so trust it over position.
case "$OPENROUTER" in
  gsk_*)      if [ -z "$GROQ" ]; then GROQ="$OPENROUTER"; OPENROUTER="-"; fi ;;
esac
case "$GROQ" in
  sk-or-*)    if [ "$OPENROUTER" = "-" ] || [ -z "$OPENROUTER" ]; then OPENROUTER="$GROQ"; GROQ="-"; fi ;;
  AIza*)      GEMINI="$GROQ"; GROQ="-" ;;
esac
case "$OPENROUTER" in
  AIza*)      GEMINI="$OPENROUTER"; OPENROUTER="-" ;;
  csk-*)      CEREBRAS="$OPENROUTER"; OPENROUTER="-" ;;
esac

cd "$DESK_DIR" || { echo "No desk dir at $DESK_DIR"; exit 1; }

# ---------- validate before touching anything ----------
if [ -z "$GEMINI" ] && [ -z "$OPENROUTER" ] && [ -z "$CEREBRAS" ] && [ -z "$CF_TOKEN" ] && [ -z "$OLLAMA" ] && [ -z "$GROQ" ]; then
  echo "Usage: bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY> [MODEL] [CEREBRAS_KEY] [CF_ACCOUNT_ID] [CF_TOKEN] [OLLAMA_KEY] [OLLAMA_MODEL] [GROQ_KEY]"
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
if [ "$GROQ" != "-" ] && [ "$GROQ" != "" ] && [ "${#GROQ}" -lt 20 ]; then
  echo "That Groq key looks too short (${#GROQ} chars). Nothing changed."
  exit 2
fi
if [ "$OLLAMA" != "-" ] && [ "$OLLAMA" != "" ] && [ "$OLLAMA" != "local" ] && [ "${#OLLAMA}" -lt 20 ]; then
  echo "That Ollama key looks too short (${#OLLAMA} chars). Nothing changed."
  echo "For a free local Ollama pass the word: local"
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
OLD_GROQ="$(grep '^GROQ_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_OLLAMA="$(grep '^OLLAMA_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_OLLAMA_MODEL="$(grep '^BRAIN_OLLAMA_MODEL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
OLD_OLLAMA_BASE="$(grep '^OLLAMA_BASE_URL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"

# ---------- back up, then rewrite only our three lines ----------
cp "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null
chmod 600 "$ENV_FILE" "$ENV_FILE.bak" 2>/dev/null

# grep -v keeps every other line, including DESK_TOKEN. Overwriting the file
# would wipe the login key and lock the owner out of their own desk.
grep -v -e '^GEMINI_API_KEY=' -e '^OPENROUTER_API_KEY=' -e '^CEREBRAS_API_KEY=' -e '^BRAIN_GEMINI_MODEL=' \
       -e '^CLOUDFLARE_ACCOUNT_ID=' -e '^CLOUDFLARE_API_TOKEN=' \
       -e '^OLLAMA_API_KEY=' -e '^BRAIN_OLLAMA_MODEL=' -e '^OLLAMA_BASE_URL=' \
       -e '^BRAIN_OLLAMA_LOCAL_MODEL=' -e '^GROQ_API_KEY=' \
  "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null
mv "$ENV_FILE.new" "$ENV_FILE"

# Keep a key that was already there when the caller passes "-".
if [ "$GEMINI" = "-" ] && [ -n "${OLD_GEMINI:-}" ]; then GEMINI="$OLD_GEMINI"; fi
if [ "$OPENROUTER" = "-" ] && [ -n "${OLD_OPENROUTER:-}" ]; then OPENROUTER="$OLD_OPENROUTER"; fi
if [ "$CEREBRAS" = "-" ] && [ -n "${OLD_CEREBRAS:-}" ]; then CEREBRAS="$OLD_CEREBRAS"; fi
if [ "$CF_ACCOUNT" = "-" ] && [ -n "${OLD_CF_ACCOUNT:-}" ]; then CF_ACCOUNT="$OLD_CF_ACCOUNT"; fi
if [ "$CF_TOKEN" = "-" ] && [ -n "${OLD_CF_TOKEN:-}" ]; then CF_TOKEN="$OLD_CF_TOKEN"; fi
if [ "$GROQ" = "-" ] && [ -n "${OLD_GROQ:-}" ]; then GROQ="$OLD_GROQ"; fi
if [ "$OLLAMA" = "-" ] && [ -n "${OLD_OLLAMA:-}" ]; then OLLAMA="$OLD_OLLAMA"; fi
if [ "$OLLAMA_MODEL" = "-" ] && [ -n "${OLD_OLLAMA_MODEL:-}" ]; then OLLAMA_MODEL="$OLD_OLLAMA_MODEL"; fi
if [ "$OLLAMA" = "-" ] && [ -n "${OLD_OLLAMA_BASE:-}" ]; then OLLAMA="local"; fi

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
if [ -n "$GROQ" ] && [ "$GROQ" != "-" ]; then
  echo "GROQ_API_KEY=$GROQ" >> "$ENV_FILE"
fi
if [ "$OLLAMA" = "local" ]; then
  # Free local model: no key, just where to reach it.
  echo "OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://localhost:11434}" >> "$ENV_FILE"
  if [ -n "$OLLAMA_MODEL" ] && [ "$OLLAMA_MODEL" != "-" ]; then
    echo "BRAIN_OLLAMA_LOCAL_MODEL=$OLLAMA_MODEL" >> "$ENV_FILE"
  fi
elif [ -n "$OLLAMA" ] && [ "$OLLAMA" != "-" ]; then
  echo "OLLAMA_API_KEY=$OLLAMA" >> "$ENV_FILE"
fi
if [ -n "$OLLAMA_MODEL" ] && [ "$OLLAMA_MODEL" != "-" ] && [ "$OLLAMA" != "local" ]; then
  echo "BRAIN_OLLAMA_MODEL=$OLLAMA_MODEL" >> "$ENV_FILE"
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
       -e '^BRAIN_MIN_INTERVAL_S=' -e '^BRAIN_CACHE_TTL_S=' -e '^BRAIN_ADVISE_ON=' \
       -e '^BRAIN_SPREAD_DAY=' -e '^DESK_BRAIN=' "$ENV_FILE" > "$ENV_FILE.new" 2>/dev/null
  mv "$ENV_FILE.new" "$ENV_FILE"
  {
    echo "BRAIN_PER_CYCLE=1"
    echo "BRAIN_OPINIONS=1"
    echo "BRAIN_DEBATES_PER_CYCLE=0"
    echo "BRAIN_MIN_INTERVAL_S=240"
    echo "BRAIN_CACHE_TTL_S=1800"
    echo "BRAIN_ADVISE_ON=exit"
    # Spread each provider's daily budget across the whole day. Without this a
    # burst can spend all 400 Groq calls by mid-morning and leave the desk on the
    # rule-based heuristic until midnight. Measured: a review every 60s with no
    # spreading runs dry at hour 6.7 and refuses 1040 calls; with spreading the
    # first refusal is at hour 23.98. Costs nothing when the desk is idle.
    echo "BRAIN_SPREAD_DAY=${BRAIN_SPREAD_DAY:-groq}"
    # The Analyst seat (desk/brain.py) is a SEPARATE module from the advisory
    # second opinion (desk/brain_advisor.py) and does not read a cloud key on its
    # own: it stays on the deterministic heuristic unless DESK_BRAIN names a
    # provider. That is deliberate, because the analyst's score gates trades and
    # a model swap changes it. Set it only when a Groq key is actually present,
    # otherwise the seat would point at a provider with no key and fall back.
    if grep -q '^GROQ_API_KEY=gsk_' "$ENV_FILE" 2>/dev/null; then
      echo "DESK_BRAIN=groq"
    fi
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
