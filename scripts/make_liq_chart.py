import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

tiers = json.load(open("liq_tiers.json"))
# add the true unfiltered baseline from the first run
baseline = {"label": "No filter\n(all pools)", "win_rate": 13.0, "rug_pct": 81.0, "n": 100}
rows = [baseline]
for t in ["0", "100000", "250000"]:
    if t in tiers:
        r = tiers[t]
        label = "≥ $50k" if t == "0" else f"≥ ${int(t):,}"
        rows.append({"label": label, "win_rate": r["win_rate"], "rug_pct": r["rug_pct"], "n": r["n"]})

labels = [r["label"] for r in rows]
win = [r["win_rate"] for r in rows]
rug = [r["rug_pct"] for r in rows]
ns = [r["n"] for r in rows]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle("Liquidity filter improves win rate & cuts rugs (real GeckoTerminal data)",
             fontsize=13, fontweight="bold")

colors = ["#e74c3c", "#f39c12", "#3498db", "#27ae60"]
b1 = ax1.bar(labels, win, color=colors)
for b, w, n in zip(b1, win, ns):
    ax1.text(b.get_x() + b.get_width()/2, b.get_height()+0.6, f"{w:.1f}%\n(n={n})",
             ha="center", fontsize=9, fontweight="bold")
ax1.set_title("Win rate by minimum liquidity", fontsize=12, fontweight="bold")
ax1.set_ylabel("win rate %"); ax1.set_ylim(0, max(win)*1.35)
ax1.spines[["top","right"]].set_visible(False)

b2 = ax2.bar(labels, rug, color=colors)
for b, r in zip(b2, rug):
    ax2.text(b.get_x()+b.get_width()/2, b.get_height()+0.6, f"{r:.1f}%",
             ha="center", fontsize=9, fontweight="bold")
ax2.set_title("Rug rate (< -90% total loss) by minimum liquidity", fontsize=12, fontweight="bold")
ax2.set_ylabel("rug %"); ax2.set_ylim(0, max(rug)*1.2)
ax2.spines[["top","right"]].set_visible(False)

plt.tight_layout(rect=[0,0,1,0.93])
plt.savefig("liq_comparison.png", dpi=130, bbox_inches="tight")
print("saved liq_comparison.png")
