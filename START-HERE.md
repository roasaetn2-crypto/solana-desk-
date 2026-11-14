# START HERE

You do **not** need GitHub, an account, a cloud service, or me.
Unzip the folder, run one command, open the link. That's it.

---

## The 60-second version (any laptop)

1. **Unzip** `solana-desk.zip` somewhere normal, like your Desktop.
2. **Install Python** if you don't have it — [python.org/downloads](https://www.python.org/downloads/).
   On Windows, tick **"Add Python to PATH"** during install. This is the only install step.
3. **Start it:**

   | Your machine | What to do |
   |---|---|
   | **Windows** | double-click **`run.bat`** |
   | **Mac / Linux** | open Terminal, `cd` into the folder, then `./run.sh` |

4. The terminal prints something like:

   ```
   ==============================================================
     Solana Desk is LOCKED

     Open:  http://localhost:8000
     Key:   kQ7f2mXp9vLbT4nR8sW
   ==============================================================
   ```

5. Open **http://localhost:8000** in your browser. Type the key once.

That's running. Press **Start agent** and it begins cycling.

The first run installs four Python packages and takes about 30 seconds. Every
run after that is instant.

---

## Put it on your phone (same Wi-Fi)

1. Start the desk on your laptop as above.
2. Find your laptop's local IP: `ipconfig` (Windows) or `ifconfig | grep inet` (Mac/Linux).
   It looks like `192.168.1.42`.
3. On your phone, open `http://192.168.1.42:8000`.
4. Type the key, then tap **Pair this device**.

From then on **that phone opens with no key at all** — it's remembered for a year.
Every other device still needs the key. You can revoke a phone any time at
`http://192.168.1.42:8000/devices`.

You can also use **Add to Home Screen** and it behaves like a real app (full
screen, own icon, no browser bar).

> **Full step-by-step, with screenshots-level detail and a troubleshooting
> table: [ANDROID.md](docs/guides/ANDROID.md).** That file also covers running the desk on
> the phone itself with Termux, and reaching it from outside your Wi-Fi.

> If the phone can't reach it, your laptop firewall is blocking port 8000.
> Allow it, or use the tunnel option below.

---

## Run it on the phone itself (no laptop)

This works on Android via Termux. It is slower and the battery cost is real,
but it means the desk lives in your pocket.

```bash
# in Termux, from F-Droid (not the Play Store version)
pkg update && pkg install python git -y
cd ~ && unzip solana-desk.zip && cd solana-desk
chmod +x run.sh && ./run.sh
```

Then open `http://localhost:8000` in the phone's browser.

---

## Reach it from anywhere (not just your Wi-Fi)

Two honest options:

**A tunnel** — no account needed for a temporary one:

```bash
# in a second terminal, while the desk is running
npx localtunnel --port 8000
# or: cloudflared tunnel --url http://localhost:8000
```

You get a public `https://...` link. Keep the key on. Anyone with the link and
no key gets the login page.

**A small VPS** — $4–6/month, always on, survives your laptop closing. Copy the
folder over, run `./run.sh`, and put it behind Caddy or nginx for HTTPS. This is
the only option that is genuinely "always available".

---

## Do I need GitHub?

**No.** GitHub is for storing and sharing *source code*. You already have the
source — it's in the zip. Nothing in this desk calls GitHub, and it does not
need to be "deployed" anywhere to work.

GitHub becomes useful only if you want one of these:

- **Version history** — so you can undo a change you made.
- **Editing on another machine** — clone, edit, push.
- **Free hosting of the dashboard** — GitHub Pages. But note: Pages only serves
  static files. This desk has a Python backend that does the scanning, so Pages
  alone cannot run it. You'd need a VPS or a container host.

So: if you just want to *use* the desk, skip GitHub entirely.

---

## What you need for it to be useful

None of these are required to start. The desk runs and trades paper with none of them.

| Thing | Why | Cost |
|---|---|---|
| **A brain** (Ollama / Groq / Gemini) | without one it uses pure rules | free |
| **Helius key** | real holder counts instead of a top-20 proxy | free tier |
| `DESK_TOKEN` | the lock. `run.sh` makes one for you | free |

```bash
# optional, in the same terminal before ./run.sh
export GROQ_API_KEY=gsk_...        # free brain
export HELIUS_API_KEY=...          # free holder counts
```

See `README.md` for the full picture.

---

## Trading real money (optional, off by default)

The desk starts in **paper** every time you launch it. Nothing you do in paper
can move real money. To go live you need three things:

```bash
pip install -r requirements-live.txt          # solders + base58, for signing
export SOLANA_PRIVATE_KEY="your base58 key"   # never put this in a file
./run.sh
```

Then in the dashboard: **Execution mode → Arm live…** and type the phrase
exactly as shown. The wallet address appears so you can confirm it is yours.
Restart the desk and you are back in paper.

The caps are hard-coded in `config.py` and checked twice, once when the ticket
is sized and again right before the transaction is signed:

- **$25** maximum on any single live ticket
- **$100** maximum live money at risk at once
- **$3** daily loss stop (3% of the $100 cap)
- **3%** maximum slippage, or the route is refused
- the **Kill** button blocks every new live buy immediately

Live mode refuses demo tokens entirely, so the synthetic feed can never reach a
real swap. Cash is USDC, so your P&L is in dollars and does not drift with SOL.

**This can lose real money.** Memecoins are adversarial: slippage, MEV,
honeypots and rugs are all real, and the desk gets fooled sometimes. Start with
a ticket you would not miss. If that is not acceptable, stay in paper — the desk
is a research tool first.

## Stopping it

`Ctrl+C` in the terminal. Nothing keeps running in the background, no daemon,
no service. Your state is saved in the folder, so restarting keeps your history:

- `paper_ledger.json` — paper cash and positions
- `live_ledger.json` — live P&L (only created once you arm live mode)
- `journal.jsonl` — every decision the desk made
- `desk_devices.json` — the phones you've paired

To wipe everything and start fresh: delete those files.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| `python: command not found` | install Python, tick "Add to PATH" on Windows |
| `Permission denied: ./run.sh` | `chmod +x run.sh` |
| `Address already in use` | something else is on 8000 — `PORT=8080 ./run.sh` |
| Page loads but says "access: key" forever | you're on a new browser; type the key again |
| Phone can't connect | firewall on port 8000, or use the tunnel |
| `pip install` fails on Debian/Ubuntu | `sudo apt install python3-venv`, then use a venv |

The desk boots in **paper** mode every single time. Live trading is off until you
paste your own key into the environment, type the confirmation phrase, and arm it
yourself. If you never do that, there is no wallet and no code path that can move
real money. That is deliberate.

For the full Android walkthrough — three routes, the Termux rust gotcha, and a
troubleshooting table — see **[ANDROID.md](docs/guides/ANDROID.md)**.
