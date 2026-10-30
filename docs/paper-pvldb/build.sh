#!/usr/bin/env bash
# Build the PVLDB submission (main.pdf) and its supplemental material (supplement.pdf).
# Each cites the other's labels through xr, so both compile twice. Figures come from ../paper/figures.
set -euo pipefail
cd "$(dirname "$0")"
for _ in 1 2; do
  for f in supplement main; do tectonic -k --keep-logs "$f.tex" 2>/dev/null; done
done
uv run -q --with pypdf python pages.py main.pdf REFERENCES
