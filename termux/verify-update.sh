#!/data/data/com.termux/files/usr/bin/bash
# Check whether the desk on THIS phone has the latest code from GitHub.
#
# Usage:
#   bash ~/solana-desk/termux/verify-update.sh            # check only
#   bash ~/solana-desk/termux/verify-update.sh --start    # check, then start it
#   bash ~/solana-desk/termux/verify-update.sh --log      # show the server log
#
# Exit code 0 = code is current AND the desk is running.
# Exit code 1 = the CODE is old or broken (it tells you what).
# Exit code 2 = the code is fine, the desk is just not running.
#
# A stopped server is NOT a failed update. Those are different problems and
# this script used to report them identically, which is confusing.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
LOG="${LOG:-$DESK_DIR/desk.log}"
PORT="${PORT:-8000}"
START=0
SHOWLOG=0
for a in "$@"; do
  case "$a" in
    --start) START=1 ;;
    --log)   SHOWLOG=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

cd "$DESK_DIR" || { echo "FAIL: no desk dir at $DESK_DIR"; exit 1; }

fail=0        # code problems only
warn=0        # not-code problems (server stopped)
say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  MISS  %s\n' "$*"; fail=1; }
note() { printf '  ..    %s\n' "$*"; warn=1; }   # a serving problem
tip()  { printf '  ..    %s\n' "$*"; }          # advice only, not a failure

running() { pgrep -f "[u]vicorn app:app" >/dev/null 2>&1; }

log_tail() {   # last N lines of the server log, errors first
  n="${1:-25}"
  [ -f "$LOG" ] || { say "  (no log at $LOG yet)"; return; }
  say "  ---- last $n lines of $LOG ----"
  tail -n "$n" "$LOG" 2>/dev/null | sed 's/^/  | /'
  say "  ---- end of log ----"
}

last_error() {  # pull the most useful crash line out of the log
  [ -f "$LOG" ] || return 0
  tail -n 120 "$LOG" 2>/dev/null | grep -E "^(Traceback|ModuleNotFoundError|[A-Za-z]*Error:|error:|ERROR)" \
    | tail -3 | sed 's/^/  ! /'
}

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
grep -q "cloudflare_url"  desk/brain_advisor.py && ok "Cloudflare is a free provider"      || bad "desk/brain_advisor.py missing Cloudflare"
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
if running; then
  pid="$(pgrep -f '[u]vicorn app:app' | head -1)"
  ok "uvicorn is running (pid $pid)"
  say "        started: $(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//')"
  say "        if that is older than your git pull, run: pkill -f '[u]vicorn app:app'"
  if curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    ok "it answers on port $PORT"
  else
    note "process exists but port $PORT does not answer yet (still booting?)"
  fi
else
  note "desk is stopped (this is not an update problem)"
fi

# ---- optional: start it and re-check ----
if [ "$START" -eq 1 ] && ! running; then
  say ""
  say "=== starting the desk ==="
  if pgrep -f "[s]tart-desk.sh" >/dev/null 2>&1; then
    say "  supervisor already running; waiting for it to respawn uvicorn"
  else
    nohup bash "$DESK_DIR/termux/start-desk.sh" >/dev/null 2>&1 &
    say "  launched termux/start-desk.sh"
  fi
  i=0
  while [ "$i" -lt 30 ]; do
    sleep 2
    i=$((i + 2))
    running && break
  done
  if running; then
    ok "uvicorn is up after ${i}s"
    if curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      ok "health endpoint answers on port $PORT"
      warn=0
    else
      note "process is up but port $PORT is not answering"
    fi
  else
    bad "uvicorn did not come up in 30s -- it is crashing on start"
    last_error
    log_tail 30
  fi
fi

# ---- if it is not running, show WHY (only when asked, or if it looks crashed) ----
if ! running; then
  if [ "$SHOWLOG" -eq 1 ] || [ -f "$LOG" ]; then
    err="$(last_error)"
    if [ -n "$err" ]; then
      say ""
      say "=== why it is not running ==="
      printf '%s\n' "$err"
      say "  full log: bash termux/verify-update.sh --log"
    fi
  fi
fi

# ---- will it come back on its own after a reboot? ----
say ""
say "=== 7. will it restart itself after a reboot? ==="
HOOK="$HOME/.termux/boot/10-desk.sh"
if [ -f "$HOOK" ]; then
  if grep -q "start-desk.sh" "$HOOK" 2>/dev/null; then
    ok "Termux:Boot hook installed ($HOOK)"
  else
    tip "a boot hook exists but does not call start-desk.sh"
  fi
else
  tip "no boot hook: after a reboot the desk stays down until you start it"
  say "        install it once:  bash termux/install-boot.sh"
fi
if command -v termux-wake-lock >/dev/null 2>&1; then
  ok "termux-wake-lock available (Android will not freeze it)"
else
  tip "termux-api missing: install it (pkg install termux-api) for the wake lock"
fi

if [ "$SHOWLOG" -eq 1 ]; then
  say ""
  say "=== server log ==="
  log_tail 60
fi

say ""
if [ "$fail" -eq 0 ] && running && [ "$warn" -eq 0 ]; then
  say "RESULT: up to date, all features present, desk running and answering."
  exit 0
fi
if [ "$fail" -ne 0 ]; then
  say "RESULT: the CODE is out of date or broken. Fix the MISS lines above, then re-run."
  exit 1
elif [ "$warn" -ne 0 ]; then
  say "RESULT: code is current, but the desk is not serving."
  say "        Start it:        bash termux/start-desk.sh"
  say "        Or let me do it: bash termux/verify-update.sh --start"
  say "        Keep it running: bash termux/install-boot.sh"
  exit 2
else
  say "RESULT: up to date, all features present, desk running and answering."
  exit 0
fi
