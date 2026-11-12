# Solana Desk on Android — Termux route, exact commands

Phone only. No laptop. Everything below runs on the phone.

---

## 1. Install Termux from F-Droid

Not the Play Store. The Play Store build is abandoned and `pkg install` fails with 404s.

1. Open **f-droid.org** in the phone browser.
2. Download and install the F-Droid app (allow "install unknown apps" when asked).
3. In F-Droid, search **Termux** and install it.

---

## 2. Open Termux and give it file access

Type this, then tap **Allow** on the Android popup:

```bash
termux-setup-storage
```

That creates `~/storage/downloads`, which points at your normal Downloads folder.

---

## 3. Update packages and install Python + Rust

```bash
pkg update -y && pkg upgrade -y
pkg install -y python rust binutils clang make pkg-config unzip
```

Rust and clang are not optional. Two of the desk's dependencies (`pydantic-core`
and, if you ever go live, `solders`) are compiled Rust extensions with no
prebuilt Android wheels, so pip builds them on your phone. Without a compiler
you get `error: can't find crate for 'core'`, which looks like a broken package
but is really a missing toolchain.

This step downloads a few hundred MB. Use Wi-Fi.

---

## 4. Move the zip into Termux and unzip it

Your zip is in Downloads. Copy it in, then extract:

```bash
cd ~
cp ~/storage/downloads/solana-desk-with-guide.zip .
unzip -o solana-desk-with-guide.zip
cd solana-desk
```

If the file name differs, list what is actually there:

```bash
ls ~/storage/downloads
```

---

## 5. Make the launcher executable and start it

```bash
chmod +x run.sh
./run.sh MyDeskKey123
```

`MyDeskKey123` is your own key — pick anything, but remember it. Passing a key
means it stays the same every launch, so you can unlock from a new browser
without hunting for a new random key each time.

The first run is the long one: pip compiles `pydantic-core` from Rust. Expect
**5 to 15 minutes** on a mid-range phone, and the phone will get warm. That is
normal. Do not close Termux.

When it finishes you will see:

```
==============================================================
  Solana Desk is LOCKED

  Open:  http://localhost:8000
  Key:   MyDeskKey123
==============================================================
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## 6. Open it in the phone browser

Leave Termux running. Open Chrome and go to:

```
http://localhost:8000
```

Type the key, then tap **Pair this device** and give it a name like `Pixel`.
Pairing is stored on disk against the device's own token, so it survives
restarts of the desk.

Then Chrome **⋮** → **Add to Home screen** → **Install**. You get a full-screen
icon with no address bar.

Tap **Start agent** on the dashboard.

---

## 7. Stop Android from killing it

This is the step people skip, and then the desk dies silently about ten minutes
after the screen locks.

In Termux:

```bash
termux-wake-lock
```

In Android settings:

- **Settings → Apps → Termux → Battery → Unrestricted**
- Samsung only: **Settings → Battery → Background usage limits** and make sure
  Termux is not in "Sleeping apps" or "Deep sleeping apps"

Also turn off battery optimisation for Termux if your phone has a separate
"App battery usage" screen. Keep the Termux notification visible — that
notification is what keeps the process alive.

---

## 8. Restarting it later

After a reboot or if you closed Termux:

```bash
cd ~/solana-desk
./run.sh MyDeskKey123
```

Dependencies are already built, so this starts in seconds. Your state is kept in
the folder: `paper_ledger.json`, `journal.jsonl`, `desk_devices.json`.

Stop it with `Ctrl+C` in Termux, or swipe the notification away.

---

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| `pkg: command not found` or 404s | Play Store Termux | uninstall, install from F-Droid |
| `error: can't find crate for 'core'` | no Rust toolchain | `pkg install rust binutils clang` then retry |
| pip build fails / killed | out of memory or heat | close other apps, retry with `CARGO_BUILD_JOBS=1 pip install -r requirements.txt` |
| `Permission denied: ./run.sh` | not executable | `chmod +x run.sh` |
| `Address already in use` | port 8000 busy | `PORT=8080 ./run.sh MyDeskKey123`, then open `:8080` |
| Browser says "can't be reached" | desk not running | check Termux still shows "Uvicorn running" |
| Dies when screen locks | no wake lock / battery restricted | `termux-wake-lock` + Battery → Unrestricted |
| Lock screen every time | different browser, or cookies cleared | pair again; do not use incognito |
| Page loads, panels empty | agent not started | tap **Start agent** |

---

## Notes

- Live signing needs `solders`, which is the same Rust toolchain from step 3.
  Verify with `pip install -r requirements-live.txt`. If it succeeds the startup
  banner says `Live signing: available`; otherwise `unavailable (paper only)`.
- The desk always boots in **paper** mode. Real money requires installing the
  live requirements, exporting `SOLANA_PRIVATE_KEY`, and arming live mode by
  typing the confirmation phrase.
- Never use `./run.sh --open` — that removes the lock entirely.

---

## Optional: turn on the AI advisor (free keys)

The desk runs fine without AI keys — the gates, the scanner and the paper broker
never call a model. With a key set, the **AI** tab stops saying *"AI keys not
set"* and each candidate gets a second opinion in the log that you can Confirm
or ignore. The advisor is advisory only: it cannot size a trade, open one,
loosen a gate or override a kill.

```bash
cd ~/solana-desk
grep -v -e '^GEMINI_API_KEY=' -e '^OPENROUTER_API_KEY=' -e '^BRAIN_GEMINI_MODEL=' .env 2>/dev/null > .env.new; mv .env.new .env
printf 'GEMINI_API_KEY=your_gemini_key\nOPENROUTER_API_KEY=your_openrouter_key\nBRAIN_GEMINI_MODEL=gemini-3.5-flash-lite\n' >> .env
chmod 600 .env
pkill -f "[u]vicorn app:app"; sleep 6
curl -s http://127.0.0.1:8000/api/health | head -c 300; echo
```

- Gemini key: https://aistudio.google.com/apikey (free tier)
- OpenRouter key: https://openrouter.ai/keys (free models only; 50 requests/day
  without credits, resets at 00:00 UTC)
- Groq is optional: `GROQ_API_KEY` from https://console.groq.com/keys

`.env` is gitignored, so keys are never committed. `load_env()` runs once at
startup, so **any change to `.env` needs a restart** — unlike the dashboard
files, which are served fresh from disk on every request.

### Why `gemini-3.5-flash-lite` and not the default

`gemini-3.6-flash` reasons before it answers. Measured on a free key it took
**42 seconds** for a one-line reply, which is past the 8s per-call timeout, so
every review failed with `gemini: timeout`. `gemini-3.5-flash-lite` answered the
same prompt in **0.7s**. If you want the bigger model, raise the timeout instead:

```bash
printf 'BRAIN_TIMEOUT_S=45\n' >> ~/solana-desk/.env
```

A timeout now falls through to the next model in the list rather than failing
the whole review.
