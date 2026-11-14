import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

d = json.load(open("winrate_lab.json"))


def find(g, pb, plan, stop):
    for r in d["all"]:
        if r["gate"] == g and r["pullback"] == pb and r["plan"] == plan and abs(r["stop"] - stop) < 1e-9:
            return r
    raise KeyError((g, pb, plan, stop))


configs = [
    ("Buy every bar\n(default ladder, 20% stop)", find("none", 0.0, "profile (default)", 0.20)),
    ("10% dip +\nfast ladder, 30% stop", find("none", 0.10, "quick 1.25x half", 0.30)),
    ("20% dip +\ndefault ladder, 30% stop", find("none", 0.20, "profile (default)", 0.30)),
    ("20% dip +\nfast ladder, 30% stop", find("none", 0.20, "quick 1.25x half", 0.30)),
]

# leave-one-pool-out for the winning config (computed earlier)
loo = {
    "drop 2XhQqp1v": 70.8, "drop AQFC3QfA": 58.5, "drop FbR7WmU8": 69.1,
    "drop GCApLjm8": 68.1, "drop F1KKNyUC": 68.2, "drop A6KHMiFz": 69.7,
    "drop 4AFZ7Xzc": 70.8, "drop Dw4kAH8L": 68.6, "drop 5tYFviFW": 69.4,
    "drop 9LhNWipL": 72.9, "drop 5E3dXLXG": 69.9,
}

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6.2))
fig.suptitle("Solana Desk — Win-Rate Lab on 120 pools / 114,033 candles",
             fontsize=15, fontweight="bold")

# Panel A
labels = [c[0] for c in configs]
wins = [c[1]["win_rate"] * 100 for c in configs]
pfs = [c[1]["profit_factor"] for c in configs]
ns = [c[1]["trades"] for c in configs]
pools = [c[1]["pools"] for c in configs]
colors = ["#b0b0b0", "#e08a3c", "#4a90d9", "#2e9e5b"]
bars = ax1.bar(range(len(configs)), wins, color=colors, edgecolor="black", linewidth=0.6)
ax1.axhline(50, color="red", linestyle="--", linewidth=1.6, label="50% win target")
ax1.set_xticks(range(len(configs)))
ax1.set_xticklabels(labels, fontsize=8.5)
ax1.set_ylabel("Win rate (%)")
ax1.set_ylim(0, 80)
ax1.set_title("Win rate by entry + exit config", fontsize=12)
for i, b in enumerate(bars):
    ax1.text(b.get_x() + b.get_width() / 2, b.get_height() + 1.2,
             f"{wins[i]:.1f}%\nPF {pfs[i]:.2f}\nn={ns[i]}, {pools[i]} pools",
             ha="center", va="bottom", fontsize=8)
ax1.legend(loc="upper left")

# Panel B
names = list(loo.keys())
vals = list(loo.values())
order = sorted(range(len(vals)), key=lambda i: vals[i])
names = [names[i] for i in order]
vals = [vals[i] for i in order]
bars2 = ax2.barh(range(len(vals)), vals, color="#2e9e5b", edgecolor="black", linewidth=0.5)
ax2.axvline(50, color="red", linestyle="--", linewidth=1.6, label="50% win target")
ax2.set_yticks(range(len(names)))
ax2.set_yticklabels(names, fontsize=8)
ax2.set_xlabel("Win rate after dropping that pool (%)")
ax2.set_xlim(0, 80)
ax2.set_title("Robustness: leave-one-pool-out (winning config)", fontsize=12)
for i, b in enumerate(bars2):
    ax2.text(b.get_width() + 0.8, b.get_y() + b.get_height() / 2, f"{vals[i]:.1f}%",
             va="center", fontsize=8)
ax2.legend(loc="lower right")

fig.text(0.5, 0.01,
         "Winning config: 20% pullback entry + fast ladder (sell half 1.25x, rest 1.6x / 15% trail) + 30% stop.  "
         "Survives dropping any single pool (worst 58.5% win, PF 2.71).",
         ha="center", fontsize=9, style="italic")
plt.tight_layout(rect=[0, 0.04, 1, 0.96])
plt.savefig("winrate_summary.png", dpi=150)
print("wrote winrate_summary.png")
