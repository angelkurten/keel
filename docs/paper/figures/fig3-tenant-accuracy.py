"""Figure 3: accuracy on the 72 tenant-wide questions, by agent and system, with 95% CIs.

Data: tenant-ci.json  (rows: model, system, n_questions, mean, lo, hi)
Run:  uv run --with matplotlib python fig3-tenant-accuracy.py
"""
from __future__ import annotations

import json
import os
import sys
from decimal import ROUND_HALF_EVEN, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import figstyle as S  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

S.apply()

DATA = os.path.join(HERE, "tenant-ci.json")
MODELS = [("gpt-6-luna", "Luna"), ("claude-haiku-5.5", "Haiku"), ("claude-sonnet-5.5", "Sonnet")]
SYSTEMS = [("keel", "Keel", S.KEEL), ("api", "API", S.GREY_DARK),
           ("rag", "RAG", S.GREY_MID), ("closed", "Closed book", S.GREY_LIGHT)]

rows = {(m, s): (n, mean, lo, hi) for m, s, n, mean, lo, hi in json.load(open(DATA))}

fig, ax = plt.subplots(figsize=(S.WIDTH_IN, 2.9))
ax.set_xlim(-0.55, len(MODELS) - 0.45)
ax.set_ylim(0, 1.1)
ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
ax.set_yticklabels(["0", "0.25", "0.50", "0.75", "1.00"])
ax.yaxis.grid(True)
ax.set_axisbelow(True)
ax.tick_params(axis="x", length=0)
ax.set_ylabel("Accuracy on 72 tenant-wide questions")

bw = 0.19      # bar width
gap = 0.025    # surface gap between adjacent bars
group = len(SYSTEMS) * bw + (len(SYSTEMS) - 1) * gap

fig.canvas.draw()  # transforms need the final layout for the rounded corners
for i, (mid, mlabel) in enumerate(MODELS):
    x0 = i - group / 2
    for j, (sid, slabel, color) in enumerate(SYSTEMS):
        n, mean, lo, hi = rows[(mid, sid)]
        x = x0 + j * (bw + gap)
        xc = x + bw / 2
        S.rounded_bar(ax, x, 0, bw, mean, color)
        if hi > lo:
            ax.errorbar(xc, mean, yerr=[[mean - lo], [hi - mean]], fmt="none",
                        ecolor=S.INK, elinewidth=0.7, capsize=2, capthick=0.7, zorder=3)
        # round half to even on the decimal value, as the paper's tables do (0.265 -> 0.26)
        label = str(Decimal(str(mean)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
        if n < 72:
            label += "†"
        ax.text(xc, max(hi, mean) + 0.025, label, ha="center", va="bottom", fontsize=7.5,
                color=S.INK if sid == "keel" else S.INK2,
                fontweight="bold" if sid == "keel" else "normal")

ax.set_xticks(range(len(MODELS)))
ax.set_xticklabels([m for _, m in MODELS])
for t in ax.get_xticklabels():
    t.set_color(S.INK)

handles = [Patch(facecolor=c, edgecolor="none", label=l) for _, l, c in SYSTEMS]
ax.legend(handles=handles, ncol=len(SYSTEMS), loc="lower left", bbox_to_anchor=(0, 1.0),
          borderaxespad=0, handlelength=0.9, handleheight=0.9)

DAGGER = "†"
ax.text(0, -0.17, DAGGER + "34 of 72 questions", transform=ax.transAxes, fontsize=7,
        color=S.MUTED, ha="left", va="top")

S.save(fig, os.path.join(HERE, "fig3-tenant-accuracy"))
