"""Render BACKTEST_GATES_REPORT.md numbers as one chart."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
d = json.loads((ROOT / "backtest_gates.json").read_text(encoding="utf-8"))

fig, axes = plt.subplots(1, 3, figsize=(16, 5.2))
fig.patch.set_facecolor("#0e1116")
for ax in axes:
    ax.set_facecolor("#0e1116")
    for sp in ax.spines.values():
        sp.set_color("#2a3140")
    ax.tick_params(colors="#c8d0dc", labelsize=9)
    ax.grid(axis="y", color="#1c2230", linewidth=0.8)

GREEN, RED, GREY, ACC = "#3ddc97", "#ff5c7a", "#7c8798", "#5b9dff"

# ---- 1. gate ladder ----
labels = ["no\ngates", "+SCAN", "+flow", "+organic", "+meme\n>=50", "+comm\n>=35", "+vol\ntrend"]
rows = [d["ladder"][k]["dev passes"] for k in d["ladder"]]
wins = [r["win_rate"] * 100 if r["trades"] else 0 for r in rows]
ns = [r["trades"] for r in rows]
bars = axes[0].bar(range(len(labels)), wins, color=[GREY] + [ACC] * 6, width=0.68)
for i, (b, w, n) in enumerate(zip(bars, wins, ns)):
    axes[0].text(b.get_x() + b.get_width() / 2, w + 1.2, f"{w:.0f}%",
                 ha="center", color="#e8edf5", fontsize=10, fontweight="bold")
    axes[0].text(b.get_x() + b.get_width() / 2, 1.5, f"n={n}",
                 ha="center", color="#0e1116", fontsize=8)
axes[0].set_xticks(range(len(labels)))
axes[0].set_xticklabels(labels, fontsize=8)
axes[0].set_ylim(0, 50)
axes[0].set_ylabel("win rate %", color="#c8d0dc")
axes[0].set_title("Gate ladder: 101 real pools", color="#e8edf5", fontsize=12, pad=10)

# ---- 2. hold time ----
combos = {c["label"]: c for c in d["combos"]["all"]}
order = ["current (stop 20%, hold 4h)", "no stop, hold 4h", "no stop, hold 12h",
         "no stop, hold 48h", "stop 40%, hold 48h", "stop 40%, hold to data end"]
short = ["4h\nstop20", "4h\nno stop", "12h\nno stop", "48h\nno stop", "48h\nstop40", "end\nstop40"]
w = [combos[k]["win_rate"] * 100 for k in order]
m = [combos[k]["mean_ret"] for k in order]
bars = axes[1].bar(range(len(order)), w, color=[RED] + [GREEN] * 5, width=0.68)
for b, ww, mm in zip(bars, w, m):
    axes[1].text(b.get_x() + b.get_width() / 2, ww + 1.0, f"{ww:.0f}%",
                 ha="center", color="#e8edf5", fontsize=10, fontweight="bold")
    axes[1].text(b.get_x() + b.get_width() / 2, ww / 2, f"{mm:+.1f}%\nmean",
                 ha="center", color="#0e1116", fontsize=7.5)
axes[1].set_xticks(range(len(order)))
axes[1].set_xticklabels(short, fontsize=8)
axes[1].set_ylim(0, 45)
axes[1].set_title("Hold time is the lever (same 101 trades)", color="#e8edf5",
                  fontsize=12, pad=10)

# ---- 3. old vs corrected ----
cats = ["win rate", "mean return"]
old = [13.0, 9.1]
new = [53.0, 10.0]
x = range(len(cats))
b1 = axes[2].bar([i - 0.19 for i in x], old, width=0.36, color=RED, label="old script")
b2 = axes[2].bar([i + 0.19 for i in x], new, width=0.36, color=GREEN, label="corrected")
for bars in (b1, b2):
    for b in bars:
        axes[2].text(b.get_x() + b.get_width() / 2, b.get_height() + 1.2,
                     f"{b.get_height():.1f}%", ha="center", color="#e8edf5",
                     fontsize=10, fontweight="bold")
axes[2].set_xticks(list(x))
axes[2].set_xticklabels(cats, fontsize=10)
axes[2].set_ylim(0, 62)
axes[2].set_title("Same 100 trades, two arithmetics", color="#e8edf5",
                  fontsize=12, pad=10)
leg = axes[2].legend(facecolor="#161b24", edgecolor="#2a3140", fontsize=9)
for t in leg.get_texts():
    t.set_color("#c8d0dc")

fig.suptitle("Solana Desk — gate ladder, hold time, and a corrected baseline",
             color="#e8edf5", fontsize=14, y=0.99)
fig.tight_layout(rect=[0, 0, 1, 0.94])
out = ROOT / "backtest_gates.png"
fig.savefig(out, dpi=140, facecolor=fig.get_facecolor())
print("wrote", out)
