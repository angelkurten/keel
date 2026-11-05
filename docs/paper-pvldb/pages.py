"""Print the page count and the page where each marker string first appears."""
import sys

from pypdf import PdfReader

r = PdfReader(sys.argv[1])
print("pages", len(r.pages))
texts = [p.extract_text() or "" for p in r.pages]
for m in sys.argv[2:]:
    hit = next((i + 1 for i, t in enumerate(texts) if m in t), None)
    print(f"{m!r}: page {hit}")
