#!/data/data/com.termux/files/usr/bin/bash
# Install / repair the Python dependencies the desk needs on Termux.
#
#   bash ~/solana-desk/termux/fix-deps.sh
#
# Options:
#   (none)   install paper deps only  (default, fast, no compiler)
#   --live   also install solders + base58 so live signing can arm
#   --help   show this text
#
# Fixes the most common Termux breakage:
#   "pumpportal: websocket-client missing (ModuleNotFoundError)"
#
# The trap: the import is `import websocket`, but the PyPI package is
# `websocket-client`. Running `pip install websocket` installs a DIFFERENT
# project (liris/websocket) that shadows the right one, and the desk still
# fails -- sometimes with a confusing AttributeError instead.
set -u

# Resolve the checkout by looking for config.py, not by guessing its name:
# the repo is `solana-desk-` but these scripts used to assume `solana-desk`.
source "$(dirname "$0")/lib-deskdir.sh"
DESK_DIR="$(desk_dir)" || { desk_dir_hint; exit 1; }
cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

say() { printf '%s\n' "$*"; }

# Live signing deps (solders, base58) are OPT-IN on Termux. solders is an ~12 MB
# compiled wheel with no Android build, so installing it pulls a Rust toolchain
# and compiles on the phone — minutes of CPU and a big download. Paper mode
# never imports it, so the default is to skip it.
LIVE=0
for a in "$@"; do
  case "$a" in
    --live) LIVE=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

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
say "=== 3. installing requirements.txt (paper deps) ==="
python -m pip install --upgrade -r requirements.txt || {
  say "FAIL: pip install failed. If it is a build error, try:"
  say "  pkg install clang rust binutils libffi openssl"
  say "  pip install --upgrade pip setuptools wheel"
  exit 1
}

say ""
if [ "$LIVE" = "1" ]; then
  say "=== 3b. installing live signing deps (solders, base58) ==="
  say "  base58 is pure Python: fast."
  python -m pip install --upgrade "base58>=2.1" || {
    say "FAIL: base58 failed to install (this one is pure Python, so it is"
    say "      usually a network problem, not a compiler problem)."
    exit 1
  }

  # solders is the hard part. Two facts, both verified:
  #
  #   1. There is no aarch64-Android wheel, so it MUST compile here.
  #   2. Every solders sdist on PyPI omits `crates/dict-derive`, which the
  #      root Cargo.toml references as a path dependency. So `pip install
  #      solders` ALWAYS dies with:
  #          failed to load source for dependency `dict-derive`
  #          No such file or directory ... crates/dict-derive/Cargo.toml
  #      This is an upstream packaging bug, not your phone. Pinning a version
  #      does not help: 0.21.0, 0.23.0, 0.26.0 and 0.29.0 all ship without it.
  #
  # The git repo DOES contain crates/dict-derive, so build from there instead.
  say ""
  say "  solders: PyPI sdist is broken (missing crates/dict-derive)."
  say "  building from the git tag instead, which has the full source tree."
  say "  this compiles the Solana Rust stack: expect 20-60 minutes."
  python -m pip install --upgrade maturin || {
    say "FAIL: could not install maturin (the Rust build backend)."
    exit 1
  }
  python -m pip install --no-build-isolation \
      "git+https://github.com/kevinheavey/solders.git@v0.27.1" || {
    say ""
    say "FAIL: solders still would not build."
    say "      Live signing needs it, so live mode will refuse to arm."
    say "      PAPER still works, and the Render lane already has solders"
    say "      installed -- so use Render as the live engine and this phone"
    say "      as the dashboard. That needs no build at all."
    exit 1
  }
else
  say "=== 3b. live signing deps SKIPPED (paper only) ==="
  say "  solders/base58 are only needed to sign real transactions."
  say "  To install them anyway:  bash termux/fix-deps.sh --live"
fi

# Prove the signing deps actually import. pip can report success on a package
# that then fails to load (wrong ABI, missing shared lib), and the desk would
# only discover that the moment you try to arm live.
if [ "$LIVE" = "1" ]; then
  say ""
  say "=== 3c. verifying the signing deps import ==="
  python - <<'PY'
import sys
ok = True
try:
    import base58
    print(f"  base58 : ok ({base58.__file__})")
except Exception as e:
    print(f"  base58 : FAIL {type(e).__name__}: {e}")
    ok = False
try:
    from solders.keypair import Keypair
    kp = Keypair()                      # generate a throwaway keypair
    print(f"  solders: ok ({Keypair.__module__})")
    print(f"           test pubkey {kp.pubkey()}")
except Exception as e:
    print(f"  solders: FAIL {type(e).__name__}: {e}")
    ok = False
sys.exit(0 if ok else 1)
PY
  [ $? -eq 0 ] || {
    say ""
    say "FAIL: signing deps installed but do not import."
    say "      Live mode will refuse to arm. Paper still works."
    exit 1
  }
fi

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
