"""Figure 5: accuracy against USD per correct answer on tenant-wide questions (log x).

Data: cost per correct answer from full-run-results.md ("Cost per correct answer", tenant rows);
      accuracies from tenant-ci.json means.
Run:  uv run --with matplotlib python fig5-cost.py
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

DATA = os.path.join(HERE, "tenant-ci.json")
acc = {(m, s): mean for m, s, n, mean, lo, hi in json.loads(Path(DATA).read_text())}
nq = {(m, s): n for m, s, n, mean, lo, hi in json.loads(Path(DATA).read_text())}

# USD per correct answer, tenant-wide questions (full-run-results.md)
COST = {
    ("gpt-6-luna", "keel"): 0.0037, ("gpt-6-luna", "api"): 0.0069, ("gpt-6-luna", "rag"): 0.11,
    ("claude-haiku-5.5", "keel"): 0.0028, ("claude-haiku-5.5", "api"): 0.0040,
    ("claude-haiku-5.5", "rag"): 0.028,
    ("claude-sonnet-5.5", "keel"): 0.040, ("claude-sonnet-5.5", "api"): 0.073,
    ("claude-sonnet-5.5", "rag"): 1.12,
}
AGENT = {"gpt-6-luna": "Luna", "claude-haiku-5.5": "Haiku", "claude-sonnet-5.5": "Sonnet"}
SYSTEMS = [("keel", "Keel", S.KEEL, "o"), ("api", "API", S.GREY_DARK, "s"),
           ("rag", "RAG", S.GREY_MID, "^")]
# label side per point, chosen to avoid collisions
SIDE = {
    ("gpt-6-luna", "keel"): "right", ("claude-haiku-5.5", "keel"): "left",
    ("claude-sonnet-5.5", "keel"): "right",
    ("gpt-6-luna", "api"): "right", ("claude-haiku-5.5", "api"): "left",
    ("claude-sonnet-5.5", "api"): "right",
    ("gpt-6-luna", "rag"): "right", ("claude-haiku-5.5", "rag"): "left",
    ("claude-sonnet-5.5", "rag"): "left",
}

fig, ax = plt.subplots(figsize=(S.WIDTH_IN, 3.1))
ax.set_xscale("log")
ax.set_xlim(0.0015, 2.2)
ax.set_ylim(0, 1.05)
ax.set_xticks([0.001, 0.01, 0.1, 1])
ax.set_xticklabels(["0.001", "0.01", "0.1", "1"])
ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
ax.set_yticklabels(["0", "0.25", "0.50", "0.75", "1.00"])
ax.grid(True)
ax.set_axisbelow(True)
ax.set_xlabel("USD per correct answer")
ax.set_ylabel("Accuracy")

for sid, slabel, color, marker in SYSTEMS:
    for mid, mlabel in AGENT.items():
        x, y = COST[(mid, sid)], acc[(mid, sid)]
        ax.scatter([x], [y], s=42, marker=marker, color=color, edgecolor=S.SURFACE,
                   linewidth=1.0, zorder=3)
        text = mlabel + ("†" if nq[(mid, sid)] < 72 else "")
        side = SIDE[(mid, sid)]
        dx = 7 if side == "right" else -7
        ax.annotate(text, (x, y), xytext=(dx, 0), textcoords="offset points",
                    ha="left" if side == "right" else "right", va="center", fontsize=7.5,
                    color=S.INK if sid == "keel" else S.INK2,
                    fontweight="bold" if sid == "keel" else "normal")

handles = [Line2D([], [], marker=m, color=c, markeredgecolor=S.SURFACE, markersize=6,
                  linestyle="none", label=l) for _, l, c, m in SYSTEMS]
ax.legend(handles=handles, ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0), borderaxespad=0)

DAGGER = "†"
ax.text(0, -0.2, DAGGER + "34 of 72 questions", transform=ax.transAxes, fontsize=7,
        color=S.MUTED, ha="left", va="top")

S.save(fig, os.path.join(HERE, "fig5-cost"))
