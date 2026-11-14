# Opening the desk on Android — step by step, no assistant needed

You do not need me, GitHub, an account, or a cloud service for any of this.
Pick one of the three routes below. **Route A is the one most people want** and
it takes about five minutes.

| Route | Where the desk runs | Good for | Catch |
|---|---|---|---|
| **A. Laptop runs it, phone opens it** | your computer | almost everyone | laptop must be on |
| **B. Termux — phone runs it** | the phone itself | no laptop, pocket desk | slow install, battery |
| **C. Tunnel or VPS** | laptop or server, reachable anywhere | leaving the house | extra step / small cost |

---

# Route A — laptop runs it, phone opens it (recommended)

The desk is a small Python program with a web page on top. The program runs on
the laptop; the phone is just a screen for it. Both need to be on the **same
Wi-Fi**.

### 1. Unzip the folder on the laptop

Put `solana-desk.zip` somewhere normal — Desktop, Documents, anywhere you'll
find it again. Right-click → **Extract All** (Windows) or double-click (Mac).
You should end up with a folder called `solana-desk` containing `run.bat`,
`run.sh`, `app.py`, and a `web` folder.

> Don't unzip it *inside* the Downloads preview window on Windows — it runs in a
> temporary folder and disappears. Extract to Desktop or Documents.

### 2. Install Python (once, only if you don't have it)

