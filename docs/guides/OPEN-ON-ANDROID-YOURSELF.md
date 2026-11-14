# Opening Solana Desk on Android — no assistant, no account, no cloud

Everything below you do yourself. No AI, no GitHub, no sign-up. The only things
you need are the zip file you already have and a computer.

There are two honest ways. Pick one:

- **Way 1 — Phone on the same Wi-Fi as the computer.** Simplest. Works in 5 minutes. Only works at home.
- **Way 2 — A public link (tunnel).** Works on mobile data, from anywhere. Two windows must stay open.

---

## Way 1 — Same Wi-Fi (do this first, always)

Do this once and confirm it works before you attempt Way 2. Way 2 is built on
top of it.

### 1. Unzip
Put `solana-desk.zip` on your Desktop or in Documents and extract it.
You get a folder `solana-desk` containing `run.bat`, `run.sh`, `app.py`, `web/`.

Do not unzip inside the Windows Downloads *preview* window — it extracts to a
temporary folder and vanishes.

### 2. Install Python (only if you don't have it)
Go to python.org/downloads and install the current version.

**On Windows, tick "Add Python to PATH"** on the first installer screen. If you
miss it you will get `python: command not found` forever. Re-run the installer,
choose Modify, and add it.

### 3. Start it
| Computer | Do this |
|---|---|
| Windows | double-click `run.bat` |
| Mac / Linux | Terminal, `cd` into the folder, then `./run.sh` |

If Mac says `Permission denied: ./run.sh`, run `chmod +x run.sh` once, then again.

A black window opens and prints:

```
==============================================================
  Solana Desk is LOCKED

  Open:  http://localhost:8000
  Key:   kQ7f2mXp9vLbT4nR8sW
==============================================================
```

**Write the Key down.** **Leave the window open** — closing it stops the desk.
First run installs four packages, about 30 seconds. Every run after is instant.

### 4. Find the computer's number on the network
The phone cannot use `localhost` — that means "this device".

| Computer | Command | Look for |
|---|---|---|
| Windows | `ipconfig` | IPv4 Address under the Wi-Fi adapter |
| Mac | `ipconfig getifaddr en0` | the number it prints |
| Linux | `hostname -I` | the first number |

It looks like `192.168.1.42`. Ignore `127.0.0.1`.

### 5. Open it on the phone
Phone must be on the **same Wi-Fi**, not mobile data. In Chrome type:

```
http://192.168.1.42:8000
```

(use your own number). You get the lock screen.

### 6. Pair the phone
Type the Key, tap **Pair this device**. That phone now opens with no key for a
year. Every other device still gets the lock screen.

### 7. Make it an app icon
Chrome **⋮** menu → **Add to Home screen** → **Install**.

### 8. Press Start agent
On the dashboard, tap **Start agent**. It cycles on its own; the page refreshes
itself every two seconds.

**If the phone cannot reach it:** the computer's firewall is blocking port 8000.
Windows: Security → Firewall → Allow an app → allow Python on **Private**
networks. Also check you are not on a guest Wi-Fi — guest networks isolate
devices from each other.

---

## Way 2 — A public link you can open anywhere

This makes the desk reachable on mobile data. The desk still runs on the
computer; a "tunnel" gives it a public `https://` address that forwards to it.

**Both the desk window and the tunnel window must stay open.** Close either and
the phone loses the desk. If the computer sleeps, it dies.

### 1. Confirm Way 1 works locally
`http://localhost:8000` must load on the computer itself. Do not skip this. A
tunnel only forwards something that is already listening.

### 2. Open a SECOND terminal window
Leave the desk running in the first one.

### 3. Start the tunnel — pick one

**Cloudflared (recommended — no account, no password page):**

```bash
cloudflared tunnel --url http://localhost:8000
```

If it is not installed:
- Windows: `winget install Cloudflare.cloudflared`
- Mac: `brew install cloudflared`
- Linux: download the package from Cloudflare's releases page

It prints a banner containing a link like:

```
https://random-words-here.trycloudflare.com
```

**Localtunnel (needs Node.js):**

