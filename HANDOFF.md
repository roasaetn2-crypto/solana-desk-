# HANDOFF — Solana Desk: OOM crash loop investigation

Written 2026-11-15 ~07:40 UTC. Everything below is verified from live logs, git
history, and code reads. Where something is a hypothesis it says so.

---

## 1. Who / what / where

| Thing | Value |
|---|---|
| Project | Solana paper-trading desk ("The Seats" architecture) |
| Repo | `roasaetn2-crypto/solana-desk-` (branch `main`) |
| Local path | `/workspace/solana-desk-` |
| Host | Render, service `Solana-desk-1`, id `srv-dbq11fek1f9s73d9ql90` |
| URL | `https://solana-desk-1.onrender.com` |
| Plan | **free** — 512 Mi RAM, **no mounted disk**, sleeps after ~15 min idle |
| Render API key that works | `<RENDER_KEY_WORKING_REDACTED>` |
| Render API key that does NOT | `<RENDER_KEY_DEAD_REDACTED>` (no access to this service) |
| Owner id (needed for logs API) | `tea-dbppepd6e07s73b2o7rg` |
| Store | Turso / libSQL, tables `journal`, `kv` |
| Journal size | **~69,500 rows and growing** |

### ⚠️ Credentials that must be rotated

The git remote URL has a GitHub PAT embedded in it:

```
git remote -v
origin  https://<GITHUB_PAT_REDACTED>@github.com/roasaetn2-crypto/solana-desk-.git
```

That token is in `.git/config` and has been pasted in chat. Both Render keys
were also pasted in chat. **Rotate all three.** This has been flagged 3 times
and not done.

---

## 2. What the user actually asked for

The user is an experienced coder and meme-coin scalping trader. Their last
instruction was **"I changed patient"** — they flipped the exit ladder from
`fast` to `patient` on the live desk, following my recommendation.

That change is **not visible on the live desk** because the box is being
OOM-killed every 1–3 minutes. Making the box stable is the prerequisite for
their change to take effect. That is the whole job right now.

**Do not lose sight of this:** the ladder change is the user's goal; the OOM is
the blocker.

---

## 3. Work completed this session (all committed and pushed)

| Commit | What |
|---|---|
| `e11f1b1` | fix(tactics): feed the ATR switches, gate pullback on 5m bars |
| `8b10206` | **fix(store): stream the journal at boot instead of loading 69k rows** |
| `c3ac7bb` | diag(store): log RSS at restore / after scan / per token |
| `2d55315` | diag(agent): log RSS after each seat |
| `2d9b400` | diag(agent): log allocated blocks + gc objects |
| `524abe3` | diag(scan): log cache sizes per token |
| `1b3aa67` | diag(agent): log top object types + gc.collect delta (HEAD) |

`8b10206` is a **real, keep-worthy fix**. Everything after it is **temporary
diagnostics that must be deleted** (see §7).

### 3.1 The real fix (`8b10206`) — keep this

`desk/store.py` used to do `SELECT body FROM journal ORDER BY id`, parse all
69,372 rows into dicts at import time, then rewrite the file. Measured: 31 MB
of JSON, **~198 MB RSS to parse**. On 512 Mi that alone was fatal.

Replaced with keyset pagination + streaming:

- `JOURNAL_PAGE` (env `JOURNAL_PAGE`, default 2000)
- `iter_journal_rows(page=None)` — `WHERE id > ? ORDER BY id LIMIT ?`, yields
  one page at a time. Keyset, not OFFSET (OFFSET is quadratic).
- `load_journal_rows()` kept as `list(iter_journal_rows())` for compatibility.
- `restore()` journal block now has 3 branches: remote empty → push local up;
  `local_n >= remote_n` → `journal:cached:N` short-circuit (mounted-disk case
  does zero work); otherwise stream to `journal.jsonl.tmp` and atomic
  `tmp.replace()`.
- Tests added in `tests/test_persist.py`: paging test (asserts `LIMIT` and
  `id >` on every query), streaming test (40 rows → `journal:40`), and the
  existing roundtrip test updated to expect `journal:3` instead of `journal`.

**Verified live:** logs now show
`store: restored journal:69495` completing in ~0.3 s, RSS 65 → 77 MB.
Restore is **no longer the problem**.

---

## 4. The OOM is NOT the journal (this is the key finding)

Render events API shows `server_failed` with
`{"evicted": false, "oomKilled": {"memoryLimit": "512Mi"}}` repeating every
1–3 minutes, **starting 05:19:57 — before any commit this session**. So it
predates all my work.

Critically: `restore()` runs at `app.py` line 82, **before** uvicorn reports
`server_available`. The box reaches `server_available`, runs `scan: cycle 1`,
and *then* dies ~45 s later. So the killer is in the **running cycle**, not boot.

