import pathlib
import re

import fitz

KEY = re.compile(r"(显示|屏幕|屏|寸|inch|分辨率|像素|LCD|HDMI)")

pdfs = [p for p in pathlib.Path(".").glob("*.pdf")]
for pdf in pdfs:
    try:
        doc = fitz.open(pdf)
    except Exception as exc:
        print(f"!! {pdf.name}: {exc}")
        continue
    print("=" * 70)
    print(f"{pdf.name}  pages={doc.page_count}")
    for i, page in enumerate(doc):
        text = page.get_text()
        if not KEY.search(text):
            continue
        lines = [ln.strip() for ln in text.splitlines()]
        hits = [ln for ln in lines if ln and KEY.search(ln)]
        if not hits:
            continue
        print(f"--- p{i + 1} ---")
        for ln in hits[:20]:
            print("   ", ln)
    doc.close()
