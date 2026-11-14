# Run the Solana Desk 24/7 on an Android phone (Termux) — $0, no hour cap

Render's free plan caps you at **750 instance-hours/month** and sleeps after 15
minutes idle. A phone running Termux has **no hour cap and never sleeps** — the
only costs are electricity and keeping the phone on Wi-Fi. This guide turns an
old Android phone into a permanent, always-on desk host.

**What you get:** the desk runs on the phone, auto-restarts if it crashes,
survives reboots, and you can open the dashboard from anywhere.

> **Read this first.** A phone is not a server. It will get warm, it wants to
> sleep, and Android will kill background apps unless you tell it not to. Every
> step below exists because skipping it is how people's desks die silently.
> Do all of them.

---

## What you need

- An Android phone (Android 8+), charged and on Wi-Fi, ideally left plugged in.
- ~1 GB free storage and a decent Wi-Fi signal.
- 20–30 minutes for the first install (compiling `pydantic-core` on a phone is slow).

---

## Part 1 — Install the right Termux

**Do not use the Play Store version.** It is abandoned and `pkg install` fails
with 404s.

1. Open **f-droid.org** in the phone browser and install the **F-Droid** app
   (allow "install unknown apps" when prompted).
2. In F-Droid, search **Termux** → install.
3. Still in F-Droid, search **Termux:Boot** → install. (This is what makes the
   desk start after a reboot. It is a separate app, from the same source.)
4. Open **Termux:Boot once**, then close it — that registers its boot hook.

---

## Part 2 — Give Termux storage access and install the toolchain

Open **Termux** and run:

```bash
termux-setup-storage      # tap Allow on the Android popup
pkg update -y && pkg upgrade -y
pkg install -y python rust binutils clang make pkg-config git openssh unzip
```

`rust`, `clang`, `binutils` and `make` are **not optional**: `pydantic-core` is a
compiled Rust extension with no prebuilt Android wheel, so pip builds it on your
phone. Without the toolchain you get `error: can't find crate for 'core'`, which
looks like a broken package but is a missing compiler.

This downloads a few hundred MB. Use Wi-Fi.

---

## Part 3 — Get the code onto the phone

The repo is on GitHub, so clone it directly (no zip, no copy-paste):

```bash
cd ~
git clone https://github.com/roasaetn2-crypto/solana-desk-.git solana-desk
cd solana-desk
```

If the repo is private, GitHub will ask for a username and password: username is
your GitHub name, **password is a Personal Access Token** (not your account
password — GitHub removed password auth in 2021). Make a token at
GitHub → Settings → Developer settings → Personal access tokens.

---

## Part 4 — Install the Python dependencies (the slow part)

```bash
cd ~/solana-desk
pip install --upgrade pip
CARGO_BUILD_JOBS=1 pip install -r requirements.txt
```

`CARGO_BUILD_JOBS=1` keeps the Rust build to one core so a mid-range phone does
not run out of RAM and get killed. Expect **5–15 minutes** and a warm phone.
That is normal. Do not close Termux.

When it finishes, check it worked:

```bash
python -c "import fastapi, uvicorn, pydantic, requests; print('deps ok')"
```

---

## Part 5 — Start it once by hand (sanity check)

```bash
cd ~/solana-desk
./run.sh MyDeskKey123
```

You should see:

```
==============================================================
  Solana Desk is LOCKED

  Open:  http://localhost:8000
  Key:   MyDeskKey123
==============================================================
INFO:     Uvicorn running on http://0.0.0.0:8000
```

Open Chrome on the phone → `http://localhost:8000` → type the key → **Pair this
device** → name it `Pixel`. Then **⋮ → Add to Home screen → Install** for a
full-screen icon.

Tap **Start agent** on the dashboard to begin the paper loop.

Stop it for now with **Ctrl+C** in Termux — we are about to run it properly.

---

## Part 6 — Run it 24/7 with the supervisor (auto-restart)

The repo ships a supervisor that keeps the desk alive: it sets a wake lock,
restarts uvicorn if it ever exits, logs to `desk.log`, and reuses one fixed
login key.

