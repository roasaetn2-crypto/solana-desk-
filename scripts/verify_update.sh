#!/data/data/com.termux/files/usr/bin/bash
# verify_update.sh - prove this clone actually has the promoted pullback package.
#
# Usage (from the repo root):
#   bash scripts/verify_update.sh          # fast: git + config + code + import
#   bash scripts/verify_update.sh --tests  # also run the full pytest suite
#
# Exit code 0 = everything checks out. Non-zero = something is stale or edited.

set -u

# The commit that promoted 20% dip + fast ladder into the live desk.
WANT_COMMIT="abacb74"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=1; }

say "== 1. where am I =="
if [ -f config.py ] && [ -d desk ]; then
  ok "repo root: $(pwd)"
else
  bad "not in the repo root (config.py / desk/ not found). cd into your solana-desk- clone first."
  say ""
  say "RESULT: FAIL - wrong directory."
  exit 2
fi

say ""
say "== 2. git state =="
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
say "  branch: $BRANCH   head: $HEAD"
[ "$BRANCH" = "main" ] && ok "on main" || bad "not on main (on '$BRANCH')"

if git cat-file -e "${WANT_COMMIT}^{commit}" 2>/dev/null; then
  ok "commit $WANT_COMMIT exists locally"
else
  bad "commit $WANT_COMMIT missing - run: git fetch origin"
fi

if git merge-base --is-ancestor "$WANT_COMMIT" HEAD 2>/dev/null; then
  ok "HEAD contains $WANT_COMMIT (promotion is in your history)"
else
  bad "HEAD is BEHIND $WANT_COMMIT - run: git pull origin main"
fi

DIRTY=$(git status --porcelain --untracked-files=no 2>/dev/null)
if [ -n "$DIRTY" ]; then
  bad "tracked files have uncommitted edits (this can mask a stale pull):"
  printf '%s\n' "$DIRTY" | sed 's/^/         /'
else
  ok "no edits to tracked files"
fi

UNTRACKED=$(git status --porcelain 2>/dev/null | grep '^??' | wc -l | tr -d ' ')
if [ "$UNTRACKED" -gt 0 ]; then
  printf '  [note] %s untracked file(s) - runtime state, harmless:\n' "$UNTRACKED"
  git status --porcelain 2>/dev/null | grep '^??' | sed 's/^?? /         /' | head -12
  [ "$UNTRACKED" -gt 12 ] && printf '         ... and %s more\n' "$((UNTRACKED-12))"
fi

say ""
say "== 3. config.py =="
if grep -q '"pullback_entry": True' config.py; then
  ok 'pullback_entry is True'
else
  bad 'pullback_entry is NOT True (still False, or file is stale)'
fi
grep -n '"pullback_entry"' config.py | sed 's/^/         /'

say ""
say "== 4. desk/meme_tactics.py ladder =="
check_line() { # pattern, human label
  if grep -q "$1" desk/meme_tactics.py; then ok "$2"; else bad "$2"; fi
}
check_line '"stop_pct": 0.30'            'stop_pct 0.30'
check_line '"tp1_at": 1.25'              'tp1_at 1.25'
check_line '"tp1_frac": 0.5'             'tp1_frac 0.5'
check_line '"tp2_at": 1.6'               'tp2_at 1.6'
check_line '"tp2_frac": 0.5'             'tp2_frac 0.5'
check_line '"trail_pct": 0.15'           'trail_pct 0.15'
grep -n '"stop_pct"\|"tp1_at"\|"tp2_at"\|"trail_pct"' desk/meme_tactics.py | sed 's/^/         /'

say ""
say "== 4b. established lane (over-age pools, half size) =="
if grep -q '"enabled": True' config.py && grep -q 'ESTABLISHED' config.py; then
  ok "ESTABLISHED lane present and enabled"
else
  bad "ESTABLISHED lane missing or disabled in config.py"
fi
grep -n '"max_age_hours": 168\|"min_liquidity_usd": 50_000\|"size_scale": 0.5' config.py | sed 's/^/         /'
if grep -q 'def established_ok' desk/scanner.py; then
  ok "scanner.established_ok present"
else
  bad "scanner.established_ok missing"
fi
if grep -q 'def established_size_scale' desk/risk.py; then
  ok "risk.established_size_scale present"
else
  bad "risk.established_size_scale missing"
fi

say ""
say "== 5. live import (what the bot will actually use) =="
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY'
import sys
try:
    import config
    from desk import meme_tactics
except Exception as e:
    print("  [FAIL] import error: %r" % (e,))
    sys.exit(1)
want = {"stop_pct": 0.30, "tp1_at": 1.25, "tp1_frac": 0.5,
        "tp2_at": 1.6, "tp2_frac": 0.5, "trail_pct": 0.15}
flag = config.MEME.get("pullback_entry")
ov = meme_tactics.exit_overrides()
print("  pullback_entry =", flag)
print("  overrides      =", ov)
ok = True
if flag is not True:
    print("  [FAIL] pullback_entry is not True at runtime")
    ok = False
if ov != want:
    print("  [FAIL] exit_overrides() does not match the promoted ladder")
    ok = False
try:
    import config as _c
    from desk import scanner as _sc, risk as _rk
    lane = _c.ESTABLISHED.get("enabled")
    print("  established lane =", lane, "scale", _c.ESTABLISHED.get("size_scale"))
    over_age = _c.SCAN["max_age_hours"] * 60 + 60
    if lane and _rk.established_size_scale(over_age) != _c.ESTABLISHED["size_scale"]:
        print("  [FAIL] established lane does not halve an over-age ticket")
        ok = False
    if lane and not _sc.established_ok({"age_minutes": over_age,
                                        "liquidity_usd": 100_000, "volume_h1": 50_000,
                                        "buys_h1": 80, "sells_h1": 70}):
        print("  [FAIL] established lane rejects a deep, live old pool")
        ok = False
except Exception as e:
    print("  [FAIL] established lane check error: %r" % (e,))
    ok = False
if ok:
    print("  [ OK ] runtime config matches the promoted package")
sys.exit(0 if ok else 1)
PY
  [ $? -eq 0 ] || FAIL=1
else
  bad "python3 not found - in Termux run: pkg install python"
fi

if [ "${1:-}" = "--tests" ]; then
  say ""
  say "== 6. test suite (slow, ~20s) =="
  if python3 -m pytest -q 2>&1 | tail -2 | sed 's/^/         /'; then
    ok "pytest finished"
  else
    bad "pytest reported failures"
  fi
fi

say ""
if [ "$FAIL" -eq 0 ]; then
  say "RESULT: PASS - this clone has the promoted pullback package."
  exit 0
else
  say "RESULT: FAIL - see the [FAIL] lines above."
  say "Fix: cd into the repo, then:  git pull origin main  &&  bash scripts/verify_update.sh"
  exit 1
fi
