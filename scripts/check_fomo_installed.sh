#!/data/data/com.termux/files/usr/bin/bash
# Prove that the FOMO seat is actually present in THIS checkout.
# Run from the repo root:  bash scripts/check_fomo_installed.sh
set -u

ok=0; bad=0
pass() { printf '  \033[32mOK\033[0m   %s\n' "$1"; ok=$((ok+1)); }
fail() { printf '  \033[31mMISSING\033[0m %s\n' "$1"; bad=$((bad+1)); }

echo "== 1. which checkout am I in =="
echo "  path:   $(pwd)"
echo "  commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git repo')"
echo "  branch: $(git branch --show-current 2>/dev/null)"
echo "  remote: $(git remote get-url origin 2>/dev/null | sed 's#//.*@#//***@#')"
echo

echo "== 2. is the commit on this checkout =="
if git merge-base --is-ancestor f5c0e39 HEAD 2>/dev/null; then
  pass "commit f5c0e39 (cache fix + FOMO seat) is in your history"
else
  fail "commit f5c0e39 not found -- run: git pull"
fi
if git merge-base --is-ancestor 3f053d7 HEAD 2>/dev/null; then
  pass "commit 3f053d7 (FOMO docs) is in your history"
else
  fail "commit 3f053d7 not found -- run: git pull"
fi
echo

echo "== 3. are the files here =="
for f in desk/fomo.py FOMO-SEAT.md tests/test_fomo.py tests/test_quote_cache.py \
         scripts/verify_entry_exit.py scripts/repro_confirm_quote.py; do
  if [ -f "$f" ]; then pass "$f ($(wc -l < "$f") lines)"; else fail "$f"; fi
done
echo

echo "== 4. is it actually wired in (not just sitting in the folder) =="
check() {  # file, pattern, label
  if grep -q "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3"; fi
}
check config.py            'FOMO = {'                       "config.py has the FOMO block"
check desk/devcheck.py     'def apply_fomo'                 "devcheck.py has apply_fomo()"
check desk/meme.py         'fomo_ev'                        "meme.py scorecard takes fomo_ev"
check desk/solana_agent.py 'fomo.token_evidence'            "agent calls fomo.token_evidence()"
check desk/solana_agent.py '"fomo": fomo.status()'          "agent reports fomo in /api/state"
check desk/scanner.py      '_fetch_live(s, ep, now)'        "scanner uses _fetch_live (cache fix)"
echo

echo "== 5. does it import and stay inert without a key =="
if DESK_OFFLINE=1 python3 -c "
import config
from desk import fomo, devcheck, meme
import desk.solana_agent
assert config.FOMO['level'] in ('signals','thesis')
assert fomo.enabled() is False, 'should be off with no key'
print('     level=%s max_nudge=%s enabled=%s' % (
    config.FOMO['level'], config.FOMO['max_nudge'], fomo.enabled()))
" 2>/dev/null; then
  pass "imports clean, FOMO inert with no key"
else
  fail "import failed -- run: pip install -r requirements.txt"
fi
echo

echo "== 6. do the tests pass here =="
if DESK_OFFLINE=1 python3 -m pytest tests/test_fomo.py tests/test_quote_cache.py -q 2>/dev/null | tail -1; then
  pass "FOMO + quote-cache tests ran"
else
  fail "pytest not available -- run: pip install pytest"
fi
echo

echo "-------------------------------------------"
echo "  $ok passed, $bad missing"
if [ "$bad" -eq 0 ]; then
  echo "  This checkout HAS the FOMO seat and the cache fix."
else
  echo "  This checkout is STALE. Run:  cd ~/solana-desk && git pull"
fi
echo "-------------------------------------------"
