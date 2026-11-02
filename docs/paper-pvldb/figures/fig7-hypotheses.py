"""Figure 7: the three hypotheses' paired differences on each set, with 95% cluster CIs.

Data: hypotheses.json (rows: hypothesis, set, diff, lo, hi in points), from evals/jira/confirm_analysis.analyze
over each set's Haiku rows (main.tex, Table tab:sets-acc).
Run:  uv run --with matplotlib python fig7-hypotheses.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import figstyle as S
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

S.apply()

HYPS = [("H1", "H1: sqlgen2 − sql", -2.0), ("H2", "H2: keel+query − sql", -2.0), ("H3", "H3: keel+query − keel", 0.0)]
SETS = [("dev", "Development", S.GREY_MID, "o"), ("heldout", "Held-out", S.GREY_DARK, "o"), ("confirm", "Confirmation", S.KEEL, "D")]

rows = {(h, s): (d, lo, hi) for h, s, d, lo, hi in json.loads(Path(HERE, "hypotheses.json").read_text())}

fig, ax = plt.subplots(figsize=(S.WIDTH_IN, 2.3))
step, spread = 1.0, 0.22
ys = {}
for i, (h, _, _) in enumerate(HYPS):
    for j, (s, _, _, _) in enumerate(SETS):
        ys[(h, s)] = -(i * step) - (j - 1) * spread
ax.axvline(0, color=S.AXIS, lw=0.6, zorder=1)
for i, (h, _, bound) in enumerate(HYPS):  # the decision bound, under that hypothesis's rows only
    y0 = -(i * step)
    ax.plot([bound, bound], [y0 - 0.42, y0 + 0.42], color=S.INK2, lw=0.9, ls=(0, (3, 2)), zorder=2)
for h, _, _ in HYPS:
    for s, _, color, marker in SETS:
        d, lo, hi = rows[(h, s)]
        y = ys[(h, s)]
        ax.plot([lo, hi], [y, y], color=color, lw=1.2, solid_capstyle="butt", zorder=3)
        ax.plot(d, y, marker=marker, ms=4.2 if marker == "D" else 4, color=color, mec="white", mew=0.5, zorder=4)

ax.set_yticks([-(i * step) for i in range(len(HYPS))])
ax.set_yticklabels([label for _, label, _ in HYPS])
for t in ax.get_yticklabels():
    t.set_color(S.INK)
ax.tick_params(axis="y", length=0)
ax.set_ylim(-(len(HYPS) - 1) * step - 0.5, 0.5)
ax.set_xlim(-2.6, 5.6)
ax.set_xticks([-2, -1, 0, 1, 2, 3, 4, 5])
ax.xaxis.grid(True)
ax.set_axisbelow(True)
ax.spines["left"].set_visible(False)
ax.set_xlabel("Paired difference, points (95% cluster CI)")

handles = [Line2D([], [], color=c, marker=m, lw=1.2, ms=4, mec="white", mew=0.5, label=l) for _, l, c, m in SETS]
handles.append(Line2D([], [], color=S.INK2, lw=0.9, ls=(0, (3, 2)), label="Decision bound"))
ax.legend(handles=handles, ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0), borderaxespad=0.2)

S.save(fig, os.path.join(HERE, "fig7-hypotheses"))
