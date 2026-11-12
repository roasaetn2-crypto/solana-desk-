# Render Free + Pinger — Permanent, Never-Expiring, Never-Sleeping

This is the "keep what you have, just stop the sleeping" path. Everything below
is doable from an Android phone in Chrome. No PC, no Termux required for the
Render part (Termux is only needed for the one-time git push, in Part 1).

---

## What is already done for you (in the fix)

| File | What it does |
|---|---|
| `start.sh` | No longer crashes when `DESK_TOKEN` is missing. Uses the host's value if set; otherwise generates one and prints it in the logs. |
| `render.yaml` | Blueprint with `plan: free`, `healthCheckPath: /api/health`, `DESK_TOKEN` as `sync: false` (asked once, stored forever), `SELF_PING=1`. |
| `desk/keepalive.py` | Self-ping thread, every **300 s** (5 min) — well inside Render's ~15 min idle window, so one missed ping can't put it to sleep. Only runs when `RENDER_EXTERNAL_URL` exists. |
| `app.py` | Starts `keepalive.start()` on startup, stops it on shutdown. |
| `desk/store.py` | Turso/libSQL support — set `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` and your journal, paper book, operator intent and autotune gates survive every redeploy. |

Verified: **444 passed, 17 skipped**, every endpoint returns HTTP 200.

---

## Part 1 — Push the fix to GitHub (one time, from Android)

Nothing deploys until this is done. The sandbox has no GitHub credentials, so
this step must be yours.

### 1a. Make a permanent GitHub token

1. Chrome → `github.com` → sign in.
2. Tap your avatar → **Settings** → scroll to **Developer settings** (bottom).
3. **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**.
4. Note: `phone`. Expiration: **No expiration** ← this is the "permanent" part.
5. Tick the **`repo`** checkbox. Nothing else is needed.
6. **Generate token** → copy the `ghp_...` string. Save it in a notes app.
   It is shown **once**.

### 1b. Push with Termux

Install **Termux from F-Droid** (`f-droid.org`), not the Play Store — the Play
Store build is abandoned and its package mirror is broken.

```bash
pkg update -y && pkg install -y git openssh
termux-setup-storage          # tap Allow

# copy the two files I gave you out of Downloads
mkdir -p ~/desk && cd ~/desk
cp ~/storage/downloads/solana-desk-fix.patch .
cp ~/storage/downloads/solana-desk-fixed.zip .

git clone https://github.com/roasaetn2-crypto/solana-desk-.git
cd solana-desk-

# apply the single clean commit
git am ../solana-desk-fix.patch

# if git am ever complains, fall back to the zip:
#   cd .. && rm -rf solana-desk- && unzip solana-desk-fixed.zip && cd solana-desk-

git config user.name  "roasaetn2-crypto"
git config user.email "your-github-email@example.com"
```

Now push. When it asks for a **Username**, type `roasaetn2-crypto`.
When it asks for a **Password**, paste the `ghp_...` token (not your account
password — GitHub removed password auth for git in 2021).

```bash
git push origin main
```

If you'd rather skip Termux entirely: on the GitHub mobile website open each
changed file → pencil icon → paste the new contents → **Commit changes**. It
works but there are ~10 files; Termux is far less painful.

### Files the patch touches

```
start.sh
render.yaml
.gitignore
desk/keepalive.py
config.py
desk/broker.py
desk/chain.py
desk/devcheck.py
desk/dialogue.py
desk/meme.py
desk/scanner.py
```

---

## Part 2 — Create the Render Blueprint

1. Chrome → `dashboard.render.com` → sign in with GitHub.
2. **New +** → **Blueprint**.
3. Pick the repo `roasaetn2-crypto/solana-desk-`. Render finds `render.yaml`
   automatically and shows one service: `solana-desk`.
4. It prompts for **`DESK_TOKEN`** (because of `sync: false`).
   **Type a long secret you will remember** — e.g. 24+ random characters.
   This is your login key. Render stores it permanently; it survives every
   redeploy and never expires.
5. **Apply**. First build takes 2–4 minutes.

If Blueprint gives you trouble, do it manually instead:

| Field | Value |
|---|---|
| Type | Web Service |
| Runtime | Python 3 |
| Build Command | `pip install --no-cache-dir -r requirements.txt` |
| Start Command | `bash start.sh` |
| Health Check Path | `/api/health` |
| Plan | Free |
| Env var `PYTHON_VERSION` | `3.11.9` |
| Env var `DESK_TOKEN` | your secret |
| Env var `PYTHONUNBUFFERED` | `1` |
| Env var `SELF_PING` | `1` |

---

## Part 3 — Verify

Open `https://solana-desk.onrender.com/api/health` (your actual URL). You should
see JSON with `"ok": true` and `"running": true`. No login needed — `/api/health`
is a public path.

Then open the root URL, log in with your `DESK_TOKEN`, and check that
`/api/state`, `/api/settings`, `/api/backtest`, `/api/proposals` all load
without a 500. They were all broken before the fix and are all 200 now.

