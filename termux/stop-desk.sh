#!/data/data/com.termux/files/usr/bin/bash
# Stop the desk cleanly: supervisor first (so it cannot respawn), then uvicorn.
#
#   bash ~/solana-desk/termux/stop-desk.sh
#
# Order matters. Killing uvicorn first just makes the supervisor start another
# one 2s later; that is the "I stopped it and it came back" bug.
set -u

DESK_DIR="${DESK_DIR:-$HOME/solana-desk}"
LOCKDIR="$DESK_DIR/.supervisor.lock"

cd "$DESK_DIR" 2>/dev/null || true
say() { printf '%s\n' "$*"; }

# Match a process by an EXACT argument in /proc/PID/cmdline, never by a substring
# of the whole command line. `pgrep -f "start-desk.sh"` also matches any shell
# whose -c string merely mentions the name -- including the shell running this
# script -- and killing that kills the operator's own terminal.
# $1 = loose pgrep pattern (pgrep matches the WHOLE command line, so it cannot
#      take an anchored regex), $2 = anchored regex tested against each argv
#      element from /proc/PID/cmdline.
pids_matching() {
  local loose="$1" exact="$2" pid
  for pid in $(pgrep -f -- "$loose" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    [ -r "/proc/$pid/cmdline" ] || continue
    if tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -Eq -- "$exact"; then
      echo "$pid"
    fi
  done
}

# 1. supervisor -- must die first, or it respawns uvicorn behind us
sup=0
for pid in $(pids_matching 'start-desk\.sh' '(^|/)start-desk\.sh$'); do
  kill "$pid" 2>/dev/null && { say "  stopped supervisor (pid $pid)"; sup=1; }
done
[ "$sup" -eq 0 ] && say "  no supervisor running"
rm -rf "$LOCKDIR" 2>/dev/null || true

# 2. uvicorn -- exact 'app:app' argument
uv=0
for pid in $(pids_matching 'app:app' '^app:app$'); do
  kill "$pid" 2>/dev/null && { say "  stopped uvicorn (pid $pid)"; uv=1; }
done
[ "$uv" -eq 0 ] && say "  no uvicorn running"

# 3. give them a moment, then insist
i=0
while [ "$i" -lt 5 ]; do
  sleep 1; i=$((i + 1))
  [ -z "$(pids_matching 'app:app' '^app:app$')" ] && break
done
for pid in $(pids_matching 'app:app' '^app:app$'); do
  say "  uvicorn $pid ignored SIGTERM; sending SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
done
for pid in $(pids_matching 'start-desk\.sh' '(^|/)start-desk\.sh$'); do
  say "  supervisor $pid ignored SIGTERM; sending SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
done

command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock
say "desk stopped."
