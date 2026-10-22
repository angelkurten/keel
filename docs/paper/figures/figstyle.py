"""Shared style for the paper's matplotlib figures.

One accent (Keel) and greys for everything else; ink tokens for text; hairline chrome.
Fonts are embedded as TrueType (Type 42) so the PDFs stay text-searchable in LaTeX.
"""
from __future__ import annotations

import matplotlib as mpl
from matplotlib.path import Path
from matplotlib.patches import PathPatch

# Marks
KEEL = "#2a78d6"        # accent, categorical slot 1
KEEL_LIGHT = "#86b6ef"  # same hue, lighter step (second bar of a pair)
GREY_DARK = "#64625d"   # API / first grey series
GREY_MID = "#a3a198"    # RAG / second grey series
GREY_LIGHT = "#dcdad2"  # Closed book / third grey series

# Ink and chrome
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#ffffff"

# Sequential blue ramp (one hue, light to dark), palette steps 100..700
SEQ_BLUE = ["#dfebfa", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

WIDTH_IN = 5.5


def apply() -> None:
    mpl.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.labelcolor": INK2,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "xtick.labelcolor": INK2,
        "ytick.labelcolor": INK2,
        "xtick.color": AXIS,
        "ytick.color": AXIS,
        "xtick.major.width": 0.5,
        "ytick.major.width": 0.5,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "legend.fontsize": 7.5,
        "legend.frameon": False,
        "legend.handlelength": 1.0,
        "legend.handleheight": 0.8,
        "legend.handletextpad": 0.5,
        "legend.columnspacing": 1.4,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": GRID,
        "grid.linewidth": 0.5,
        "grid.linestyle": "-",
        "lines.linewidth": 1.0,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "text.color": INK,
    })


def save(fig, out: str, width_in: float = WIDTH_IN, png_dpi: int = 220) -> None:
    """Save PDF + PNG twin with the tight bounding box exactly width_in wide.

    savefig(bbox_inches="tight") crops to the artists, so the page width would depend
    on label lengths; LaTeX would then rescale the figure and with it the font sizes.
    Here the figure is widened or narrowed until the cropped page is width_in, so an
    \\includegraphics[width=5.5in] shows the fonts at the size they were set.
    """
    import io
    import re

    def saved_size():
        buf = io.BytesIO()
        fig.savefig(buf, format="pdf")
        m = re.search(rb"/MediaBox\s*\[\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)\s*\]", buf.getvalue())
        return float(m.group(1)) / 72, float(m.group(2)) / 72

    for _ in range(6):
        tight_w, tight_h = saved_size()
        if abs(tight_w - width_in) < 0.003:
            break
        w, h = fig.get_size_inches()
        fig.set_size_inches(w + (width_in - tight_w), h)
    fig.savefig(out + ".pdf")
    fig.savefig(out + ".png", dpi=png_dpi)
    print(f"wrote {out}.pdf ({tight_w:.2f} x {tight_h:.2f} in)")


def rounded_bar(ax, x, y0, w, h, color, radius_pt=2.5, horizontal=False):
    """A bar with rounded data-end corners and a square baseline end.

    (x, y0) is the baseline corner; w is the extent along the category axis and
    h the value extent.  radius_pt is in points and is converted to data units on
    each axis, so the corners stay round regardless of axis scale.  Axis limits
    must be set before calling.
    """
    if h <= 0:
        return None
    fig = ax.figure
    # points -> pixels -> data units, per axis
    px = radius_pt * fig.dpi / 72.0
    inv = ax.transData.inverted()
    p0 = inv.transform((0, 0))
    p1 = inv.transform((px, px))
    rx, ry = abs(p1[0] - p0[0]), abs(p1[1] - p0[1])
    rx, ry = min(rx, w / 2), min(ry, h / 2)
    # CURVE3 = one control point then one end point (quadratic corner)
    if horizontal:
        # grows along x from the baseline at x; w is the value extent, h the thickness
        verts = [
            (x, y0), (x + w - rx, y0),
            (x + w, y0), (x + w, y0 + ry),            # bottom-right corner
            (x + w, y0 + h - ry),
            (x + w, y0 + h), (x + w - rx, y0 + h),    # top-right corner
            (x, y0 + h), (x, y0),
        ]
        codes = [Path.MOVETO, Path.LINETO, Path.CURVE3, Path.CURVE3, Path.LINETO,
                 Path.CURVE3, Path.CURVE3, Path.LINETO, Path.CLOSEPOLY]
    else:
        # grows along y from the baseline at y0; w is the thickness, h the value extent
        verts = [
            (x, y0), (x + w, y0), (x + w, y0 + h - ry),
            (x + w, y0 + h), (x + w - rx, y0 + h),    # top-right corner
            (x + rx, y0 + h),
            (x, y0 + h), (x, y0 + h - ry),            # top-left corner
            (x, y0),
        ]
        codes = [Path.MOVETO, Path.LINETO, Path.LINETO, Path.CURVE3, Path.CURVE3,
                 Path.LINETO, Path.CURVE3, Path.CURVE3, Path.CLOSEPOLY]
    patch = PathPatch(Path(verts, codes), facecolor=color, edgecolor="none", linewidth=0)
    ax.add_patch(patch)
    return patch
