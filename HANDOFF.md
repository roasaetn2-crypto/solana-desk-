# HANDOFF — Solana Paper-Trading Desk

_Last updated: 2026-11-15 ~08:00 UTC. Written for another AI agent to pick up cold._

This document is the complete state of the project: what it is, what the user wants,
what has been done, what is still broken, and exactly what to do next. Read all of it
before touching code. It is deliberately blunt about what is proven vs. what is a guess.

---

## 1. WHO / WHAT / WHERE

- **User:** experienced coder (20 yrs) and meme-coin scalping trader. Runs a Solana
  paper-trading desk. Prefers direct action over discussion; has twice redirected me
  mid-investigation ("wait, save memory first"). Do not over-explain; ship.
- **Repo:** `roasaetn2-crypto/solana-desk-` (GitHub). Default branch `main`.
- **Local checkout:** `/workspace/solana-desk-`
- **Deploy:** Render, service name `Solana-desk-1`, service id `srv-dbq11fek1f9s73d9ql90`,
  public URL `https://solana-desk-1.onrender.com`. Auto-deploys on push to `main`.
- **Render owner id:** `tea-dbppepd6e07s73b2o7rg` (required for the `/v1/logs` endpoint).
- **Render API key:** pasted in chat and **compromised — rotate it.** Get the current key
  from Render dashboard → Account Settings → API Keys. (Value deliberately not written
  here; GitHub push protection blocks the literal string.)
- **GitHub PAT:** embedded in `git remote -v` as `https://<PAT>@github.com/...`.
  **Compromised (pasted in chat). Rotate it.** Note: pushing any file containing the
  literal PAT is blocked by GitHub push protection — that is why this doc has no secrets.
- **Runtime:** Render free tier: **512 MiB RAM**, no mounted disk, sleeps after ~15 min
  idle. Python **3.14.10** in a `.venv` at `/opt/render/project/src/.venv`.
- **Persistence:** Turso (libSQL) over HTTP `/v2/pipeline`. Tables:
  `journal(id INTEGER PRIMARY KEY AUTOINCREMENT, time, kind, mint, body TEXT NOT NULL)`
  and `kv(key TEXT PRIMARY KEY, value, updated_at)`. `store.restore()` runs at
  `app.py` line 82, **module level, before uvicorn reports `server_available`.**

---

## 2. WHAT THE USER ACTUALLY WANTS RIGHT NOW

The user flipped the exit ladder to **`patient`** on the live desk ("I changed patient").
That change **never took effect** because the desk was crash-looping from OOM kills.
So there are two jobs, in this order:

1. **Stop the OOM crash-loop** (the blocker; everything else is moot).
2. **Confirm the `patient` ladder is actually live.** `GET /api/health` → `exits.name`
   should read `patient`, with `source` = `operator` (set from dashboard) or `config`
   (edited in `config.py`). If it reads `fast`, the change was lost in a crash and must
   be re-applied once stable. Settings persist to `operator_settings.json` + Turso `kv`,
   but require a propose→confirm handshake, so an unconfirmed change is dropped.

Ladder reference (`desk/exits.py`):
- `fast` (default): stop 0.30, tp1 1.25 / sell 0.5, tp2 1.6 / sell 0.5, trail 0.15
- `patient`: stop 0.30, tp1 2.0 / sell 0.33, tp2 3.0 / sell 0.5, trail 0.20
- also `slow`, `single`.
Precedence: env `BRAIN_EXIT_LADDER` → `config.MEME["exit_ladder"]` → `DEFAULT_LADDER`.
`meme_tactics.ladder_info()` reports `{name, requested, source, valid, fallback_used,
known, ladder}`; `set_ladder()` writes `MEME["exit_ladder"]` and pops the env var.

---

## 3. ARCHITECTURE IN ONE SCREEN

"The Seats" pipeline, per token, per cycle:

```
Scanner (discovery + hard gates)
  → Chain facts (RPC: mint/freeze authority, top holders, concentration)
  → DexScreener facts + dev investigation (RugCheck creator graph)
  → FOMO evidence (optional, keyed)
  → Security (GoPlus / GMGN)
  → Meme scorecard
  → Analyst (LLM or rules, 0–10 thesis)
  → Signal engine (6 weighted components → 0–100 confidence)
  → Risk engine (sizing, caps, guard checklist)
  → Dialogue debate (LLM or template transcript)
  → Paper/live broker
```