### 4.1 The evidence chain (RSS instrumentation)

```
07:18:47 store: restore done rss=77.4MB          <- boot fine
07:18:50 mem: after scan rss=82.5MB tokens=33    <- scan fine
07:18:51 token HOGE        rss=84.3MB
07:18:53 token GTALAMBO    rss=84.5MB
07:18:54 token USDC        rss=84.5MB
07:19:02 token HWAN        rss=88.5MB
07:19:04 token Max         rss=88.5MB
07:19:10 token MORI        rss=130.2MB           <- +42MB
07:19:21 token Juan        rss=118.2MB
07:19:24 token PROMPT      rss=202.5MB           <- +84MB
07:19:27 token horse       rss=287.0MB           <- +84MB
07:19:30 token USDGO       rss=349.5MB           <- +62MB
07:19:31 mem: after token loop rss=390.3MB       <- dead shortly after
```

Pattern: tokens that pass the shallow gates are cheap (~0 MB). Tokens that
reach the **deep seats** (chain → dexscreener → dev → security → meme →
analyst → signal) cost **~64–84 MB each, retained, never returned**.

### 4.2 It is a real reference leak, not allocator bloat

Added `sys.getallocatedblocks()` + `len(gc.get_objects())` next to RSS:

```
07:29:27 token DCI   rss=104.3MB blocks=641880   objs=102119   (+312k blocks, +25k objs)
07:29:41 token Max   rss=157.1MB blocks=1310044  objs=151524   (+980k blocks, +75k objs)
07:34:26 token ATC   rss=206.2MB blocks=2066786  objs=209334   (+1.7M blocks, +132k objs)
07:34:29 token Christmax rss=270.8MB blocks=3182502 objs=292961
07:34:31 token Rings rss=318.8MB blocks=3803695  objs=338620
```

Blocks and gc objects climb **with** RSS. If it were glibc arena bloat, blocks
would stay flat while RSS rose. They don't. **This is a genuine Python object
leak of ~25k–132k objects per deep token.**

### 4.3 The jumps are NOT localised to the seat that allocates

Per-seat logging showed the growth landing on seats that cannot possibly
allocate it — `signal` +118 MB, `security` +69 MB, `meme` +74 MB. `signal_engine.evaluate`
and `meme.scorecard` are pure Python over small dicts. RSS is process-wide, so
this means **something else is allocating concurrently** while the main thread
logs — most likely a worker thread.

### 4.4 Ruled OUT (with evidence)

| Suspect | Why ruled out |
|---|---|
| Journal restore | Completes in 0.3 s at 77 MB; box reaches `server_available` after it |
| `scanner._EP_CACHE` / `_POOL` / `_PAIR_CACHE` / `_QUOTE_CACHE` / `_JUP_ROWS` | `cache_stats()` logged every token — all **flat** (`ep:1 pool:22 pair:0 quote:0 jup:100`) |
| `chart._TICKS` | `ticks=0` / `ticks=2` — flat |
| `helius._CACHE` | `helius=0` / `helius=4` — flat |
| `self.decisions` / `transcripts` / `events` | Bounded deques (`maxlen` 60 / 40 / 400) |
| `ops.journal()` | Writes to disk + Turso; no in-memory buffer |
| `groq_postmortem.build_cases()` | Scripts-only, never called in the live loop |
| `brain_advisor.review()` | Returns early (`not self.enabled()` — no model configured) |
| Thread explosion | `_advise`/`_advise_exit` use a fixed 2-worker `ThreadPoolExecutor`; `dialogue.debate` spawns no threads |

### 4.5 Leading hypotheses (ranked, NOT yet confirmed)

1. **A per-token object graph retained by a live reference in the deep path.**
   Candidates that build large structures per token:
   - `dialogue.debate()` → `convo["messages"]`, stored in `transcript` AND
     `decision["dialogue"]`. Two deques hold these (60 + 40 = up to 100
     transcripts, each with a full message list). If each transcript is
     ~1 MB that is ~100 MB steady-state — plausible but should plateau, not
     grow unboundedly.
   - `decision` dict itself is huge: `token + facts + links + thesis +
     dexscreener + dev + scorecard + dialogue + signal + sizes`. Stored in
     `self.decisions` (maxlen 60). If each is ~2 MB → 120 MB. Again should
     plateau.
   - **`analyst.analyze_token`** — if it caches prompts/responses per mint in a
     module global, that is unbounded. **NOT YET INSPECTED.**
   - **`signal_engine.evaluate`** — same. **NOT YET INSPECTED.**
   - **`meme.scorecard`** — same. **NOT YET INSPECTED.**