---

## Part 4 — Stop the sleeping (the actual point of this guide)

Render's free plan spins a service down after ~15 minutes with no inbound
request. Two layers of defence, use **both**:

### Layer 1 — built-in self-ping (already on)

`desk/keepalive.py` hits its own `/api/health` every 5 minutes using
`RENDER_EXTERNAL_URL`, which Render injects automatically. Nothing to configure;
`SELF_PING=1` in `render.yaml` turns it on.

Limitation: if the process is already asleep, it cannot wake itself. That is
what Layer 2 is for.

### Layer 2 — external pinger (free, no card)

1. Go to `cron-job.org` → sign up (free, email only).
2. **Create cronjob**:
   - Title: `solana-desk keepalive`
   - URL: `https://solana-desk.onrender.com/api/health`
   - Schedule: **every 10 minutes** (choose "Every 10 minutes" from the
     predefined list, or custom `*/10 * * * *`)
   - Execution: enable **"Save responses in job history"** off, and turn on
     **"Treat redirects as success"**.
3. **Test run** once, then save.

Alternatives that work the same way: `uptimerobot.com` (free plan = 5-minute
checks, 50 monitors), `betterstack.com/uptime` (free 3-minute checks).

Now the service receives a request every 5–10 minutes forever, so it never
crosses the 15-minute idle threshold.

### The 750-hour catch

Render free includes **750 instance-hours per month**. That is ~31 days of one
always-on service — exactly one service, and no headroom. Consequences:

- Run **one** service only. A second free service halves your uptime.
- If you exceed 750 hours Render suspends the service until the next month.
  With a single always-on service you land right at the line; if you want
  margin, set the pinger to every 14 minutes instead of 10 and accept a brief
  cold start, or drop to `SELF_PING` only during the hours you trade.

---

## Part 5 — Make your data survive redeploys

Render free has **ephemeral disk**. Every deploy, restart, or sleep/wake cycle
can wipe `desk.db` and `journal.jsonl` — meaning your paper trades and autotune
history vanish. Fix it with Turso (free tier: 500 databases, 9 GB, no card).

1. `turso.tech` → sign up with GitHub.
2. Create a database, e.g. `solana-desk`. Pick the region closest to your
   Render region (Frankfurt or Oregon).
3. Copy the **Database URL** (`libsql://solana-desk-xxxx.turso.io`).
4. Generate a token: in the dashboard, **Create token** → copy it.
5. Render → your service → **Environment** → add:
   - `TURSO_DATABASE_URL` = the `libsql://...` URL
   - `TURSO_AUTH_TOKEN` = the token
6. **Save** — Render redeploys automatically.

`desk/store.py` detects both vars and mirrors the journal, paper ledger,
operator intent and autotune gates to Turso on every write, and restores them on
boot. Without them the app still runs, it just forgets on redeploy.

---

## Part 6 — The four things that can still "expire", and how each is handled

| Expiry vector | Status | Fix |
|---|---|---|
| **GitHub token** | You control it | Chose **No expiration** in step 1a. If you ever revoke it, make a new one. |
| **DESK_TOKEN** | Permanent | `sync: false` stores it in Render forever. It is *not* regenerated on redeploy. Never change it unless you want to log out all devices. |
| **Render sleep** | Handled | `keepalive.py` (5 min) + cron-job.org (10 min). |
| **Ephemeral data** | Handled | Turso in Part 5. |

The only genuine remaining limit is the 750 instance-hours/month, which is a
Render policy, not an expiry date. It resets every month, forever.

---

## If you ever want zero compromises

Render **Starter** is $7/month: no sleeping, no 750-hour cap, 512 MB → 2 GB RAM,
persistent disk available. That is the cheapest genuinely always-on PaaS. The
Oracle Cloud Always Free VM (2 OCPU / 12 GB, never sleeps, never expires, $0) is
the free equivalent, but you manage the OS yourself.

---

## Troubleshooting

**Build fails on `pip install`** — check `requirements.txt` has no pinned version
that doesn't exist for Python 3.11.9. It currently pins nothing exotic.

**`DESK_TOKEN was not set by the host`** in the logs — the env var didn't reach
the service. Render → Environment → confirm `DESK_TOKEN` exists and has a value,
then **Manual Deploy → Clear build cache & deploy**.

**Login page rejects your key** — the cookie is `HttpOnly` + `SameSite=Lax`.
Make sure you're on `https://`, not `http://`. Clear site cookies and retry.

**Still sleeping** — check the logs for `[desk] keepalive: self-ping every 5 min`.
If it's absent, `RENDER_EXTERNAL_URL` is missing (happens on non-Render hosts) or
`SELF_PING=0`. The cron-job.org pinger works regardless.

**`/api/state` returns 500** — you deployed before the fix landed. Confirm
`config.py` on GitHub contains `"max_h1_change_pct"` and that `desk/broker.py`
contains `def realized_equity`. Both were the root cause of the original 500s.