Key modules:
- `desk/solana_agent.py` — cycle loop, seat wiring, `self.decisions`,
  `self.transcripts`, `self.tokens_seen`. **Where the OOM diagnostics live.**
- `desk/signal_engine.py` — weights: momentum 0.24, flow 0.22, liquidity 0.16,
  holders 0.14, narrative 0.12, safety 0.12. Gates: `min_confidence` 60,
  `min_win_probability` 0.52, `sell_confidence` 68, `require_bullish` True.
- `desk/exits.py` — price ladder (not indicators). Order: stop → runner trail → TP2 →
  TP1 → volume decay → timeout → max hold.
- `desk/meme_tactics.py` — rules A (ATR stop), B (pullback from 20-bar high), C
  (flash-pump breaker), D (volatility ceiling). **Only B is live.** A and D are wired
  but off because a 3,629-trade measurement said they don't beat the 15.4% baseline.
- `desk/chart.py` — timeframes `15s, 30s, 1m, 5m, 15m, 1H, 4H`. `_TICKS` is a
  `defaultdict(lambda: deque(maxlen=2400))`; `_OHLCV` is a per-`pool:tf` last-good cache
  (one pool refreshed per cycle, bounded in practice).
- `desk/scanner.py` — discovery + caches: `_EP_CACHE` (endpoint id → body, TTL 90 s,
  **never pruned**), `_POOL` (pruned), `_PAIR_CACHE` (mint → raw pairs, TTL, **never
  pruned**), `_QUOTE_CACHE` (mint → quote, TTL, **never pruned**), `_JUP_ROWS` (bounded
  at ~4000, the model prune pattern), `_JUP_HYDRATED` (bounded at 6000).
- `desk/store.py` — Turso persistence. `iter_journal_rows()` is keyset-paginated.
- `desk/brain_advisor.py` — advisory LLM second opinion. Fixed
  `ThreadPoolExecutor(max_workers=2)`, bounded `_cache` (CACHE_MAX), `_inflight` set,
  `PER_CYCLE` cap. `enabled()` is now True because the user added Ollama.
- `desk/ops.py` — `journal(kind, **fields)` appends to `data/journal.jsonl` then
  `store.append_journal(row)`. **No in-memory buffer.** Also holds `data_file()`,
  `rss_mb()` (added by me, diagnostic only).

---

## 4. WORK COMPLETED (all committed and pushed)

| Commit | What |
|---|---|
| `1cca524` | `brain_advisor.py`: added Ollama provider (local-first, cloud last). |
| `70da024` | Record how many tokens an AI entry holds; surfaced on the decision. |
| `0d28f3e` | Indicator research: added `5m` timeframe to `chart._TF`; backtest tooling. |
| `e11f1b1` | Two audit bugs: wired `facts["atr_pct"]` into meme tactics A/D; `_hlc()` normaliser so `true_ranges()` accepts both `h/l/c` and `high/low/close`; added `5m`. |
| `8b10206` | **THE REAL BOOT OOM FIX — keep this.** `desk/store.py`: replaced whole-table `SELECT body FROM journal ORDER BY id` with keyset pagination (`WHERE id > ? ORDER BY id LIMIT ?`) + streaming to `journal.jsonl.tmp` + atomic replace. Added `JOURNAL_PAGE` env (default 2000). Added 3 tests to `tests/test_persist.py`. |
| `c3ac7bb`..`e72d62d` | **Diagnostics only — all to be removed.** See §7. |

### The boot OOM that was fixed (do not undo)
Before `8b10206`, `restore()` parsed **69,372** journal rows into dicts at boot:
~31 MB of JSON → **~198 MB RSS** to parse, on a 512 Mi box. Now it streams: verified
live `store: restored journal:69495` in ~0.3 s, RSS 65 → 77 MB. Correct and tested
(13/13 `test_persist` tests pass).

---

## 5. THE CURRENT BUG: A SECOND OOM, IN THE RUNNING CYCLE

**This is the live problem. The journal fix did not solve it.**

### Evidence that it is not the journal
Render events show `server_failed` / `{"evicted": false, "oomKilled": {"memoryLimit":
"512Mi"}}` repeating every 1–3 min, starting **05:19:57, before any commit this
session**. `restore()` finishes first (RSS 77 MB), the box reaches `server_available`,
runs `scan: cycle 1`, then dies ~45 s later. The killer is in the cycle.