2. **A cycle in the object graph that gc cannot collect** because a deque or
   module global holds a reference to a dict that references back. The
   `gc.collect()` delta instrumentation (HEAD, `1b3aa67`) was deployed to test
   this but **the logs were not yet read** when this handoff was written.
3. **`requests.Session` response objects retained** — if any seat stores
   `r.text` or the response object rather than a parsed field.

**The single highest-value next action is to read the `top=[...]` object-type
list from the HEAD deploy.** That names the leaking type directly and collapses
the search space. See §6.

---

## 5. Current state of the live box

- HEAD `1b3aa67` is deployed and live (deploy went live 07:36:51).
- It is almost certainly still OOM-looping. Confirm with the events API (§6.1).
- `exits.ladder` on `/api/health` has never been observed as `patient` because
  the box never stays up long enough to read it. **Unverified.**

---

## 6. Exact next steps

### 6.1 Read the object-type logs (do this first)

```bash
KEY=<RENDER_KEY_WORKING_REDACTED>
OWN=tea-dbppepd6e07s73b2o7rg
SVC=srv-dbq11fek1f9s73d9ql90
curl -s -H "Authorization: Bearer $KEY" \
  "https://api.render.com/v1/logs?ownerId=$OWN&resource=$SVC&limit=1000" \
  -o /tmp/logs.json
python3 -c "
import json
d=json.load(open('/tmp/logs.json'))
for l in d['logs']:
    m=l['message']
    if 'mem: token' in m:
        print(l['timestamp'][11:19], m[:300])
" | tail -20
```

Look at `blocks=BEFORE->AFTER` and `top=[...]`:

- If `AFTER` is much lower than `BEFORE` → uncollected cycles; fix the cycle
  (or add `gc.freeze()` after boot + periodic `gc.collect()`).
- If `AFTER ≈ BEFORE` → live reference; the `top` list names the type.

Also confirm the loop is still happening:

```bash
curl -s -H "Authorization: Bearer $KEY" \
  "https://api.render.com/v1/services/$SVC/events?limit=10" | python3 -c "
import sys,json
for r in json.load(sys.stdin):
    e=r.get('event',{})
    print(e.get('timestamp','')[:19], e.get('type'), json.dumps(e.get('details') or {})[:80])
"
```

### 6.2 Then inspect the three unexamined modules

```bash
cd /workspace/solana-desk-
grep -nE "^_[A-Z][A-Z0-9_]* *[:=]|defaultdict|deque\(|lru_cache|\.append\(" \
  desk/analyst.py desk/signal_engine.py desk/meme.py desk/dialogue.py
```

Any module-level dict/list that is written per-mint and never pruned is the
leak. The fix pattern is already established in this codebase — see
`_JUP_ROWS` in `desk/scanner.py` line ~642, which does exactly the right thing:

```python
if len(_JUP_ROWS) > 4000:                       # bounded: drop the oldest quarter
    for m, _ in sorted(_JUP_ROWS.items(), key=lambda kv: kv[1]["at"])[:1000]:
        _JUP_ROWS.pop(m, None)
```

### 6.3 Also worth doing regardless

`scanner._EP_CACHE`, `_PAIR_CACHE`, `_QUOTE_CACHE` have TTLs but are **never
pruned** — expired entries live forever. They were flat during the observed
window (short uptime) but they *will* grow on a long-lived box. Add a prune on
write, same pattern as `_JUP_ROWS`. This is a latent leak even if it is not
today's killer.

### 6.4 After the leak is fixed

1. Remove all diagnostics (§7), keep `rss_mb()` optionally.
2. Run the test suite (§8).
3. Commit, push, deploy, confirm `uptime_s` climbs monotonically across
   several minutes instead of resetting.
4. **Then verify the user's ladder change:**
   `curl -s https://solana-desk-1.onrender.com/api/health` → `exits.name`
   should be `patient`. If it reads `fast`, the change was lost in a crash.
   Settings persist to `operator_settings.json` + Turso `kv`, so it *should*
   survive — but it needs propose→confirm, so it may never have been confirmed.
   The user may need to re-apply it once the desk is stable.

---

## 7. Diagnostics to DELETE (all added after `8b10206`)

Revert cleanly with `git revert --no-commit c3ac7bb 2d55315 2d9b400 524abe3 1b3aa67`
then remove leftovers, or delete by hand:

