import fitz

doc = fitz.open("智能+赛道_赛规.pdf")
for i in (26, 27, 31, 32):
    if i < doc.page_count:
        print("=" * 70)
        print(f"### page {i + 1}")
        print(doc[i].get_text())
doc.close()
