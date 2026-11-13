#!/data/data/com.termux/files/usr/bin/bash
# Check whether the desk on THIS phone has the latest code from GitHub.
#
# Usage:
#   bash ~/solana-desk/termux/verify-update.sh
#
# Exit code 0 = up to date and the signal feature is present.
# Exit code 1 = something is missing (it tells you what).
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

fail=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  MISS  %s\n' "$*"; fail=1; }

say "=== 1. what commit is on disk ==="
say "  HEAD:   $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git repo')"
say "  branch: $(git branch --show-current 2>/dev/null || echo '?')"

say ""
say "=== 2. is GitHub ahead of this phone? ==="
if git fetch --quiet origin 2>/dev/null; then
  behind=$(git rev-list --count HEAD..origin/$(git branch --show-current 2>/dev/null || echo main) 2>/dev/null || echo "?")
  if [ "$behind" = "0" ]; then
    ok "no new commits on GitHub (phone is current)"
  elif [ "$behind" = "?" ]; then
    bad "could not compare with origin (offline?)"
  else
    bad "$behind commit(s) behind GitHub -> run: git pull"
  fi
else
  bad "git fetch failed (no network, or auth problem)"
fi

say ""
say "=== 3. are the new files actually here? ==="
for f in desk/signal_engine.py desk/signal_backtest.py \
         scripts/backtest_signals.py tests/test_signal_engine.py \
         BACKTEST_SIGNALS_REPORT.md; do
  [ -f "$f" ] && ok "$f" || bad "$f"
done

say ""
say "=== 4. are the edits inside the old files? ==="
grep -q "Signal</th>" web/index.html      && ok "dashboard has the Signal column"      || bad "web/index.html missing Signal column"
grep -q "sgAct"        web/app.js         && ok "dashboard renders the signal chip"    || bad "web/app.js missing signal rendering"
grep -q "tag.sell"     web/styles.css     && ok "sell chip style present"              || bad "web/styles.css missing .tag.sell"
grep -q "signal_engine" desk/solana_agent.py && ok "agent imports the signal engine"   || bad "desk/solana_agent.py not wired"
grep -q "take_profit_mode" config.py      && ok "config has take_profit_mode"          || bad "config.py missing take_profit_mode"
grep -q "exits.tp_plan" desk/settings.py  && ok "settings expose exits.tp_plan"        || bad "desk/settings.py missing exits.tp_plan"
grep -q "CEREBRAS_URL"   desk/brain_advisor.py && ok "Cerebras is a free provider"        || bad "desk/brain_advisor.py missing Cerebras"
grep -q "def quota"      desk/brain_advisor.py && ok "advisor reports per-provider quota"  || bad "desk/brain_advisor.py missing quota()"
grep -q "keys_cooling"   desk/brain_advisor.py && ok "per-key cooldown is reported"       || bad "desk/brain_advisor.py missing keys_cooling"
grep -q "quotaBars"      web/index.html        && ok "dashboard has the AI quota panel"   || bad "web/index.html missing quota panel"
grep -q "renderQuota"    web/app.js            && ok "dashboard renders quota bars"        || bad "web/app.js missing renderQuota"
grep -q "BRAIN_ADVISE_ON" desk/brain_advisor.py && ok "AI advice is exit-only by default"   || bad "desk/brain_advisor.py missing advise mode"
grep -q "_advise_exit"    desk/solana_agent.py  && ok "agent reviews exits with the AI"      || bad "desk/solana_agent.py missing _advise_exit"

say ""
say "=== 5. does the code import cleanly? ==="
if python -c "from desk import signal_engine, signal_backtest; from desk import settings; print('   ', len(settings.REGISTRY), 'settings')" 2>/dev/null; then
  ok "signal_engine + signal_backtest import, settings registry loads"
else
  bad "import failed -> python -c 'from desk import signal_engine' to see the error"
fi

say ""
say "=== 6. is the server running the new code? ==="
if pgrep -f "[u]vicorn app:app" >/dev/null 2>&1; then
  ok "uvicorn is running (pid $(pgrep -f '[u]vicorn app:app' | head -1))"
  say "        restarted at: $(ps -o lstart= -p "$(pgrep -f '[u]vicorn app:app' | head -1)" 2>/dev/null)"
  say "        if that is older than your git pull, run: pkill -f '[u]vicorn app:app'"
else
  bad "uvicorn is NOT running -> bash termux/start-desk.sh"
fi

say ""
if [ "$fail" -eq 0 ]; then
  say "RESULT: up to date, signal feature present, desk running."
  exit 0
else
  say "RESULT: something above is missing. Fix the MISS lines, then re-run this."
  exit 1
fi
