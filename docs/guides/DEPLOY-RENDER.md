# Deploying Solana Desk on Render

This document explains **why the deploy was failing** and exactly how to get it
live on Render.

---

## Why it would not deploy

Two independent problems:

### 1. The service crashed on boot because `DESK_TOKEN` was not set

`start.sh` used to do this:

```bash
if [ -z "${DESK_TOKEN:-}" ]; then
  echo "FATAL: DESK_TOKEN is not set."
  exit 1
fi
```

`render.yaml` declared the token as `sync: false`, which means **you must type it
into the dashboard yourself**. If you deployed before doing that (very easy to
miss), the container exited immediately with `FATAL: DESK_TOKEN is not set` and
Render marked the deploy as failed.

**Fix applied:**
- `render.yaml` now uses `generateValue: true`, so Render creates a random
  secret for you automatically — no manual step, no crash.
- `start.sh` is now resilient: if no token is supplied it generates one,
  persists it to `.desk_token`, and prints it in the deploy logs instead of
  exiting. A deploy can never fail because of a missing token again.

### 2. The code was internally inconsistent (this broke the dashboard)

Several files had been overwritten with older versions (the `Update config.py`,
`Update broker.py`, `Update scanner.py`, ... commits). The result was a mix of
old and new code that did not agree with each other:

| Symptom | Cause |
|---|---|
| `GET /api/state` -> 500 | `PaperBroker` had no `realized_equity()` but `solana_agent.py` called it |
| `GET /api/settings` -> 500 | `EXITS["profiles"]`, `RISK["compound"]`, `PAPER["min_ticket_usd"]` were missing from `config.py` |
| `GET /api/backtest` -> 500 | `SCAN["max_h1_change_pct"]` was missing |
| health `desk` block -> `{"error":"KeyError"}` | `EXITS["profiles"]` missing |
| autotune snapshot crash | `AUTOTUNE["gates"]` was a list, code expected a dict |

**Fix applied:** all 7 regressed files were restored to the last coherent commit
(`d57db8d`, "Owner settings"), which is the version the rest of the code was
written against:

```
config.py  desk/broker.py  desk/chain.py  desk/devcheck.py
desk/dialogue.py  desk/meme.py  desk/scanner.py
```

After the restore: **444 tests pass, 17 skipped**, and every API endpoint
returns HTTP 200.

---

## Deploy on Render — the easy way (Blueprint)

1. Push this fixed repo to GitHub.
2. In Render: **New -> Blueprint**.
3. Pick this repository. Render reads `render.yaml` automatically.
4. Click **Apply**. That's it.

`render.yaml` already sets:
- Build: `pip install --no-cache-dir -r requirements.txt`
- Start: `bash start.sh`
- Health check: `/api/health`
- `PYTHON_VERSION=3.11.9`
- `DESK_TOKEN` generated automatically

### Find your login key

Render dashboard -> your **solana-desk** service -> **Environment** -> copy the
value of **`DESK_TOKEN`**. Open the service URL and paste it into the login box.

---

## Deploy on Render — manual Web Service

If you don't use the Blueprint:

| Setting | Value |
|---|---|
| Environment | Python 3 |
| Build Command | `pip install --no-cache-dir -r requirements.txt` |
| Start Command | `bash start.sh` |
| Health Check Path | `/api/health` |

Environment variables:

| Key | Value |
|---|---|
| `PYTHON_VERSION` | `3.11.9` |
| `DESK_TOKEN` | any strong secret you choose (e.g. `openssl rand -base64 24`) |
| `PYTHONUNBUFFERED` | `1` |

If you skip `DESK_TOKEN`, `start.sh` will generate one and print it in the
**Logs** tab — look for the banner `DESK_TOKEN was not set by the host`.

---

## Verify it is healthy

Open:

```
https://YOUR-SERVICE.onrender.com/api/health
```

You should see `"ok": true`. The `desk` block should contain numbers, **not**
`{"error": ...}`.

---

## Notes / gotchas

- **Free plan sleeps** after ~15 minutes of inactivity. The first request after
  a sleep takes ~30-60 seconds to wake. The built-in `keepalive` pings the
  public URL to reduce this.
- **Ephemeral disk.** Paper state (`desk.db`, `journal.jsonl`, `.desk_token`)
  is wiped on every redeploy. Set `DESK_TOKEN` explicitly if you want a stable
  key.
- **Live trading is disabled** in this build by design; it runs in paper mode.
- If a deploy still fails, open the **Logs** tab and read the last lines — the
  app now prints a clear banner and any traceback there.