### Evidence of where the growth is (RSS instrumentation, 5 diag commits)
```
07:18:47 store: restore done rss=77.4MB
07:18:50 mem: after scan rss=82.5MB tokens=33
07:19:10 token MORI     rss=130.2MB   (+42MB)
07:19:24 token PROMPT   rss=202.5MB   (+84MB)
07:19:30 token USDGO    rss=349.5MB
07:19:31 mem: after token loop rss=390.3MB   <- dead shortly after
```
Pattern: tokens that die at shallow gates cost ~0 MB. Tokens that reach the **deep
seats** (chain → dexscreener → dev → security → meme → analyst → signal) cost
**~24–84 MB each, and it is never returned.**

### Evidence it is a live reference, not uncollected garbage
`gc.collect()` did **not** reclaim it (`blocks=337745->337504`, `4532948->4532779` —
after is same or higher). Object-type histogram showed `dict` and `list` exploding
(13,440 → 250,718 dicts; 3,024 → 83,553 lists) while `function`/`tuple`/`cell` stayed
flat — i.e. accumulated data records, ~3 dicts per list.

### THE DECISIVE FINDING (tracemalloc, commits `b06f324` / `e72d62d`)
tracemalloc was started after the scan and sampled per token. The result **overturns
the "Python object leak" theory**:

```
07:51:34 token HOGE       rss=82.7MB  traced_cur=506KB   fds=15 threads=5
07:51:36 token TAIL       rss=83.8MB  traced_cur=750KB   fds=15 threads=5
07:51:37 token ATC        rss=84.7MB  traced_cur=1020KB  fds=15 threads=5
07:51:40 token HWAN       rss=84.8MB  traced_cur=1030KB  fds=15 threads=5
07:51:42 token PREDICTOOR rss=86.4MB  traced_cur=1549KB  fds=17 threads=6
```
Earlier run at higher RSS:
```
07:48:13 token poorcat rss=106.7MB traced_cur=3897KB traced_peak=4052KB
```

**`traced_cur` is ~1–4 MB while RSS is 85–107 MB.** That means:

> There is **no Python-heap leak**. Essentially all of the growth is allocated
> **outside Python's tracked allocator** — i.e. in C extensions, thread stacks, or
> glibc malloc arenas / fragmentation.

The `dict`/`list` counts that looked like a leak are consistent with transient
allocation that tracemalloc shows is *not retained* — they were a red herring created
by `gc.get_objects()` sampling mid-flight.

Also note: the biggest tracemalloc entry in one run was
`json/decoder.py:361` (~1.2 MB, 19.5k objects) — a **parsed JSON body still referenced
at sample time**. And `linecache.py:198` (~1.5 MB) is **my own diagnostic** reading
source files — ignore it.

### What has been RULED OUT with evidence
- Journal restore (finishes first, RSS 77 MB).
- `scanner._EP_CACHE / _POOL / _PAIR_CACHE / _QUOTE_CACHE / _JUP_ROWS` — logged flat
  (`{'ep':1,'pool':22,'pair':0,'quote':0,'jup':100,'cool':0,'host_cool':2}`).
- `chart._TICKS` (`ticks=0/2`) and `chart._OHLCV` (one pool/cycle).
- `helius._CACHE` (`helius=0/4`).
- Bounded deques: `decisions` (60), `transcripts` (40), `events` (400).
- `ops.journal()` (no buffer).
- `brain_advisor`: bounded `_cache`, `PER_CYCLE` cap, fixed 2-worker pool.
- Module-level caches in the deep-seat modules — **none exist** (grep of
  `analyst.py`, `signal_engine.py`, `meme.py`, `dialogue.py`, `security.py`,
  `dexscreener.py`, `devcheck.py`, `links.py`, `social.py` found only local lists).
- FD leak: `fds` stayed 15–17 across deep tokens.
- Thread explosion: `threads` went 5 → 6 (the advisor worker), not +1 per token.
- Real payload sizes: DexScreener token-profiles = 24 KB / 455 objects; a single pair
  = 1.4 KB / 58 objects. So scanner caches cannot hold 24 MB/token.

