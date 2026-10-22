"""Figure 4: accuracy by question type, all agents pooled (heatmap).

Data: full-run-results.md, section "Accuracy by type, all agents pooled"
      (order there: abstain/as_of/current/known_at/links/rule/tenant), reordered here.
Run:  uv run --with matplotlib python fig4-by-type.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import figstyle as S  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, to_rgb  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

S.apply()

COLS = ["Current", "As-of", "Known-at", "Links", "Rule", "Abstain", "Tenant-wide"]
# (system, [current, as_of, known_at, links, rule, abstain, tenant]); None = not asked
ROWS = [
    ("Keel",        [1.00, 1.00, 0.97, 0.97, 0.99, 1.00, 0.94]),
    ("API",         [1.00, 0.94, 0.94, 0.92, 0.99, 1.00, 0.65]),
    ("RAG",         [1.00, 0.99, 0.97, 0.93, 0.99, 0.99, 0.17]),
    ("Mem0",        [0.55, 0.53, 0.77, 0.44, 0.56, 1.00, None]),
    ("Graphiti",    [0.15, 0.15, 0.07, 0.00, 0.12, 0.96, None]),
    ("Closed book", [0.00, 0.00, 0.00, 0.00, 0.00, 1.00, 0.00]),
]

cmap = LinearSegmentedColormap.from_list("keel_blue", S.SEQ_BLUE)


def luminance(rgb):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


nrow, ncol = len(ROWS), len(COLS)
fig, ax = plt.subplots(figsize=(S.WIDTH_IN, 2.75))
ax.set_xlim(0, ncol + 0.18)   # extra room for the divider before the last column
ax.set_ylim(nrow, 0)
ax.set_aspect("auto")
for sp in ax.spines.values():
    sp.set_visible(False)
ax.tick_params(length=0)

DIV = 0.18  # horizontal space inserted before the tenant-wide column
inset = 0.045


def cx(j):
    return j + (DIV if j == ncol - 1 else 0)


for i, (name, vals) in enumerate(ROWS):
    for j, v in enumerate(vals):
        x = cx(j) + inset
        y = i + inset
        w = 1 - 2 * inset
        h = 1 - 2 * inset
        if v is None:
            ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.06",
                                        facecolor="none", edgecolor=S.AXIS, linewidth=0.5,
                                        linestyle=(0, (2, 2))))
            ax.text(x + w / 2, y + h / 2, "n/a", ha="center", va="center", fontsize=7.5,
                    color=S.MUTED)
            continue
        color = cmap(v)
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.06",
                                    facecolor=color, edgecolor="none"))
        txt = S.INK if luminance(to_rgb(color)) > 0.3 else "#ffffff"
        ax.text(x + w / 2, y + h / 2, f"{v:.2f}", ha="center", va="center", fontsize=8,
                color=txt)

# divider between one-issue and tenant-wide columns
xd = ncol - 1 + DIV / 2
ax.plot([xd, xd], [-0.05, nrow + 0.02], color=S.AXIS, linewidth=0.5, clip_on=False)

ax.set_yticks([i + 0.5 for i in range(nrow)])
ax.set_yticklabels([r[0] for r in ROWS], fontsize=8, color=S.INK)
ax.set_xticks([cx(j) + 0.5 for j in range(ncol)])
ax.set_xticklabels(COLS, fontsize=8, color=S.INK)
ax.xaxis.set_ticks_position("top")
ax.tick_params(axis="x", pad=2)

# column-group labels
ax.text((ncol - 1) / 2, -0.42, "one-issue types (315 questions)", ha="center", va="bottom",
        fontsize=7, color=S.MUTED)
ax.text(cx(ncol - 1) + 0.5, -0.42, "72 questions", ha="center", va="bottom", fontsize=7,
        color=S.MUTED)

S.save(fig, os.path.join(HERE, "fig4-by-type"))
