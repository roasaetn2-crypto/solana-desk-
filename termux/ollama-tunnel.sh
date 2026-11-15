#!/usr/bin/env bash
# Run a LOCAL Ollama and give the Render desk a reachable HTTPS address for it.
#
# WHY THIS EXISTS
#   The Render free instance cannot host Ollama: ~512MB RAM, no GPU, and
#   llama3.2 alone needs more than that. So the model runs on the phone (Termux)
#   or a home box, and Render talks to it through a Cloudflare quick tunnel.
#
#   brain_advisor.py and brain.py both read OLLAMA_BASE_URL, so ONE value
#   configures the advisory layer and the analyst seat together.
#
# WHAT IT DOES
#   1. starts `ollama serve` if it is not already up
#   2. pulls the model (default llama3.2; override with OLLAMA_MODEL=...)
#   3. opens a Cloudflare quick tunnel to port 11434
#   4. prints the OLLAMA_BASE_URL to paste into Render -> Environment
#
# USAGE
#   bash termux/ollama-tunnel.sh              # llama3.2
#   OLLAMA_MODEL=qwen2.5:7b bash termux/ollama-tunnel.sh
#
# THEN, in Render -> solana-desk-1 -> Environment, add:
#   OLLAMA_BASE_URL            = https://<the printed host>
#   BRAIN_OLLAMA_LOCAL_MODEL   = <the model you pulled>
# and redeploy. Check it with:  python3 scripts/ai_status.py
#
# NOTES
#   * A quick tunnel URL is RANDOM and CHANGES when the tunnel restarts. For a
#     stable hostname use a named tunnel (see deploy/oracle/cloudflared-setup.sh).
#   * The desk treats a local Ollama as ADVISORY ONLY (brain_advisor.py). It
#     cannot size a trade or loosen a gate. Keep the analyst score on
#     Gemini/Groq: a 3B model scoring structured JSON gates entries badly.
#   * Nothing leaves your machine except the prompt you send to your own tunnel.
set -euo pipefail

PORT="${OLLAMA_PORT:-11434}"
MODEL="${OLLAMA_MODEL:-llama3.2}"
LOG_DIR="${HOME}/desk-data"
OLLAMA_LOG="${LOG_DIR}/ollama.log"
TUNNEL_LOG="${LOG_DIR}/ollama-tunnel.log"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

mkdir -p "$LOG_DIR"

# ---------- 1. ollama installed? ----------
if ! command -v ollama >/dev/null 2>&1; then
  say "Installing Ollama"
  if command -v pkg >/dev/null 2>&1; then
    pkg install -y ollama || die "pkg install ollama failed; see https://ollama.com/download"
  else
    curl -fsSL https://ollama.com/install.sh | sh \
      || die "Ollama install failed; see https://ollama.com/download"
  fi
fi

# ---------- 2. is the server up? ----------
up() { curl -fsS -m 3 "http://127.0.0.1:${PORT}/api/tags" >/dev/null 2>&1; }

if up; then
  say "Ollama already serving on :${PORT}"
else
  say "Starting ollama serve on :${PORT}"
  OLLAMA_HOST="127.0.0.1:${PORT}" nohup ollama serve >"$OLLAMA_LOG" 2>&1 &
  for _ in $(seq 1 30); do
    up && break
    sleep 1
  done
  up || die "ollama did not come up; see $OLLAMA_LOG"
fi

# ---------- 3. pull the model ----------
say "Pulling model: ${MODEL} (first run downloads a few hundred MB)"
ollama pull "$MODEL" || die "could not pull ${MODEL}"

# ---------- 4. cloudflared ----------
if ! command -v cloudflared >/dev/null 2>&1; then
  say "Installing cloudflared"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64) CF_ARCH="arm64" ;;
    armv7l|armv8l) CF_ARCH="arm" ;;
    x86_64|amd64)  CF_ARCH="amd64" ;;
    *) die "unsupported arch: $ARCH" ;;
  esac
  if command -v pkg >/dev/null 2>&1; then
    pkg install -y cloudflared || die "pkg install cloudflared failed"
  else
    curl -fsSL -o /tmp/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
      || die "cloudflared download failed"
    chmod +x /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/local/bin/cloudflared 2>/dev/null \
      || mv /tmp/cloudflared "${PREFIX:-/usr/local}/bin/cloudflared" 2>/dev/null \
      || die "could not install cloudflared to PATH"
  fi
fi

# ---------- 5. open the tunnel ----------
say "Opening a Cloudflare quick tunnel to 127.0.0.1:${PORT}"
: >"$TUNNEL_LOG"
nohup cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate \
  >"$TUNNEL_LOG" 2>&1 &

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || die "no tunnel URL yet; check $TUNNEL_LOG"

# ---------- 6. prove the tunnel really reaches ollama ----------
say "Checking the tunnel from the outside"
if curl -fsS -m 10 "${URL}/api/tags" >/dev/null 2>&1; then
  printf '\033[1;32m[ok] tunnel reaches Ollama\033[0m\n'
else
  warn "tunnel URL is up but /api/tags did not answer yet; give it ~10s and retry"
fi

cat <<EOF

$(printf '\033[1;32m')================ OLLAMA IS READY ================$(printf '\033[0m')

  OLLAMA_BASE_URL          = ${URL}
  BRAIN_OLLAMA_LOCAL_MODEL = ${MODEL}

Paste those two into:
  Render -> solana-desk-1 -> Environment  (then redeploy)

Verify from the repo:
  python3 scripts/ai_status.py

This tunnel URL is RANDOM and dies when the tunnel stops. Keep this process
running (or install a named tunnel) or Render will fall back to Gemini/Groq.

Logs: ${OLLAMA_LOG}
      ${TUNNEL_LOG}
EOF