Go to [python.org/downloads](https://www.python.org/downloads/) and install the
current version.

**On Windows there is one box you must tick:** on the very first installer
screen, at the bottom, tick **"Add Python to PATH"**. If you miss it, the desk
will say `python: command not found` forever and you'll be confused. You can
re-run the installer and choose "Modify" to fix it later.

Mac: the python.org installer is fine, or `brew install python`.

### 3. Start it

| Your laptop | What to do |
|---|---|
| **Windows** | double-click **`run.bat`** |
| **Mac / Linux** | open Terminal, `cd` into the folder, then `./run.sh` |

A black terminal window opens and prints something like:

```
==============================================================
  Solana Desk is LOCKED

  Open:  http://localhost:8000
  Key:   kQ7f2mXp9vLbT4nR8sW
==============================================================
```

**Leave that window open.** Closing it stops the desk. The first run installs
four Python packages and takes ~30 seconds; after that it starts instantly.

If Mac says `Permission denied: ./run.sh`, run `chmod +x run.sh` once, then try
again.

### 4. Find the laptop's address on the Wi-Fi

The phone can't use `localhost` — that means "this device". You need the
laptop's number on the network, which looks like `192.168.1.42`.

**Windows** — open Command Prompt and type:

```
ipconfig
```

Look under your Wi-Fi adapter for **IPv4 Address**. Ignore `127.0.0.1`.

**Mac** — in Terminal:

```
ipconfig getifaddr en0
```

**Linux**:

```
hostname -I
```

Write that number down. Call it `YOUR-IP` below.

### 5. Open it on the phone

Make sure the phone is on the **same Wi-Fi** as the laptop (not mobile data).
In Chrome, type:

```
http://YOUR-IP:8000
```

So if your number was `192.168.1.42`, you type `http://192.168.1.42:8000`.

You'll see the lock screen. Type the key from step 3.

### 6. Pair the phone so it never asks again

On the lock screen, after typing the key, tap **Pair this device**.

That's the whole trick. The desk now remembers *this* browser for a year with a
cookie. Next time you open the link, it goes straight in — no key. Any other
phone or laptop that opens the same address still gets the lock screen.

To check what's paired, or to revoke a device you no longer have, open:

```
http://YOUR-IP:8000/devices
```

Revoking takes effect immediately.

### 7. Make it look like a real app

In Chrome on Android: tap the **⋮** menu → **Add to Home screen** → **Install**.

You now get an icon on your home screen that opens full-screen with no address
bar, no tabs, no browser chrome. It's a PWA, so it behaves like an installed
app while still being the same page.

### 8. Press Start

On the dashboard, tap **Start agent**. It begins cycling on its own — scanning,
reading chain facts, investigating the dev wallet, scoring the meme, sizing,
and journalling. You don't have to babysit it. The page polls every two seconds
and updates itself.

---

## Route A troubleshooting

| What you see | What it means | Fix |
|---|---|---|
| Phone shows "site can't be reached" | phone and laptop aren't on the same network, or a firewall is blocking | confirm both are on the same Wi-Fi (not guest network — guest networks often isolate devices), then allow port 8000 through the laptop firewall |
| Laptop works, phone doesn't, same Wi-Fi | Windows Firewall prompt was dismissed | Windows Security → Firewall → Allow an app → allow Python (Private networks) |
| `python: command not found` | Python isn't on PATH | re-run the Python installer → Modify → tick "Add to PATH" |
| `Address already in use` | something else is on port 8000 | `PORT=8080 ./run.sh` (Windows: `set PORT=8080` then `run.bat`), then use `:8080` in the URL |
| Lock screen reappears every visit | cookie was cleared, or you're in a private/incognito tab | pair again in a normal tab; don't use incognito |
| Page loads but every panel is empty | agent hasn't been started, or there's no network for the data feeds | tap **Start agent**; check the laptop has internet |
| It worked yesterday, not today | the laptop's IP changed (routers reassign them) | run step 4 again — the number moved |

---

# Route B — run it on the phone itself with Termux

No laptop needed. The desk lives in your pocket. It is slower, the install is
longer, and Android will try to kill it in the background unless you tell it not
to. Read all of this before starting.

### 1. Install Termux from F-Droid, not the Play Store

This matters. The Play Store version is abandoned and its package repo is
broken; `pkg install` will fail with 404s.

- Install **F-Droid**: [f-droid.org](https://f-droid.org)
- In F-Droid, search **Termux** and install it.

### 2. Give Termux access to your files

Open Termux and run:

```bash
termux-setup-storage
```

Android will ask for permission. Allow it. This creates `~/storage/downloads`,
which is where your zip probably is.

### 3. Install Python **and a Rust toolchain**

```bash
pkg update && pkg upgrade -y
pkg install -y python rust binutils unzip
```

**Why rust?** Two of the desk's dependencies — `pydantic-core` and `solders` —
are compiled Rust extensions. There are no prebuilt Android wheels for them, so
pip has to compile them from source on your phone. Without rust you get:

```
error: can't find crate for `core`
```

which looks like a broken package but is really just a missing compiler. With
`rust` and `binutils` installed it compiles — slowly, several minutes, and the
phone gets warm. That's normal.

### 4. Unzip and start

```bash
cd ~
unzip ~/storage/downloads/solana-desk.zip
cd solana-desk
chmod +x run.sh
./run.sh
```

First run is the long one — it's compiling. Give it 5–15 minutes on a mid-range
phone. When it finishes you'll see the same locked banner with a key.

### 5. Open it

In the phone's browser: `http://localhost:8000`. Type the key, tap **Pair this
device**, then **Add to Home screen** as in Route A.

### 6. Stop Android from killing it

Two things, both necessary:

```bash
termux-wake-lock
```

This holds a wake lock so the CPU doesn't sleep the process. And in Android
settings: **Apps → Termux → Battery → Unrestricted**. On Samsung also check
**Settings → Battery → Background usage limits** and make sure Termux isn't in
"Sleeping apps".

Without both of these the desk dies silently about ten minutes after you lock
the screen, and you'll think it crashed.

### 7. Real-money signing (optional)

Live mode needs `solders`, which is one of the Rust packages from step 3 — so if
you got through the install, live signing already works. Verify with:

```bash
pip install -r requirements-live.txt
```

If that succeeds, the startup banner says `Live signing: available`. If it says
`unavailable (paper only)`, you're missing rust — go back to step 3.

---

# Route C — reach it when you're not at home

Route A only works on your own Wi-Fi. If you want the desk on your phone while
you're out, you need the laptop reachable from the internet. Two honest options.

### C1. A tunnel (free, temporary)

Leave the desk running on the laptop, open a **second** terminal, and run:

```bash
npx localtunnel --port 8000
```

It prints a public `https://something.loca.lt` link. Open that on the phone.
The link dies when you close the terminal, and it changes every time you start
it. Keep the key on — anyone who finds the URL gets the lock screen, not the
desk.

`cloudflared tunnel --url http://localhost:8000` does the same thing and is
generally more reliable if you have cloudflared installed.

### C2. A small VPS (paid, always on)

$4–6/month gets you a box that never sleeps and survives your laptop closing.
Copy the folder over, run `./run.sh`, and put it behind Caddy or nginx so you
get HTTPS. This is the only option that is genuinely always available, and the
only one where the phone doesn't depend on your laptop being open.

Whatever you choose: **leave `DESK_TOKEN` set.** A public URL with no key is a
public trading desk.

---

# The shortest possible version

1. Unzip on the laptop.
2. Install Python, tick "Add to PATH".
3. Double-click `run.bat` (or `./run.sh`).
4. Note the IP from `ipconfig` / `ipconfig getifaddr en0` / `hostname -I`.
5. On the phone, same Wi-Fi: `http://THAT-IP:8000`.
6. Type the key, tap **Pair this device**.
7. Chrome ⋮ → **Add to Home screen**.
8. Tap **Start agent**.

That's it. From step 6 onward that phone opens with no key, and every other
device still needs one.

---

## One thing worth being clear about

Pairing remembers a **browser**, not a person. If someone picks up your unlocked
phone and opens the home-screen icon, they're in — same as any logged-in app.
The pairing protects you from *other devices*, not from *your own phone in
someone else's hand*. If that matters, use your phone's own screen lock, and
revoke the device at `/devices` when you're done with a phone.
