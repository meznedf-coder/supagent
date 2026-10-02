"""The documentation exports (supagent.export): Markdown pages as Word (.docx) and PDF, an image as a one-page PDF.
The files are read back here without their writer's help: the PDF through its cross-reference table (every object
where the table says), its pages' text in order, its table of contents against where the pages really start, its
bookmarks and links; the .docx as a zip of XML parts that must all parse."""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

import pytest

from supagent import export as E

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MD = """# Billing

The **billing** run starts at *02:00* and reads `orders` → see [the wiki](https://wiki.example.com/x?a=1&b=2).

1. First
2. Second, in parts:
    - part *a*
    - part b
3. Third

| Job | Count | Status |
|:----|------:|:------:|
| é à ç € | 1,234 | OK |

```sql
SELECT 1 FROM t WHERE x = 'a';
```

> A quote with **bold**.

---

[nothing to run](javascript:alert(1)) <script>alert(2)</script>
"""


# --------------------------------------------------------------------------------------------- #
# reading the files back
# --------------------------------------------------------------------------------------------- #
def pdf_objects(data: bytes) -> dict[int, bytes]:
    """Every object of a PDF, found where its cross-reference table says (which checks the table too)."""
    assert data.startswith(b"%PDF-1.4\n") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
    assert data[start:start + 5] == b"xref\n"
    m = re.match(rb"xref\n0 (\d+)\n", data[start:])
    count = int(m.group(1))
    table = data[start + m.end():start + m.end() + 20 * count]
    offsets = [int(table[i * 20:i * 20 + 10]) for i in range(count)]
    objs = {}
    for i in range(1, count):
        head = b"%d 0 obj\n" % i
        assert data[offsets[i]:offsets[i] + len(head)] == head, f"object {i} is not where the xref says"
        end = data.index(b"\nendobj\n", offsets[i])
        objs[i] = data[offsets[i] + len(head):end]
    return objs


def pdf_stream(obj: bytes) -> bytes:
    body = obj[obj.index(b"stream\n") + 7:obj.rindex(b"\nendstream")]
    return zlib.decompress(body) if b"/FlateDecode" in obj[:200] else body


def pdf_unescape(s: bytes) -> str:
    out, i = bytearray(), 0
    while i < len(s):
        if s[i:i + 1] == b"\\":
            nxt = s[i + 1:i + 2]
            out += {b"n": b"\n", b"r": b"\r"}.get(nxt, nxt)
            i += 2
        else:
            out += s[i:i + 1]
            i += 1
    return out.decode("cp1252")