```bash
npx localtunnel --port 8000
```

It prints `your url is: https://something.loca.lt`.

Localtunnel shows a "Friendly reminder" page asking for a **Tunnel Password**
before the desk loads. That password is your public IP address — open
`https://loca.lt/mytunnelpassword` on the same computer, copy the number, paste
it, click Continue. Cloudflared has no such page, which is why it is preferred.

### 4. Open the link on the phone
On mobile data or any Wi-Fi. Chrome → paste the `https://…` link → type the
**Key** → tap **Pair this device**.

### 5. Install it as an app
Chrome **⋮** → **Add to Home screen** → **Install**. Because the link is HTTPS,
Android installs it properly: full screen, own icon, no address bar. This is the
one real advantage over Way 1.

### 6. Press Start agent.

### 7. Keep it alive
- Do not close either window.
- Stop the computer sleeping: Windows Settings → System → Power → Screen and
  sleep → Never. On Mac, start the desk with `caffeinate -i ./run.sh`.
- **The free tunnel link changes every time you restart the tunnel.** When it
  changes, the phone's saved pairing no longer applies, so you pair again on the
  new link. Nothing is broken; it is how free tunnels work.

---

## Security — read this before using Way 2

A public link with no key is a public trading desk. Anyone who finds the URL can
press Start, Reset and Kill.

- **Never run `./run.sh --open`** on a tunnel. That flag removes the lock.
- Set your own key instead of the random one: `./run.sh MyLongKeyHere`
  (Windows: `set DESK_TOKEN=MyLongKeyHere` then `run.bat`).
- See who is paired at `https://your-link/devices` and revoke anything you do not
  recognise. It takes effect immediately.
- Pairing remembers a **browser**, not a person. Someone holding your unlocked
  phone is in, same as any logged-in app. Use your phone's screen lock.
- The desk starts in **paper** mode every time. Real money only moves if you
  install `requirements-live.txt`, export `SOLANA_PRIVATE_KEY`, and arm live mode
  by typing the confirmation phrase. If you never do that, no code path can
  touch a wallet.

---

## Troubleshooting

| What you see | What it means | Fix |
|---|---|---|
| `python: command not found` | Python not on PATH | re-run installer → Modify → Add to PATH |
| `Permission denied: ./run.sh` | not executable | `chmod +x run.sh` |
| `Address already in use` | port 8000 is taken | `PORT=8080 ./run.sh`, then use `:8080` in the URL |
| Phone: "site can't be reached" (Way 1) | different network, or firewall | same Wi-Fi, non-guest; allow port 8000 through the firewall |
| Laptop works, phone doesn't, same Wi-Fi | firewall prompt was dismissed | Windows Security → Firewall → Allow an app → Python (Private) |
| Tunnel link gives 404 | desk not running, or wrong port | confirm `localhost:8000` loads; tunnel the same port |
| "Friendly reminder" password page | localtunnel interstitial | IP from `https://loca.lt/mytunnelpassword`, or use cloudflared |
| Link worked, now dead | tunnel closed or computer slept | restart the tunnel; the URL will be new |
| Lock screen on every visit | new tunnel hostname, so the cookie doesn't apply | pair again, or use a fixed domain (VPS) |
| Page loads, all panels empty | agent not started, or no internet for data feeds | tap Start agent; check the computer has internet |
| Worked yesterday, not today (Way 1) | the computer's IP changed | repeat the IP step; routers reassign numbers |

---

## Stopping it

`Ctrl+C` in the desk window, and `Ctrl+C` in the tunnel window. Nothing runs in
the background — no daemon, no service. Your state is saved in the folder:

- `paper_ledger.json` — paper cash and positions
- `journal.jsonl` — every decision the desk made
- `desk_devices.json` — the phones you paired

Delete those files to start completely fresh.

---

## If you ever want a permanent address

A $4–6/month VPS is the only version that survives your computer being off, and
the only one with a link that never changes. Copy the folder up, run `./run.sh`,
put it behind Caddy for automatic HTTPS, keep `DESK_TOKEN` set. Then the phone
pairs once and never asks again.
