#!/usr/bin/env python3
"""Why a looser preset can make less money: expectancy, not win rate.

Reads the real preset values out of desk/settings.py and prints, for each one:

  * the blended take-profit (TP1 and TP2 weighted by their fractions)
  * the reward:risk ratio against the stop
  * the break-even win rate that ratio demands
  * the expected P&L per trade at a given observed win rate, net of fees

Run:
    python scripts/compare_presets.py
    python scripts/compare_presets.py --win legend_scalp=0.47 super_legend_scalp=0.64
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import PAPER  # noqa: E402
from desk.settings import PRESETS  # noqa: E402
from desk.broker import paper_side_fee  # noqa: E402

# Observed on the two live desks (screenshots, 2026-11-16).
#   legend_scalp       $98.89  +$48.89  +97.78%  39 entries 30 closed 9 open  47% win
#   super_legend_scalp $52.94  +$2.94   +5.88%   35 entries 25 closed 10 open 64% win
DEFAULT_WIN = {"legend_scalp": 0.47, "super_legend_scalp": 0.64,
               "super_legend_v2": 0.58}
OBSERVED = {
    "legend_scalp": {"pnl": 48.89, "closed": 30, "start": 50.0},
    "super_legend_scalp": {"pnl": 2.94, "closed": 25, "start": 50.0},
}


def blended_tp(v: dict) -> float:
    """TP1 and TP2 weighted by their fractions, as a return multiple."""
    f1 = float(v.get("scalp.tp1_frac", 0.5))
    f2 = float(v.get("scalp.tp2_frac", 0.5))
    t1 = float(v.get("scalp.tp1_at", 1.05))
    t2 = float(v.get("scalp.tp2_at", 1.12))
    total = f1 + f2
    if total <= 0:
        return t1
    return (f1 * t1 + f2 * t2) / total


def analyse(name: str, win: float, ticket: float = 5.0) -> dict:
    v = PRESETS[name]["values"]
    stop = float(v.get("scalp.stop_pct", 0.08))
    tp = blended_tp(v)
    gain = tp - 1.0
    rr = gain / stop if stop else 0.0
    # Break-even: p*gain == (1-p)*stop  ->  p = stop / (stop + gain)
    be = stop / (stop + gain) if (stop + gain) else 1.0
    # Fees: what the PAPER desk actually charges (base + priority, no rent --
    # rent is a refundable deposit, see desk/broker.paper_side_fee). Plus the
    # 2% slippage the paper fill already applies on each side.
    fee_pct = (paper_side_fee(ticket)["total_usd"]
               + paper_side_fee(ticket)["total_usd"]) / ticket
    slip = float(PAPER.get("slippage_pct", 0.02)) * 2
    drag = fee_pct + slip
    win_net = gain - drag
    loss_net = stop + drag
    exp_pct = win * win_net - (1 - win) * loss_net
    return {
        "preset": name,
        "label": PRESETS[name]["label"],
        "stop": stop, "tp": tp, "gain": gain, "rr": rr,
        "breakeven": be, "win": win, "fee_pct": drag,
        "exp_pct": exp_pct, "exp_usd": exp_pct * ticket,
        "hold": v.get("scalp.max_hold_minutes"),
        "swing_vol": v.get("swing.min_volume_h1_usd"),
        "swing_trades": v.get("swing.min_trades_h1"),
        "max_mcap": v.get("scan.max_mcap_usd"),
    }


def implied_winner(name: str, ticket: float) -> dict:
    """Back out the average winner and loser from the observed P&L.

    We know the book's P&L, the number of closed trades and the win rate, and
    we know the preset's stop. So the average loser is the stop (a loss exits
    at the stop, by definition) and the average winner is whatever makes the
    arithmetic add up. That tells us how far the trailing stop actually let
    winners run -- the number the TP ladder cannot tell you.
    """
    o = OBSERVED[name]
    win = DEFAULT_WIN[name]
    stop = float(PRESETS[name]["values"].get("scalp.stop_pct", 0.08))
    n_win = o["closed"] * win
    n_loss = o["closed"] - n_win
    loss_usd = stop * ticket
    total_loss = n_loss * loss_usd
    total_win = o["pnl"] + total_loss
    avg_win_usd = total_win / n_win if n_win else 0.0
    return {
        "n_win": n_win, "n_loss": n_loss,
        "avg_loss_usd": loss_usd, "avg_win_usd": avg_win_usd,
        "avg_win_pct": avg_win_usd / ticket,
        "payoff": (avg_win_usd / loss_usd) if loss_usd else 0.0,
        "total_loss": total_loss, "total_win": total_win,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--win", nargs="*", default=[],
                    help="preset=winrate overrides, e.g. legend_scalp=0.47")
    ap.add_argument("--ticket", type=float, default=5.0)
    args = ap.parse_args()

    wins = dict(DEFAULT_WIN)
    for item in args.win:
        k, _, val = item.partition("=")
        wins[k.strip()] = float(val)

    rows = [analyse(n, wins.get(n, 0.5), args.ticket)
            for n in ("legend_scalp", "super_legend_scalp")]

    print(f"ticket ${args.ticket:.2f}  |  paper round-trip cost "
          f"{rows[0]['fee_pct']*100:.2f}% (fees + 2x slippage)")
    print()
    hdr = f"{'preset':<22}{'stop':>7}{'TP':>7}{'R:R':>7}{'BE win':>9}{'win':>7}{'EV/trade':>11}{'hold':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['preset']:<22}{r['stop']*100:>6.0f}%{r['tp']:>7.2f}"
              f"{r['rr']:>7.2f}{r['breakeven']*100:>8.1f}%{r['win']*100:>6.0f}%"
              f"{r['exp_pct']*100:>+10.2f}%{str(r['hold']):>6}m")
    print()
    for r in rows:
        edge = r["win"] - r["breakeven"]
        verdict = "PROFITABLE" if edge > 0 else "LOSING"
        print(f"{r['preset']}: needs {r['breakeven']*100:.1f}% to break even, "
              f"wins {r['win']*100:.0f}%  ->  edge {edge*100:+.1f} pts  ->  {verdict}")
        print(f"    expected {r['exp_pct']*100:+.2f}% per trade = "
              f"${r['exp_usd']:+.3f} on a ${args.ticket:.2f} ticket")

    print()
    print("What the ledgers actually did (implied winner size):")
    print(f"{'preset':<22}{'wins':>6}{'losses':>8}{'avg win':>10}{'avg loss':>10}{'payoff':>8}")
    print("-" * 64)
    for name in ("legend_scalp", "super_legend_scalp"):
        iw = implied_winner(name, args.ticket)
        print(f"{name:<22}{iw['n_win']:>6.1f}{iw['n_loss']:>8.1f}"
              f"{iw['avg_win_pct']*100:>9.0f}%{iw['avg_loss_usd']/args.ticket*100:>9.0f}%"
              f"{iw['payoff']:>8.2f}")
    print()
    print("payoff = average winner / average loser. A scalper needs payoff > "
          "(1-win)/win to make money.")
    for name in ("legend_scalp", "super_legend_scalp"):
        iw = implied_winner(name, args.ticket)
        need = (1 - DEFAULT_WIN[name]) / DEFAULT_WIN[name]
        print(f"  {name}: has {iw['payoff']:.2f}, needs {need:.2f}"
              f"  ->  {'OK' if iw['payoff'] > need else 'TOO SMALL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