```bash
chmod +x ~/solana-desk/termux/start-desk.sh
bash ~/solana-desk/termux/start-desk.sh
```

It runs in the foreground and prints nothing (it logs to `desk.log`). **Leave
Termux open.** To check it is alive:

```bash
tail -n 20 ~/solana-desk/desk.log
curl -s http://localhost:8000/api/health
```

Your permanent login key is stored in `~/.desk_key`. See it with:

```bash
cat ~/.desk_key
```

---

## Part 7 — Stop Android from killing it (the step everyone skips)

If you skip this, the desk dies ~10 minutes after the screen locks.

**In Termux:**

```bash
termux-wake-lock
```

(The supervisor already does this, but run it once by hand to be sure.)

**In Android settings:**

- **Settings → Apps → Termux → Battery → Unrestricted**
- **Settings → Apps → Termux:Boot → Battery → Unrestricted**
- Samsung only: **Settings → Battery → Background usage limits** → make sure
  Termux is **not** in "Sleeping apps" or "Deep sleeping apps".
- If your phone has a separate "App battery usage" screen, set Termux to
  **Unrestricted / Don't optimise** there too.
- **Keep the Termux notification visible.** That notification is what keeps the
  process alive. Do not swipe it away.

---

## Part 8 — Auto-start after a reboot

So the desk comes back on its own after the phone restarts or the battery dies:

```bash
bash ~/solana-desk/termux/install-boot.sh
```

That one command creates `~/.termux/boot`, installs the hook, makes it
executable and checks that it points at `start-desk.sh`. It also tells you if
Termux:Boot is missing. Remove it later with
`bash ~/solana-desk/termux/install-boot.sh --remove`.

Reboot the phone to test. After ~30 seconds the desk should be up again —
verify with `curl -s http://localhost:8000/api/health`.

> **If the desk is not running**, do not guess. Run:
>
> ```bash
> bash ~/solana-desk/termux/verify-update.sh
> ```
>
> It separates three different problems that look the same from the outside:
> the code is old (exit 1), the code is fine but the server is stopped
> (exit 2), or everything is healthy (exit 0). When the server is stopped it
> reads `desk.log` and prints the actual crash line, e.g.
> `ModuleNotFoundError: No module named 'fastapi'`.
>
> ```bash
> bash termux/verify-update.sh --start   # check, then start it
> bash termux/verify-update.sh --log     # show the server log
> ```

> Termux:Boot only fires if the Termux:Boot app has been opened at least once
> (Part 1, step 4) and is not battery-restricted (Part 7).

---

## Part 9 — Open it from anywhere (not just the phone)

`localhost` only works on the phone itself. To reach the desk from a laptop or
another phone, pick **one** of these. **Tailscale is the safest and easiest.**

### Option A — Tailscale (recommended, free, private)

Tailscale puts the phone and your other devices on a private network, so the
desk is reachable by its Tailscale IP from anywhere, with no public exposure.

```bash
pkg install -y tailscale
tailscaled > /dev/null 2>&1 &      # start the daemon
tailscale up                        # open the printed URL and log in
tailscale ip -4                     # note the 100.x.y.z address
```

Then on your laptop (also running Tailscale, logged into the same account):
open `http://100.x.y.z:8000`. Only your own devices can reach it. This is the
best option for a trading desk — nobody else can even see it.

To make `tailscaled` survive reboots, add it to the boot hook too:

```bash
cat >> ~/.termux/boot/10-desk.sh <<'EOF'
nohup tailscaled >/dev/null 2>&1 &
EOF
```

### Option B — Cloudflare Tunnel (public URL, free, no port forwarding)

Only if you want a public `https://…trycloudflare.com` link. Anyone with the URL
reaches the login page; the `DESK_TOKEN` still guards it.