### The remaining suspects (in priority order)
1. **glibc arena fragmentation.** Python 3.14 on Linux uses per-thread malloc arenas;
   `requests`/`urllib3`/`ssl` allocate and free large buffers per call. With a 2-worker
   pool + the main thread, several arenas each hold freed-but-unreturned pages. RSS
   climbs, `traced_cur` stays tiny. **This best fits every observation.** Mitigation:
   `MALLOC_ARENA_MAX=2`, `MALLOC_TRIM_THRESHOLD_=131072`, `MALLOC_MMAP_THRESHOLD_=131072`
   in the Render env, and/or `gc.freeze()` after boot.
2. **A C extension holding memory per deep token.** Candidates: `pydantic` v2 (Rust
   core — `thesis.model_dump()` and `Token` models are built per token), `ssl`,
   `websocket-client` (the PumpPortal feed). `traced_cur` not moving is consistent with
   Rust-side allocation invisible to tracemalloc.
3. **A large response body held only while a deep token is in flight**, e.g. a
   `getTokenLargestAccounts` / RugCheck report. `chain.py` limits to
   `CHAIN["largest_accounts_checked"]`, so this should be small — but verify.
4. **Thread stacks.** Each thread reserves ~8 MB virtual (usually lazily committed).
   Only relevant if thread count actually climbs over a long run — re-measure.

---

## 6. EXACT NEXT STEPS (copy-paste)

### 6.1 Confirm current live behaviour
```bash
KEY=<render key>; OWN=tea-dbppepd6e07s73b2o7rg; SVC=srv-dbq11fek1f9s73d9ql90
curl -s -H "Authorization: Bearer $KEY" \
  "https://api.render.com/v1/services/$SVC/deploys?limit=3" | python3 -m json.tool | head -40
curl -s -H "Authorization: Bearer $KEY" \
  "https://api.render.com/v1/logs?ownerId=$OWN&resource=$SVC&limit=1000" -o /tmp/L.json
python3 - <<'EOF'
import json
d=json.load(open('/tmp/L.json')); logs=d.get("logs",[])
print("entries",len(logs),"hasMore",d.get("hasMore"))   # NOTE: chronological, oldest first
for l in logs[-200:]:
    m=l.get("message","")
    if "mem:" in m or "oom" in m.lower() or "Killed" in m:
        print(l.get("timestamp","")[11:19], m[:200])
EOF
```
Render events (to see `oomKilled`):
```bash
curl -s -H "Authorization: Bearer $KEY" \
  "https://api.render.com/v1/services/$SVC/events?limit=50" | python3 -m json.tool | head -60
```

### 6.2 Test the fragmentation hypothesis FIRST (cheapest, most likely)
Add to Render env vars and redeploy (no code change):
```
MALLOC_ARENA_MAX=2
MALLOC_TRIM_THRESHOLD_=131072
MALLOC_MMAP_THRESHOLD_=131072
PYTHONMALLOC=malloc
```
Then watch `rss` at `after token loop` for 3–4 cycles. If the per-token +24 MB stops,
it was fragmentation. If not, move to 6.3.
Optional code complement: call `gc.freeze()` once after boot (after `restore()`), so
long-lived objects are excluded from GC scans (minor CPU win, not a memory fix).

### 6.3 If fragmentation is not it — bisect the seats
The instrumentation already prints `seat <name> rss=...` after each seat
(`solana_agent.py` lines ~658–802). For one deep token, diff consecutive seat lines:
the seat whose line jumps is the culprit. Then instrument *inside* that seat only
(e.g. time each HTTP call and log `len(response.content)` and `rss_mb()`).

### 6.4 If it is pydantic
Test by replacing `thesis.model_dump()` call sites with a plain dict build, or pin
`pydantic<2` temporarily, and see if per-token growth drops. (`requirements.txt` has
`pydantic>=2.7`.)

### 6.5 If nothing else works — bound the cycle
Hard-cap work per cycle: limit deep-seat tokens per cycle (there is already
`chain_left` / `analyst_left`), and/or run on a smaller `SCAN` set. A 512 Mi box cannot
deep-process 30+ tokens per cycle if each costs 24 MB and never returns.

---

## 7. DIAGNOSTICS TO DELETE (exact locations)

All added by me purely to find the leak. **Remove before shipping.** Nothing else
depends on them except `rss_mb` / `cache_stats` themselves.

`desk/ops.py`
- `def rss_mb()` at line ~25 (whole function). Remove after removing all callers.

