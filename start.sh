#!/usr/bin/env bash
# Start command for hosted platforms (Render, Koyeb, Railway, Fly, VPS).
# The platform supplies $PORT. Never hardcode it.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# DESK_TOKEN locks the dashboard. Prefer setting it in the host's environment
# variables. If the host did not supply one, we DO NOT crash the deploy:
# a crash-loop / failed health check is worse than a key you can read from the
# logs. We generate one, persist it next to the app (best effort) and print it
# so you can log in. Set DESK_TOKEN in the dashboard to keep a stable key.
if [ -z "${DESK_TOKEN:-}" ]; then
  if [ -f .desk_token ]; then
    DESK_TOKEN="$(cat .desk_token 2>/dev/null || true)"
  fi
  if [ -z "${DESK_TOKEN:-}" ]; then
    DESK_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    printf '%s' "$DESK_TOKEN" > .desk_token 2>/dev/null || true
  fi
  export DESK_TOKEN
  echo "=============================================================="
  echo "  DESK_TOKEN was not set by the host."
  echo "  Using this key for this deployment:"
  echo ""
  echo "      $DESK_TOKEN"
  echo ""
  echo "  Open the URL and log in with the key above."
  echo "  To keep a stable key across redeploys, set DESK_TOKEN in the"
  echo "  host's environment variables (Render: Environment tab)."
  echo "=============================================================="
fi

echo "Solana Desk starting on port $PORT (locked)"
exec python -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips='*'