**`desk/ops.py`** — `rss_mb()` helper (keep it, it's harmless and useful).

**`desk/scanner.py`** — `cache_stats()` (keep, harmless).

**`desk/store.py`**
- line 23: `from desk.ops import data_file, rss_mb`
- line 374: `print(f"[desk] store: restore start rss={rss_mb()}MB", flush=True)`
- line 443: `print(f"[desk] store: restore done rss={rss_mb()}MB", flush=True)`

**`desk/solana_agent.py`**
- imports: `import gc`, `import sys` (lines ~19-22), `rss_mb` in the
  `from desk.ops import ...` line (line 46)
- after scan: `print(f"[desk] mem: after scan rss={rss_mb()}MB tokens={len(tokens)}", flush=True)`
- per-token probe block: the `try: _b0 = sys.getallocatedblocks() ... except ...`
  block at the top of `for t in tokens:`
- 7 × `print(f"[desk] seat <name> {t.get('symbol')} rss={rss_mb()}MB", flush=True)`
  (chain, dexscreener, dev, security, meme, analyst, signal)
- after loop: `print(f"[desk] mem: after token loop ...", flush=True)`

Find them all with:

```bash
cd /workspace/solana-desk-
grep -rn "rss_mb()\|getallocatedblocks\|mem: token\|mem: after\|seat .* rss=" desk/
```

---

## 8. Test suite baseline

```bash
cd /workspace/solana-desk-/tests
PYTHONPATH=.. python3 -m unittest discover
```

Expected: **670 tests, 1 failure, 10 errors, 17 skipped.**

- The 1 failure is **pre-existing**:
  `test_settings.Owner.test_owner_bankroll_proposal_confirm_persists_and_journals`
- The 10 errors are all `ModuleNotFoundError: No module named 'pytest'` —
  those test files need pytest, which is not installed. **Pre-existing.**

`test_persist` alone: 13 tests, all pass.

---

## 9. Architecture notes the next bot will need

### The Seats (in cycle order, `desk/solana_agent.py::run_once`)
Scanner → Chain facts → DexScreener + dev investigation → FOMO (optional) →
Security → Meme scorecard → Analyst (0–10) → Signal engine → Risk engine →
Dialogue debate → Paper/live broker.

### Exit ladders (`desk/exits.py` + `desk/meme_tactics.py`)
| name | stop | tp1 | tp2 | trail |
|---|---|---|---|---|
| `fast` (default) | 0.30 | 1.25 / 0.5 | 1.6 / 0.5 | 0.15 |
| `patient` | 0.30 | 2.0 / 0.33 | 3.0 / 0.5 | 0.20 |

Precedence: env `BRAIN_EXIT_LADDER` → `config.MEME["exit_ladder"]` →
`DEFAULT_LADDER`. `meme_tactics.ladder_info()` reports
`{name, requested, source, valid, fallback_used, known, ladder}`.
`set_ladder()` writes `MEME["exit_ladder"]` and pops the env var.

### Why `patient` was recommended but is contested
`LADDER-REPLAY.md` says `patient` earns ~3× `fast` on expectancy, but that
replay used **G87E trend entries**. The live desk takes **pullback entries**,
and `scripts/winrate_lab.py` (which sweeps ladders *and* entry filters
together on 120 pools) had `"patient 2x/3x"` **lose** to `fast` (the 68.9% win
/ PF 3.75 promotion). Also `winrate_lab`'s patient cell used `trail_pct` 0.25
while `LADDERS["patient"]` ships 0.20. Both datasets are gitignored
(`data/`, `backtest_g87e.json`) so neither replay is reproducible from the
checkout. Recommendation was **try `patient` live, don't commit it as the
default** — which is what the user did.

### Meme tactics
Rules A (ATR stop), B (pullback from 20-bar high), C (flash-pump breaker),
D (volatility ceiling). Only **B is live**. A and D were inert until `e11f1b1`
wired `facts["atr_pct"]`; they stay `False` because the 3,629-trade measurement
said they don't beat the 15.4% baseline.

---

## 10. One-paragraph summary for a fresh bot

The Solana desk on Render free tier (512 Mi) is OOM-killed every 1–3 minutes.
The boot-time journal load was a real cause and is **fixed** (`8b10206`,
streams 69,495 rows in 0.3 s at 77 MB). But the box still dies ~45 s into the
first cycle, and instrumentation proves it is a **genuine Python reference
leak of 25k–132k objects (~64–84 MB) per token that reaches the deep seats**,
not allocator bloat and not any of the scanner/chart/helius caches (all logged
flat). The growth lands on seats that cannot allocate it, implying a concurrent
worker thread. HEAD `1b3aa67` logs the top object types and the
`gc.collect()` block delta — **read those logs first**, they name the leaking
type. Then inspect `desk/analyst.py`, `desk/signal_engine.py`, `desk/meme.py`
and `desk/dialogue.py` for a module-level per-mint cache that is never pruned.
Fix with the bounded-prune pattern already used by `_JUP_ROWS` in
`desk/scanner.py`. Then delete the diagnostics (§7), re-run tests (§8), deploy,
and finally confirm the user's `patient` ladder change actually took effect on
`/api/health` → `exits.name`.
