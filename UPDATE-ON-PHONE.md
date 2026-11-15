# Get this onto your phone (Termux)

The code is on a branch, not on `main`. That is deliberate: nothing on your
current desk changes until you ask for it, and you can go back with one command.

## The branch

    desk/winrate-presets

Two commits on top of `main`:

    81e0cb3  feat(desk): measured win-rate presets, volume gate, and a build self-report
    60d0a08  fix(build): derive the asset version from file bytes, not mtime

## Update

    cd ~/solana-desk-
    git fetch origin
    git checkout desk/winrate-presets
    bash termux/stop-desk.sh
    bash termux/start-desk.sh

Then open the desk and run the verifier:

    bash termux/verify-update.sh

It should end with:

    RESULT: up to date, all features present, desk running and answering.

112 checks, 0 failures.

## Go back if you do not like it

    cd ~/solana-desk-
    git checkout main
    bash termux/stop-desk.sh
    bash termux/start-desk.sh

## How to tell, in two seconds, whether the phone is running this code

Open the desk. In the header, next to the access pill, there is a build chip:

    build: 970c528e38

* **Green** and `970c528e38` -> this is the new code.
* **No chip at all** -> the phone is still running the old files. The update
  did not land. Nothing else you see on screen means anything until that chip
  is there.
* **Amber** -> the files on disk are new but the running process is old. Stop
  and start the desk.

Tap the chip (or open `/api/build` in the browser) for the raw report:

    {
      "version": "970c528e38",
      "complete": true,
      "features": { "strategy_panel": true, "preset_row": true, ... },
      "presets": ["balanced", "pf11", "scalper", "swing", "wr60", "wr65"]
    }

`complete: true` means every file this round depends on is present on the phone.
The version is a hash of the file *contents*, so it is the same number on my
machine and on yours. If your number differs, your files differ.

## Where the settings actually are

This is the part that was invisible before. The presets lived only inside the
Desk AI chat panel, which is a floating button in the corner. On a phone that
panel is easy to miss, and the preset row was a single-line scroller with the
scrollbar hidden, so the last three buttons sat off the right edge where you
could not see or tap them.

Now:

1. **Control tab** -> a **Strategy** panel at the top of the left column. It
   reads the live config and shows: pullback %, stop %, target plan, volume
   gate, and the name of the preset in force (`WR-60`, `WR-65`, `PF-1.1`, or
   `custom`). Button: **Change in Desk AI**.
2. **Desk AI** -> the preset row is now a grid, two or three per line, every
   button 44 px tall (the touch minimum). All eight are visible:
   Recommend, Settings, Scalper, Swing, Balanced, WR-60, WR-65, PF-1.1.

## The three measured presets

| Preset | Entry | Gate | Exit | Win rate |
|--------|-------|------|------|----------|
| WR-60  | 15% pullback | none | 15% target | 60.5% |
| WR-65  | 15% pullback | 1.5x volume | 15% target | 64.7% |
| PF-1.1 | 25% pullback | 1.5x volume | patient 2x / 3x ladder | PF 1.08 |

PF-1.1 is the only arm that did not lose money over the wide sample. It is a
button, not the default, because its worst leave-one-pool-out fold was 0.98 --
it is not robust enough to be the house strategy. WR-60 and WR-65 win more
often but sit at or below the 72.3% break-even line for a 30% stop against a
15% target once fees and slippage are counted. A higher win rate is not the
same thing as a profit.

## What is not in this branch

Nothing about live trading. The desk still never holds a wallet key and cannot
sign or send. It is paper only.
