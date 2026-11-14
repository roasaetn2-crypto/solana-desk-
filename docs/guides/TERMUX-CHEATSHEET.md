# Solana Desk — Termux cheat sheet

Closing Termux does **not** delete anything. Files stay on disk. Only the
running server stops. Open Termux again and paste one line.

---

## 1. Start the desk (after closing Termux or rebooting)

```
cd ~/solana-desk && nohup bash termux/start-desk.sh >/dev/null 2>&1 & sleep 8 && curl -s http://127.0.0.1:8000/api/health | head -c 200
```

The `nohup ... &` detaches it, so you can press Home or close the session and
it keeps running.

---

## 2. Load the AI keys (once — already done if .env has them)

```
cd ~/solana-desk && bash termux/set-ai-keys.sh - -
```

`- -` means "reuse whatever keys are already in .env". To change them:

```
cd ~/solana-desk && bash termux/set-ai-keys.sh <GEMINI_KEY> <OPENROUTER_KEY>
```

---

## 3. Check it is alive

```
curl -s http://127.0.0.1:8000/api/health | head -c 400
```

Look for `"running":true` and `"keys_present":true`.

---

## 4. Open the dashboard

Phone browser → http://127.0.0.1:8000/?v=2

Password is in `~/.desk_key`:

```
cat ~/.desk_key
```

---

## 5. Stop everything

```
pkill -f "[s]tart-desk.sh"; pkill -f "[u]vicorn app:app"
```

The brackets matter — without them `pkill` matches its own command line and
kills your shell too.

---

## 6. Survive a phone reboot (optional, one time)

Install **Termux:Boot** from F-Droid (not Play Store), open it once, then:

```
mkdir -p ~/.termux/boot && cp ~/solana-desk/termux/boot-start.sh ~/.termux/boot/10-desk.sh && chmod +x ~/.termux/boot/10-desk.sh
```

---

## 7. Update the code

```
cd ~/solana-desk && git pull && pkill -f "[u]vicorn app:app"
```

The supervisor respawns the server within 10s with the new code.

---

## Rules of thumb

- **Paste one line at a time.** Termux splits multi-line pastes on newlines and
  runs the fragments as separate commands, which produces garbage.
- **Ctrl+C not responding?** Swipe from the left edge → NEW SESSION. Abandon
  the stuck one.
- **Never** `git checkout .` unless you mean to throw away local edits.
- `.env` is gitignored, so keys are never committed.
- Changing `.env` needs a restart. Changing dashboard files (`web/*`) does not.

---

## 8. AI quota — why the desk says "AI busy / limited"

The trading loop ticks every **30 seconds**. Unpaced, the advisor asks for an
opinion **120 times an hour**:

```
2 coins x 2 providers + 1 debate (3 turns) = 7 calls per cycle
7 x 120 cycles/hour = 840 calls/hour
```

A 200-call daily Gemini budget is gone in **about 25 minutes**, and then the AI
tab says "AI busy / limited right now" for the rest of the day.

The quota-saver profile in `.env` fixes that:

| Setting | Default | Saver | Effect |
|---|---|---|---|
| `BRAIN_PER_CYCLE` | 2 | **1** | one coin reviewed per cycle |
| `BRAIN_OPINIONS` | 2 | **1** | one model instead of two voices |
| `BRAIN_DEBATES_PER_CYCLE` | 1 | **0** | drops the 3-turn debate |
| `BRAIN_MIN_INTERVAL_S` | 0 | **240** | at most one review every 4 min |
| `BRAIN_CACHE_TTL_S` | 1500 | **1800** | reuse an opinion for 30 min |

That is **15 calls/hour ~ 180/day**, inside the 200 cap - the AI lasts all day.

`termux/set-ai-keys.sh` writes this profile automatically. To get the two-bot
debate back (and burn quota faster):

```
cd ~/solana-desk && KEEP_QUOTA=0 bash termux/set-ai-keys.sh - -
```

To tune by hand, edit `~/solana-desk/.env` and restart:

```
cd ~/solana-desk && pkill -f "[u]vicorn app:app"
```

Check the live settings any time:

```
curl -s http://127.0.0.1:8000/api/ai/status
```