def pdf_pages(data: bytes) -> tuple[list[str], dict[int, bytes], list[int]]:
    """(the text of each page in drawing order, the objects, the page object numbers)."""
    objs = pdf_objects(data)
    root = int(re.search(rb"/Root (\d+) 0 R", data[data.rindex(b"trailer"):]).group(1))
    pages_id = int(re.search(rb"/Pages (\d+) 0 R", objs[root]).group(1))
    kids = [int(k) for k in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[([^\]]*)\]", objs[pages_id]).group(1))]
    assert int(re.search(rb"/Count (\d+)", objs[pages_id]).group(1)) == len(kids)
    texts = []
    for k in kids:
        content = int(re.search(rb"/Contents (\d+) 0 R", objs[k]).group(1))
        ops = pdf_stream(objs[content])
        texts.append("\n".join(pdf_unescape(m) for m in re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", ops)))
    return texts, objs, kids


def docx_parts(data: bytes) -> dict[str, ET.Element]:
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.namelist()[0] == "[Content_Types].xml"
    return {n: ET.fromstring(z.read(n)) for n in z.namelist() if n.endswith((".xml", ".rels"))}


def doc(pages_per_section: int = 3, sections: int = 2, extra: str = "") -> E.Document:
    secs = []
    for si in range(1, sections + 1):
        pages = []
        for pi in range(1, pages_per_section + 1):
            body = "\n\n".join(f"P{si}{pi}-{k:02d} " + "The night batch reads the orders and writes the ledger. " * 6
                               for k in range(12))
            pages.append(E.Page(f"Page {si}.{pi}", f"# Page {si}.{pi}\n\n{body}\n\n{extra}", note="AI-written · v1",
                                sources=[f"doc:{si}{pi} Runbook"]))
        secs.append(E.Section(f"Section {si}", pages))
    return E.Document("Context (test) \\ export", secs, subtitle="Documentation", meta=["Exported by admin"])


# --------------------------------------------------------------------------------------------- #
# the Markdown, read once for both writers
# --------------------------------------------------------------------------------------------- #
def test_the_markdown_becomes_blocks():
    blocks = E.parse_markdown(MD)
    kinds = [type(b).__name__ for b in blocks]
    assert kinds == ["Heading", "Para", "ListBlock", "Table", "Code", "Quote", "Rule", "Para"]
    para = blocks[1]
    assert [(r.text, r.bold, r.italic, r.code, r.href) for r in para.runs] == [
        ("The ", False, False, False, None), ("billing", True, False, False, None),
        (" run starts at ", False, False, False, None), ("02:00", False, True, False, None),
        (" and reads ", False, False, False, None), ("orders", False, False, True, None),
        (" → see ", False, False, False, None), ("the wiki", False, False, False, "https://wiki.example.com/x?a=1&b=2"),
        (".", False, False, False, None)]
    lst = blocks[2]
    assert lst.ordered and lst.start == 1 and len(lst.items) == 3
    nested = lst.items[1][1]
    assert isinstance(nested, E.ListBlock) and not nested.ordered and len(nested.items) == 2
    table = blocks[3]
    assert [E._plain(c) for c in table.header] == ["Job", "Count", "Status"]
    assert table.aligns == ["left", "right", "center"] and E._plain(table.rows[0][0]) == "é à ç €"
    assert blocks[4].text == "SELECT 1 FROM t WHERE x = 'a';"
    assert E._plain(blocks[5].blocks[0].runs) == "A quote with bold."
    assert "alert(2)" not in E._plain(blocks[7].runs)                # a script's text is never content
    assert E.parse_markdown("3. three\n4. four\n")[0].start == 3


def test_a_first_heading_that_repeats_the_title_is_left_out():
    p = E.Page("Billing", MD)
    assert type(E.page_blocks(p)[0]).__name__ == "Para"
    assert type(E.page_blocks(E.Page("Other title", MD))[0]).__name__ == "Heading"


# --------------------------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------------------------- #
def test_the_pdf_reads_back_whole_and_in_order():
    d = doc(extra=MD)
    data = E.to_pdf(d)
    texts, objs, kids = pdf_pages(data)
    assert len(texts) > 8
    assert "Context (test) \\ export" in texts[0] and "Exported by admin" in texts[0]
    assert "Page 1 of" not in texts[0]                                 # no footer on the cover
    assert all(f"Page {i + 1} of {len(texts)}" in texts[i] for i in range(1, len(texts)))
    everything = "\n".join(texts)
    markers = re.findall(r"\bP\d\d-\d\d\b", everything)
    assert markers == [f"P{s}{p}-{k:02d}" for s in (1, 2) for p in (1, 2, 3) for k in range(12)]
    # the table of contents: each entry's number is the page where it starts (sections and pages: a new page)
    toc = dict(re.findall(r"^((?:Section|Page) [\d.]+)\n[ .]+\n(\d+)$", texts[1], re.M))
    assert len(toc) == 2 + 6
    for title, number in toc.items():
        first = texts[int(number) - 1].split("\n")
        assert title in first[:3], (title, number, first[:3])
    # the bookmarks: one per section with its pages under it
    catalog = objs[int(re.search(rb"/Root (\d+) 0 R", data[data.rindex(b"trailer"):]).group(1))]
    outline = objs[int(re.search(rb"/Outlines (\d+) 0 R", catalog).group(1))]
    assert re.search(rb"/Count 8\b", outline)
    titles = [bytes.fromhex(h.decode()).decode("utf-16-be") for h in re.findall(rb"/Title <FEFF([0-9A-F]*)>", data)]
    assert titles[0] == d.title                                        # the document's (its info)
    assert sorted(titles[1:]) == ["Page 1.1", "Page 1.2", "Page 1.3", "Page 2.1", "Page 2.2", "Page 2.3", "Section 1",
                                  "Section 2"]
    # links: the web ones (never a javascript: one), the table of contents' entries to their pages
    assert b"/URI (https://wiki.example.com/x?a=1&b=2)" in data
    assert b"javascript" not in data
    assert len(re.findall(rb"/Dest \[(\d+) 0 R /XYZ", objs[kids[1]])) == 8
    # the text: accents, euro, quotes kept (WinAnsi); an arrow as ->; bold, italic, code fonts used
    assert "é à ç €" in everything and "-> see" in everything and "the wiki" in everything
    streams = [pdf_stream(objs[int(re.search(rb"/Contents (\d+)", objs[k]).group(1))]) for k in kids]
    fonts = {f for ops in streams for f in re.findall(rb"/(F\d) [\d.]+ Tf", ops)}
    assert {b"F1", b"F2", b"F3", b"F5"} <= fonts
    assert b"/BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255" in data


def test_characters_outside_winansi_never_break_the_pdf():
    odd = "Emoji 🚀, CJK 中文, Greek λ, arrows → ← ⇒, ≥ ≤ ≠ ✓ ✗, combining é, control \x07\x1b, bom ﻿, tab\tend"
    d = E.Document(odd, [E.Section("S " + odd, [E.Page("P " + odd, odd + "\n\n```\n" + odd + "\n```\n",
                                                       note=odd, sources=[odd])])], meta=[odd])
    texts, _objs, _kids = pdf_pages(E.to_pdf(d))
    squeezed = "".join("\n".join(texts).split())                       # lines wrap where they must
    folded = "Emoji ?, CJK ??, Greek ?, arrows -> <- =>, >= <= != v x, combining é, control , bom , tab    end"
    assert squeezed.count("".join(folded.split())) >= 6                 # cover, section, page, note, text, code, source
    assert E._ansi("ł ő ﬁ ½ µ") == "? o fi ½ µ"                         # ł has no plain form; the others do


def test_tables_wrap_and_repeat_their_header_across_pages():
    rows = "\n".join(f"| R{i:02d} | " + "word " * 40 + f"| {i * 3} |" for i in range(80))
    wide = "| " + " | ".join(f"Column {c}" for c in range(8)) + " |\n|" + "---|" * 8 + "\n" + \
           "| " + " | ".join("x" * 70 if c == 3 else f"v{c}" for c in range(8)) + " |\n"
    d = E.Document("Tables", [E.Section("S", [E.Page("Long", f"| Key | Text | N |\n|---|---|--:|\n{rows}\n\n{wide}")])])
    texts, _o, _k = pdf_pages(E.to_pdf(d))
    with_rows = [t for t in texts if re.search(r"\bR\d\d\b", t)]
    assert len(with_rows) >= 3 and all("Key" in t and "Text" in t for t in with_rows)
    assert re.findall(r"\bR\d\d\b", "\n".join(texts)) == [f"R{i:02d}" for i in range(80)]
    assert "x" * 70 in "".join("".join(texts).split())                  # a word wider than its column: cut, kept
    widths = E._share([20.0, 30.0, 400.0, 500.0], 300.0)
    assert widths[:2] == [20.0, 30.0] and widths[2] == widths[3] == 125.0   # the narrow ones whole, the rest shared
    assert E._share([10.0, 30.0], 80.0) == [20.0, 60.0]


def test_a_row_taller_than_a_page_and_a_huge_code_block_go_on_over_pages():
    tall = "| A | B |\n|---|---|\n| " + "<br>".join(f"line {i}" for i in range(140)) + " | x |\n"
    code = "```\n" + "\n".join(f"code line {i}" for i in range(200)) + "\n```\n"
    d = E.Document("Big", [E.Section("S", [E.Page("Tall", tall + "\n" + code)])])
    texts, _o, _k = pdf_pages(E.to_pdf(d))
    everything = "\n".join(texts)
    assert re.findall(r"line (\d+)\n", everything.replace("code line", "c"))[:140] == [str(i) for i in range(140)]
    assert re.findall(r"code line (\d+)", everything) == [str(i) for i in range(200)]


def test_empty_documents_and_sections_still_make_valid_files():
    for d in (E.Document("Nothing", []), E.Document("One", [E.Section("Empty section", [])]),
              E.Document("Blank", [E.Section("S", [E.Page("Blank page", "")])], toc=False)):
        texts, _o, _k = pdf_pages(E.to_pdf(d))
        assert texts[0].startswith(d.title)
        parts = docx_parts(E.to_docx(d))
        assert parts["word/document.xml"].find(f"{W}body") is not None
    texts, _o, _k = pdf_pages(E.to_pdf(E.Document("Nothing", [])))
    assert "This document has no page yet." in texts[-1]


# --------------------------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------------------------- #
def test_the_docx_is_a_complete_word_package():
    d = doc(pages_per_section=2, extra=MD + "\n5. five\n6. six\n\nControl\x07char and 🚀 kept.")
    parts = docx_parts(E.to_docx(d))
    for name in ("[Content_Types].xml", "_rels/.rels", "docProps/core.xml", "docProps/app.xml", "word/document.xml",
                 "word/_rels/document.xml.rels", "word/styles.xml", "word/numbering.xml", "word/settings.xml",
                 "word/footer1.xml"):
        assert name in parts, name
    body = parts["word/document.xml"].find(f"{W}body")
    paras = body.findall(f"{W}p")

    def style(p):
        s = p.find(f"{W}pPr/{W}pStyle")
        return s.get(f"{W}val") if s is not None else None

    def text(p):
        return "".join(t.text or "" for t in p.iter(f"{W}t"))

    assert [text(p) for p in paras if style(p) == "Heading1"] == ["Section 1", "Section 2"]
    assert [text(p) for p in paras if style(p) == "Heading2"] == ["Page 1.1", "Page 1.2", "Page 2.1", "Page 2.2"]
    assert "Billing" in [text(p) for p in paras if style(p) == "Heading3"]
    assert [text(p) for p in paras if style(p) == "Title"] == ["Context (test) \\ export"]
    # the table of contents: a field whose entries link to the headings' bookmarks
    instr = "".join(x.text for x in body.iter(f"{W}instrText"))
    assert 'TOC \\o "1-2"' in instr
    anchors = [h.get(f"{W}anchor") for h in body.iter(f"{W}hyperlink") if h.get(f"{W}anchor")]
    marks = {b.get(f"{W}name") for b in body.iter(f"{W}bookmarkStart")}
    assert len(anchors) == 6 and set(anchors) <= marks
    # lists: bullets share numId 1; each numbered list restarts (its own numId, its start)
    numbering = parts["word/numbering.xml"]
    starts = {n.get(f"{W}numId"): n.find(f"{W}lvlOverride/{W}startOverride").get(f"{W}val")
              for n in numbering.findall(f"{W}num") if n.find(f"{W}lvlOverride") is not None}
    assert "5" in starts.values() and "1" in starts.values()
    levels = {p.find(f"{W}pPr/{W}numPr/{W}ilvl").get(f"{W}val") for p in paras
              if p.find(f"{W}pPr/{W}numPr") is not None}
    assert levels == {"0", "1"}
    # tables: a header row that repeats, shaded; the alignment of the columns
    tables = body.findall(f"{W}tbl")
    assert tables and tables[0].find(f"{W}tr/{W}trPr/{W}tblHeader") is not None
    assert any(j.get(f"{W}val") == "right" for j in tables[0].iter(f"{W}jc"))
    # links: external relationships, never a javascript: one
    rels = parts["word/_rels/document.xml.rels"]
    targets = {r.get("Target"): r.get("TargetMode") for r in rels}
    assert targets.get("https://wiki.example.com/x?a=1&b=2") == "External"
    assert not any("javascript" in (t or "") for t in targets)
    # every character kept, except what XML cannot hold
    alltext = "".join(t.text or "" for t in body.iter(f"{W}t"))
    assert "é à ç €" in alltext and "→ see" in alltext and "Controlchar and 🚀 kept." in alltext
    footer = parts["word/footer1.xml"]
    assert {"PAGE", "NUMPAGES"} <= {x.text.strip() for x in footer.iter(f"{W}instrText")}
    assert parts["docProps/core.xml"].find("{http://purl.org/dc/elements/1.1/}title").text == "Context (test) \\ export"


# --------------------------------------------------------------------------------------------- #
# an image (the system map)
# --------------------------------------------------------------------------------------------- #
def _png(w: int, h: int, dpi: int | None = None, alpha: bool = True) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGBA" if alpha else "RGB", (w, h), (255, 255, 255, 0) if alpha else (250, 250, 250))
    ImageDraw.Draw(im).rectangle([10, 10, w - 10, h - 10], outline=(30, 110, 140), width=3)
    buf = io.BytesIO()
    im.save(buf, "PNG", **({"dpi": (dpi, dpi)} if dpi else {}))
    return buf.getvalue()


@pytest.mark.parametrize("w,h,dpi,page", [(600, 400, None, (841.89, 595.28)), (1600, 900, None, (1190.55, 841.89)),
                                          (3200, 1800, 192, (1190.55, 841.89)), (5000, 3000, None, (1683.78, 1190.55))])
def test_an_image_on_one_landscape_page_as_large_as_it_needs(w, h, dpi, page):
    data = E.image_pdf(_png(w, h, dpi), "System map", note="Exported 2 Oct 2026")
    texts, objs, kids = pdf_pages(data)
    assert len(kids) == 1 and texts[0] == "System map\nExported 2 Oct 2026"
    box = [float(x) for x in re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", objs[kids[0]]).groups()]
    assert box == list(page)
    image = next(o for o in objs.values() if b"/Subtype /Image" in o)
    assert b"/Filter /DCTDecode" in image and b"/Width %d /Height %d" % (w, h) in image
    jpeg = image[image.index(b"stream\n") + 7:image.rindex(b"\nendstream")]
    from PIL import Image

    got = Image.open(io.BytesIO(jpeg))
    assert got.format == "JPEG" and got.size == (w, h) and got.getpixel((w // 2, h // 2))[0] > 240   # on white
