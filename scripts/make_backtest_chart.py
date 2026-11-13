"""Render a visual summary of backtest_100.json -> backtest_100.png"""
import json
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

d = json.load(open("backtest_100.json"))
t = d["trades_detail"]
cap = 1000.0
rets = [min(x["ret_pct"], cap) for x in t]
n = len(rets)
wins = sum(1 for r in rets if r > 0)
losses = n - wins

fig, axes = plt.subplots(1, 3, figsize=(16, 5.2))
fig.suptitle(f"Solana Desk — 100-trade historical backtest (real GeckoTerminal data)",
             fontsize=14, fontweight="bold")

# 1) win/loss donut
ax = axes[0]
ax.pie([wins, losses], labels=[f"Wins\n{wins}", f"Losses\n{losses}"],
       colors=["#2ecc71", "#e74c3c"], autopct="%1.0f%%", startangle=90,
       wedgeprops=dict(width=0.42), textprops=dict(fontsize=11))
ax.set_title(f"Win rate: {wins/n*100:.1f}%", fontsize=13, fontweight="bold")

# 2) return buckets
ax = axes[1]
labels = ["Rug\n<-90%", "Big loss\n-90..-30", "Loss\n-30..0",
          "Win\n0..50", "Win\n50..200", "Big win\n200%+"]
counts = [0] * 6
for r in rets:
    if r <= -90: counts[0] += 1
    elif r <= -30: counts[1] += 1
    elif r < 0: counts[2] += 1
    elif r < 50: counts[3] += 1
    elif r < 200: counts[4] += 1
    else: counts[5] += 1
colors = ["#c0392b", "#e67e22", "#f1c40f", "#2ecc71", "#27ae60", "#16a085"]
bars = ax.bar(labels, counts, color=colors)
for b, c in zip(bars, counts):
    ax.text(b.get_x() + b.get_width()/2, b.get_height() + 0.5, str(c),
            ha="center", fontsize=10, fontweight="bold")
ax.set_title("Outcome distribution", fontsize=13, fontweight="bold")
ax.set_ylabel("trades")
ax.set_ylim(0, max(counts) * 1.18)
ax.spines[["top", "right"]].set_visible(False)

# 3) exit reasons
ax = axes[2]
er = d["exit_reasons"]
items = sorted(er.items(), key=lambda kv: kv[1])
ax.barh([k for k, _ in items], [v for _, v in items], color="#3498db")
for i, (_, v) in enumerate(items):
    ax.text(v + 0.5, i, str(v), va="center", fontsize=9)
ax.set_title("Why trades closed", fontsize=13, fontweight="bold")
ax.set_xlabel("count")
ax.spines[["top", "right"]].set_visible(False)

med = statistics.median(rets)
gw = sum(r for r in rets if r > 0)
gl = -sum(r for r in rets if r <= 0)
fig.text(0.5, 0.01,
         f"Median return: {med:+.1f}%   |   Mean (outliers capped at +1000%): {statistics.mean(rets):+.1f}%   |   "
         f"Profit factor (capped): {gw/gl:.2f}   |   {n} trades   |   PAPER fees 1% + slippage 2%",
         ha="center", fontsize=9, color="#555")

plt.tight_layout(rect=[0, 0.04, 1, 0.95])
out = "backtest_100.png"
plt.savefig(out, dpi=130, bbox_inches="tight")
print("saved", out)