`desk/store.py`
- line 23: `from desk.ops import data_file, rss_mb` → drop `rss_mb`
- line ~374: `print(f"[desk] store: restore start rss={rss_mb()}MB", flush=True)`
- line ~443: `print(f"[desk] store: restore done rss={rss_mb()}MB", flush=True)`

`desk/scanner.py`
- `def cache_stats()` at line ~143 (whole function) — or keep it; it is cheap,
  read-only and genuinely useful for an `/api/health` block. **Recommended: keep.**

`desk/solana_agent.py`
- line 20: `import gc` (remove if unused after cleanup)
- line 23: `import sys` (remove if unused after cleanup)
- line 48: `from desk.ops import ... rss_mb` → drop `rss_mb`
- line 565: `print(f"[desk] mem: after scan rss=...")`
- lines 566–572: the `tracemalloc` start block
- lines ~603–618: the per-token probe block (tracemalloc + fds + threads print)
- lines ~658, 670, 672, 741, 774, 793, 802: the seven `seat <name> rss=...` prints
- lines ~1063–1069: the `containers ...` probe block
- line ~1070: `print(f"[desk] mem: after token loop ...")`

Verify removal with:
```bash
grep -rn "rss_mb\|mem: \|tracemalloc" desk/ | grep -v "def cache_stats"
```
(Should show only the `cache_stats` definition if you keep it.)

---

## 8. TEST BASELINE (do not be alarmed)

Run from `tests/` with `PYTHONPATH=..`:
```bash
cd tests && PYTHONPATH=.. python3 -m unittest discover
```
Baseline: **670 tests**, with **1 pre-existing failure**, **10 pre-existing import
errors**, **17 skipped**:
- 1 failure: `test_settings.Owner.test_owner_bankroll_proposal_confirm_persists_and_journals`
- 10 import errors: all `ModuleNotFoundError: No module named 'pytest'` (tests written
  for pytest, run under unittest). Not caused by any change here.
- `tests/test_persist.py`: all 13 pass (includes the 3 new journal-pagination tests).

---

## 9. OTHER THINGS FLAGGED, STILL NOT DONE

- **Rotate the GitHub PAT** (in `git remote -v`) and **both Render API keys** — all
  were pasted in chat. High priority, unrelated to the OOM.
- **Prune `scanner._EP_CACHE`, `_PAIR_CACHE`, `_QUOTE_CACHE`.** They have TTLs but
  entries are never removed, so on a long-lived box they grow without bound. Use the
  `_JUP_ROWS` bounded-prune pattern (drop oldest ~1000 when over a cap). Latent leak,
  not the current one, but worth fixing.
- **`self.tokens_seen` is an unbounded `dict[str, dict]` keyed by mint**, never pruned.
  It holds the full token dict per mint ever seen. Over days this grows. Add a cap / LRU.
  (Not the current OOM — small per entry — but unbounded.)
- **`self.decisions` / `self.transcripts` store the full `token`/`facts`/`ds`/`dev`
  blobs.** maxlen 60/40 bounds the count, but raising it would explode memory.
  Consider storing only display fields.
- **`/api/health` 502s** are usually free-tier cold start, or `x-render-routing:
  no-deploy` when a deploy has not registered yet. Retry after ~60 s.

---

## 10. ONE-PARAGRAPH SUMMARY

The desk is a Solana meme-coin paper-trading bot on Render's free 512 Mi tier. The boot
OOM (parsing 69k journal rows) is **fixed** (`8b10206`, keyset pagination + streaming,
tested). A **second OOM remains, in the running cycle**: deep-seated tokens add
~24–84 MB each and it is never released, killing the box every 1–3 minutes, which is
why the user's `patient` exit-ladder change never took effect. Fresh tracemalloc
evidence shows `traced_cur` is only ~1–4 MB while RSS is ~100 MB, so **there is no
Python-heap leak** — the growth is outside Python's tracked allocator (most likely
glibc arena fragmentation from `requests`/`ssl`, possibly pydantic v2's Rust core or a
C extension). The next action is to set `MALLOC_ARENA_MAX=2` (+ trim/mmap thresholds)
in Render env and redeploy, then re-measure per-token RSS; if that fails, bisect the
seats using the existing `seat <name> rss=` logs. All the `[desk] mem:` / `rss_mb` /
tracemalloc instrumentation is diagnostic scaffolding listed in §7 and must be deleted
before the final commit. Then verify `GET /api/health` reports `exits.name == "patient"`.
