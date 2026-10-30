"""Figure 6: share of answers equal to the poisoner's value, by condition (Luna / Haiku).

Data: "poisoned" columns of ../../c4-results.md
Run:  uv run --with matplotlib python fig6-poisoned.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import figstyle as S
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

S.apply()

# (label, luna_poisoned, haiku_poisoned, leaks)
CONDS = [
    ("Clean",                 0.00, 0.00, False),
    ("Open",                  0.91, 0.98, True),
    ("Evidence (242d8c2)",    0.00, 0.00, False),
    ("Cited, before ea9b30a", 0.92, 0.98, True),
    ("Cited, after ea9b30a",  0.00, 0.00, False),
    ("Registry",              0.00, 0.00, False),
    ("Proposed",              0.00, 0.00, False),
    ("Mem0, clean",           0.00, 0.02, False),
    ("Mem0, poisoned",        0.02, 0.02, False),
]
N_KEEL = 7  # first seven conditions are Keel; a divider separates the Mem0 ones

fig, ax = plt.subplots(figsize=(S.WIDTH_IN, 3.4))
bh = 0.34     # bar thickness
gap = 0.04    # surface gap between the two bars of a pair
step = 1.0    # distance between condition centres
extra = 0.45  # extra space at the Keel / Mem0 divider

centres = []
y = 0.0
for i, _ in enumerate(CONDS):
    if i == N_KEEL:
        y += extra
    centres.append(y)
    y += step

ax.set_xlim(0, 1.0)
ax.set_ylim(centres[-1] + 0.6, -0.6)   # top to bottom
ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
ax.xaxis.grid(True)
ax.set_axisbelow(True)
ax.set_xlabel("Share of answers equal to the poisoner's value")
ax.tick_params(axis="y", length=0)

fig.canvas.draw()
for (label, luna, haiku, leaks), yc in zip(CONDS, centres):
    pairs = [(luna, yc - gap / 2 - bh, S.KEEL if leaks else S.GREY_DARK),
             (haiku, yc + gap / 2, S.KEEL_LIGHT if leaks else S.GREY_MID)]
    for v, y0, color in pairs:
        if v > 0:
            S.rounded_bar(ax, 0, y0, v, bh, color, horizontal=True)
        else:
            ax.add_patch(matplotlib.patches.Rectangle((0, y0), 0.004, bh, facecolor=color,
                                                      edgecolor="none"))
        ax.text(max(v, 0.004) + 0.008, y0 + bh / 2, f"{round(v * 100):d}%", ha="left",
                va="center", fontsize=7.5,
                color=S.INK if leaks else S.INK2, fontweight="bold" if leaks else "normal")

ax.set_yticks(centres)
ax.set_yticklabels([c[0] for c in CONDS], fontsize=8)
for t, c in zip(ax.get_yticklabels(), CONDS):
    t.set_color(S.INK)
    if c[3]:
        t.set_fontweight("bold")

# divider between the Keel conditions and the Mem0 ones
yd = (centres[N_KEEL - 1] + centres[N_KEEL]) / 2
ax.plot([-0.42, 1.0], [yd, yd], color=S.AXIS, linewidth=0.5, clip_on=False,
        transform=ax.get_yaxis_transform())

handles = [Patch(facecolor=S.GREY_DARK, label="Luna (upper bar)"),
           Patch(facecolor=S.GREY_MID, label="Haiku (lower bar)"),
           Patch(facecolor=S.KEEL, label="Write accepted by Keel (leaks)")]
# inside the axes, in the empty right half of the zero-value rows (a row above the axes
# would be wider than the 5.5 in figure)
ax.legend(handles=handles, ncol=1, loc="center right", bbox_to_anchor=(0.995, 0.43),
          borderaxespad=0, handlelength=0.9, handleheight=0.9, labelspacing=0.35)

S.save(fig, os.path.join(HERE, "fig6-poisoned"))
