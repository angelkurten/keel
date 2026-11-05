#!/usr/bin/env bash
# Rebuild the six paper figures (vector PDFs) and their PNG twins.
# Charts: matplotlib via uv.  Diagrams: TikZ via tectonic; PNG twins via qlmanage (macOS).
set -euo pipefail
cd "$(dirname "$0")"

for f in fig3-tenant-accuracy fig4-by-type fig5-cost fig6-poisoned; do
  uv run --with matplotlib python "$f.py"
done

for f in fig1-architecture fig2-bitemporal; do
  tectonic "$f.tex"
  if command -v qlmanage >/dev/null; then
    qlmanage -t -s 1800 -o . "$f.pdf" >/dev/null 2>&1 && mv -f "$f.pdf.png" "$f.png"
  fi
done

ls -la fig*.pdf
