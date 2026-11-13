#!/data/data/com.termux/files/usr/bin/bash
# Install / repair the Python dependencies the desk needs on Termux.
#
#   bash ~/solana-desk/termux/fix-deps.sh
#
# Fixes the most common Termux breakage:
#   "pumpportal: websocket-client missing (ModuleNotFoundError)"
#
# The trap: the import is `import websocket`, but the PyPI package is
# `websocket-client`. Running `pip install websocket` installs a DIFFERENT
# project (liris/websocket) that shadows the right one, and the desk still
# fails -- sometimes with a confusing AttributeError instead.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }

say "=== 1. python / pip ==="
python -V || { say "FAIL: python not found"; exit 1; }
python -m pip -V || { say "FAIL: pip not found (pkg install python)"; exit 1; }

say ""
say "=== 2. removing the wrong 'websocket' package if present ==="
if python -m pip show websocket >/dev/null 2>&1; then
  say "  found the wrong package 'websocket' -- uninstalling it"
  python -m pip uninstall -y websocket >/dev/null 2>&1 || true
else
  say "  not installed (good)"
fi

say ""
say "=== 3. installing requirements.txt ==="
python -m pip install --upgrade -r requirements.txt || {
  say "FAIL: pip install failed. If it is a build error, try:"
  say "  pkg install clang rust binutils libffi openssl"
  say "  pip install --upgrade pip setuptools wheel"
  exit 1
}

say ""
say "=== 4. verifying the websocket module is the RIGHT one ==="
python - <<'PY'
import sys
try:
    import websocket
except Exception as e:
    print(f"  FAIL: import websocket -> {type(e).__name__}: {e}")
    sys.exit(1)
need = ("create_connection", "WebSocketTimeoutException", "WebSocketApp")
missing = [n for n in need if not hasattr(websocket, n)]
print(f"  module : {websocket.__file__}")
print(f"  version: {getattr(websocket, '__version__', '?')}")
if missing:
    print(f"  FAIL: missing {missing} -- this is the wrong 'websocket' package")
    print("        run: pip uninstall -y websocket && pip install websocket-client")
    sys.exit(1)
print("  OK: websocket-client is installed and correct")
PY
[ $? -eq 0 ] || { say ""; say "FAIL: websocket check failed (see above)"; exit 1; }

say ""
say "=== 5. checking every desk import ==="
python - <<'PY'
import importlib, sys
mods = ["fastapi", "uvicorn", "pydantic", "requests", "multipart",
        "websocket", "app"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  ok   {m}")
    except Exception as e:
        print(f"  FAIL {m}: {type(e).__name__}: {e}")
        bad.append(m)
sys.exit(1 if bad else 0)
PY
[ $? -eq 0 ] || { say ""; say "FAIL: some imports are still broken (see above)"; exit 1; }

say ""
say "=== done. restart the desk so the new deps load: ==="
say "  pkill -f '[u]vicorn app:app'"
say "  bash ~/solana-desk/termux/start-desk.sh"
say ""
say "You should see 'pumpportal: connected, subscribed subscribeNewToken'"
say "and the dashboard feed chip should read 'on' instead of 'off'."