```bash
pkg install -y cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a public URL. Keep the `DESK_TOKEN` set — never run `./run.sh --open`
on a publicly reachable desk.

> Do **not** port-forward your router to the phone. That exposes the desk to the
> whole internet with no TLS and no protection.

---

## Part 10 — Keep the data (it lives on the phone)

Unlike Render's ephemeral disk, the phone's storage persists. Your state is in
the repo folder:

- `paper_ledger.json` — paper book and P&L
- `journal.jsonl` — every decision, including rejections
- `desk_devices.json` — paired browsers
- `autotune.json` — self-tune history

**Back them up** (they are the only copy). To your Downloads folder:

```bash
cd ~/solana-desk
cp paper_ledger.json journal.jsonl desk_devices.json ~/storage/downloads/ 2>/dev/null
```

Do this weekly, or after a good trading day. A phone can be lost, dropped, or
wiped — the desk's history should not go with it.

---

## Part 11 — Heat, battery, and the honest limits

- **Leave it plugged in.** A phone running a Python loop 24/7 will drain a
  battery in a few hours otherwise. Charging while running is fine.
- **Heat is the real enemy.** Do not leave it under a pillow or in the sun. Put
  it on a hard surface with airflow. If it gets hot, Android throttles the CPU
  and cycles slow — the desk still works, just slower.
- **Battery health:** keeping a phone at 100% and warm for months degrades the
  battery. If you care, some phones have "charge limit 80%" or "protect battery"
  settings — use them. An old phone you don't care about is ideal.
- **Wi-Fi drops.** If the Wi-Fi drops, the desk keeps running locally but cannot
  fetch pools. It recovers on its own when the network returns; the scanner has
  cooldowns and retries built in.
- **Do Not Disturb.** Turn on DND so notifications don't wake the screen
  constantly.

---

## Part 12 — Updating the code later

When you push changes to GitHub (or I do), pull them on the phone:

```bash
cd ~/solana-desk
pkill -f start-desk.sh ; pkill -f uvicorn     # stop the desk
git pull
CARGO_BUILD_JOBS=1 pip install -r requirements.txt   # only if deps changed
bash termux/start-desk.sh                      # start again
```

If `git pull` complains about local changes, and you have not edited files on
the phone, discard them: `git checkout -- . && git pull`.

---

## Part 13 — Handy commands

| Task | Command |
|---|---|
| Is it running? | `curl -s http://localhost:8000/api/health` |
| See the log | `tail -n 50 ~/solana-desk/desk.log` |
| Follow the log live | `tail -f ~/solana-desk/desk.log` |
| Show login key | `cat ~/.desk_key` |
| Stop everything | `pkill -f start-desk.sh; pkill -f uvicorn` |
| Restart | `bash ~/solana-desk/termux/start-desk.sh` |
| Check wake lock | `termux-wake-lock` (re-apply) |
| Tailscale IP | `tailscale ip -4` |

---

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| `pkg: command not found` / 404s | Play Store Termux | Uninstall, install from F-Droid |
| `error: can't find crate for 'core'` | no Rust toolchain | `pkg install rust binutils clang make` |
| pip build killed / OOM | too much RAM used | `CARGO_BUILD_JOBS=1 pip install -r requirements.txt` |
| `Permission denied` on a script | not executable | `chmod +x <script>` |
| `Address already in use` | port 8000 busy | `PORT=8080 bash termux/start-desk.sh`, open `:8080` |
| Dies when screen locks | no wake lock / battery restricted | Part 7 — wake lock + Unrestricted |
| Doesn't start after reboot | Termux:Boot not opened / restricted | Open Termux:Boot once; set it Unrestricted |
| Browser "can't be reached" | desk not running | `tail desk.log`; restart with the supervisor |
| Locked out every time | different browser or cleared cookies | Pair again; don't use incognito |
| Page loads, panels empty | agent not started | Tap **Start agent** |
| Scanner shows 0 pools | Wi-Fi down or rate-limited | Check Wi-Fi; wait for cooldown |

---

## The bottom line

A phone in Termux is the only genuinely **unlimited, $0** host for this desk: no
750-hour cap, no 15-minute sleep, no monthly bill. The trade is that you own the
hardware — keep it charged, cool, and battery-unrestricted, back up your data,
and it will run for as long as the phone lives.

If you would rather not babysit a phone, **Render Starter ($7/mo)** removes the
cap with zero maintenance, and **Oracle Cloud Always Free** gives unlimited hours
on a real VM for $0 if you are willing to manage a Linux box.
