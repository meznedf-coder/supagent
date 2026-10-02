"""Exports of the documentation: pages of Markdown (the Context) as a Word (.docx) or a PDF file, and an image (the
system map) as a one-page PDF. Written with the standard library only, plus Pillow (Superset ships it) for the image:
nothing to install.

    doc = Document("Context", [Section("Functional", [Page("Billing", "# Billing\\n...", note="AI-written")])])
    to_docx(doc), to_pdf(doc)                                   # bytes: DOCX_MIME, PDF_MIME
    image_pdf(png_bytes, "System map", note="Exported 2 Oct 2026")

The Markdown is read once (Markdown -> HTML -> a small block model: headings, paragraphs with bold, italic, code and
links, lists, tables, code blocks, quotes, rules) and both writers draw that model. The PDF uses the standard fonts
(Helvetica, Courier): its text is the WinAnsi (Windows-1252) set, so accents, the euro sign, dashes and quotes are kept,
other characters become their plain equivalent (an arrow ->, >=) or "?". The .docx keeps every character.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Union
from urllib.parse import quote

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


# --------------------------------------------------------------------------------------------- #
# what is exported
# --------------------------------------------------------------------------------------------- #
@dataclass
class Page:
    title: str
    markdown: str
    note: str = ""                       # a line under the title, e.g. "AI-written · updated 1 Oct 2026"
    sources: list[str] = field(default_factory=list)


@dataclass
class Section:
    title: str
    pages: list[Page]


@dataclass
class Document:
    title: str
    sections: list[Section]
    subtitle: str = ""
    meta: list[str] = field(default_factory=list)      # lines of the cover, e.g. "Exported 2 Oct 2026 by admin"
    toc: bool = True


# --------------------------------------------------------------------------------------------- #
# the block model, read from the Markdown
# --------------------------------------------------------------------------------------------- #
@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None

    def style(self) -> tuple:
        return (self.bold, self.italic, self.code, self.href)


@dataclass
class Heading:
    level: int                            # 1-4
    runs: list[Run]


@dataclass
class Para:
    runs: list[Run]


@dataclass
class ListBlock:
    ordered: bool
    items: list[list[Any]]               # each item: its blocks (its text first, nested lists after)
    start: int = 1


@dataclass
class Table:
    header: list[list[Run]]               # [] when the table has no header row
    rows: list[list[list[Run]]]
    aligns: list[str]                     # left | center | right, per column


@dataclass
class Code:
    text: str


@dataclass
class Quote:
    blocks: list[Any]


@dataclass
class Rule:
    pass


Block = Union[Heading, Para, ListBlock, Table, Code, Quote, Rule]


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag, self.attrs, self.children = tag, attrs, []


VOID = {"br", "hr", "img", "input", "meta", "link", "col", "wbr", "area", "base", "source"}
SKIP = {"script", "style", "head", "title", "template", "noscript"}


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.skip and tag not in SKIP:
            self.stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.stack[-1].children.append(data)


def parse_markdown(text: str) -> list[Any]:
    """The blocks of a Markdown text (the extensions of the pages: tables, fenced code, sane lists)."""
    import markdown

    return parse_html(markdown.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"]))


def parse_html(html: str) -> list[Any]:
    tree = _Tree()
    tree.feed(html or "")
    tree.close()
    return _blocks(tree.root.children)


HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 4, "h6": 4}
BLOCKS = {"p", "ul", "ol", "li", "table", "pre", "blockquote", "hr", "div", "section", "article", "header", "footer",
          "nav", "aside", "main", "figure", "figcaption", "details", "summary", "dl", "dt", "dd", "thead", "tbody",
          "tfoot", "tr", "th", "td", "caption", "form", "fieldset", *HEADINGS}


def _blocks(nodes: list[Any]) -> list[Any]:
    out: list[Any] = []
    inline: list[Any] = []

    def flush() -> None:
        runs = _tidy(_inline(inline))
        if runs:
            out.append(Para(runs))
        inline.clear()

    for n in nodes:
        if isinstance(n, str) or n.tag not in BLOCKS:
            inline.append(n)
            continue
        flush()
        t = n.tag
        if t in HEADINGS:
            runs = _tidy(_inline(n.children))
            if runs:
                out.append(Heading(HEADINGS[t], runs))
        elif t in ("p", "dt", "summary", "figcaption", "caption"):
            runs = _tidy(_inline(n.children, bold=t == "dt"))
            if runs:
                out.append(Para(runs))
        elif t in ("ul", "ol"):
            lst = _list(n)
            if lst.items:
                out.append(lst)
        elif t == "li":                                    # an item outside a list
            out.append(ListBlock(False, [_blocks(n.children)]))
        elif t == "table":
            table = _table(n)
            if table is not None:
                out.append(table)
        elif t == "pre":
            text = _raw(n)
            text = text[1:] if text.startswith("\n") else text
            out.append(Code(text.rstrip("\n")))
        elif t in ("blockquote", "dd"):
            inner = _blocks(n.children)
            if inner:
                out.append(Quote(inner))
        elif t == "hr":
            out.append(Rule())
        else:                                              # div, section, stray table parts...: what is inside
            out.extend(_blocks(n.children))
    flush()
    return out


def _inline(nodes: list[Any], bold: bool = False, italic: bool = False, code: bool = False,
            href: str | None = None) -> list[Run]:
    out: list[Run] = []
    for n in nodes:
        if isinstance(n, str):
            text = re.sub(r"[\r\n\t]+", " ", n) if code else re.sub(r"\s+", " ", n)
            if text:
                out.append(Run(text, bold, italic, code, href))
            continue
        t = n.tag
        if t == "br":
            out.append(Run("\n", bold, italic, code, href))
            continue
        if t == "img":
            alt = " ".join((n.attrs.get("alt") or "").split())
            if alt:
                out.append(Run(f"[{alt}]", bold, italic, code, href))
            continue
        if t == "hr":
            continue
        if t in BLOCKS and out:                            # a block inside a cell or a heading: on its own line
            out.append(Run("\n", bold, italic, code, href))
        b, i, c, h = bold, italic, code, href
        if t in ("strong", "b"):
            b = True
        elif t in ("em", "i", "cite", "var", "dfn"):
            i = True
        elif t in ("code", "kbd", "samp", "tt"):
            c = True
        elif t == "a":
            h = (n.attrs.get("href") or "").strip() or h
        out.extend(_inline(n.children, b, i, c, h))
    return out


def _tidy(runs: list[Run]) -> list[Run]:
    """Runs of one paragraph: the same style together, white space as HTML shows it (one space, none at the ends or
    around a line break)."""
    merged: list[Run] = []
    for r in runs:
        text = r.text
        if not text:
            continue
        if not r.code:
            text = re.sub(r" *\n *", "\n", text)
            if merged and not merged[-1].code and merged[-1].text.endswith((" ", "\n")):
                text = text.lstrip(" ")
            if text.startswith("\n") and merged and not merged[-1].code:
                merged[-1].text = merged[-1].text.rstrip(" ")
        if not text:
            continue
        if merged and merged[-1].style() == r.style():
            merged[-1].text += text
        else:
            merged.append(Run(text, r.bold, r.italic, r.code, r.href))
    while merged:
        merged[0].text = merged[0].text.lstrip(" \n")
        if merged[0].text:
            break
        merged.pop(0)
    while merged:
        merged[-1].text = merged[-1].text.rstrip(" \n")
        if merged[-1].text:
            break
        merged.pop()
    return merged


def _list(n: _Node) -> ListBlock:
    try:
        start = int(n.attrs.get("start") or 1)
    except ValueError:
        start = 1
    items: list[list[Any]] = []
    for c in n.children:
        if isinstance(c, _Node) and c.tag == "li":
            items.append(_blocks(c.children))
        elif isinstance(c, _Node) and c.tag in ("ul", "ol"):     # a list straight in a list: the item before's
            if items:
                items[-1].append(_list(c))
            else:
                items.append([_list(c)])
        elif isinstance(c, str) and c.strip():
            items.append([Para([Run(" ".join(c.split()))])])
    return ListBlock(n.tag == "ol", items, start)


def _table(n: _Node) -> Table | None:
    rows: list[tuple[bool, list[_Node]]] = []

    def walk(node: _Node, head: bool) -> None:
        for c in node.children:
            if not isinstance(c, _Node):
                continue
            if c.tag == "tr":
                rows.append((head, [x for x in c.children if isinstance(x, _Node) and x.tag in ("td", "th")]))
            elif c.tag in ("thead", "tbody", "tfoot"):
                walk(c, c.tag == "thead")

    walk(n, False)
    rows = [r for r in rows if r[1]]
    if not rows:
        return None
    ncols = max(len(cells) for _h, cells in rows)
    header = rows[0][0] or all(c.tag == "th" for c in rows[0][1])

    def align(c: _Node) -> str:
        m = re.search(r"text-align:\s*(left|right|center)", c.attrs.get("style") or "")
        a = m.group(1) if m else (c.attrs.get("align") or "left").lower()
        return a if a in ("left", "center", "right") else "left"

    def cells_of(cells: list[_Node]) -> list[list[Run]]:
        return [_tidy(_inline(c.children)) for c in cells] + [[] for _ in range(ncols - len(cells))]

    aligns = [align(c) for c in rows[0][1]] + ["left"] * (ncols - len(rows[0][1]))
    body = rows[1:] if header else rows
    return Table(cells_of(rows[0][1]) if header else [], [cells_of(cells) for _h, cells in body], aligns)


def _raw(n: _Node) -> str:
    return "".join(c if isinstance(c, str) else ("\n" if c.tag == "br" else _raw(c)) for c in n.children)


def _plain(runs: list[Run]) -> str:
    return "".join(r.text for r in runs)


def page_blocks(page: Page) -> list[Any]:
    """A page's blocks, without a first heading that only repeats the page's title."""
    blocks = parse_markdown(page.markdown or "")
    if blocks and isinstance(blocks[0], Heading) and \
            " ".join(_plain(blocks[0].runs).split()).lower() == " ".join((page.title or "").split()).lower():
        blocks = blocks[1:]
    return blocks


def _safe_url(url: str | None) -> bool:
    return bool(url) and bool(re.match(r"(?i)^(https?://|mailto:)", url or ""))


# --------------------------------------------------------------------------------------------- #
# PDF: the standard fonts and their widths (Adobe's AFM metrics, WinAnsiEncoding, codes 32-255)
# --------------------------------------------------------------------------------------------- #
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350,
         556, 350, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350,
         350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667,
         278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333,
         400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611,
         667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
         722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
         556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
              975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
              333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350,
              556, 350, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350,
              350, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350, 500, 667,
              278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333,
              400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611,
              722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278,
              722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611,
              556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278,
              611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556]
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Helvetica-Oblique", "F4": "Helvetica-BoldOblique",
         "F5": "Courier", "F6": "Courier-Bold"}
_WIDTHS: dict[str, list[int] | None] = {"F1": _HELV, "F2": _HELV_BOLD, "F3": _HELV, "F4": _HELV_BOLD, "F5": None,
                                        "F6": None}

# characters outside WinAnsi: their plain equivalent (the rest: without its accent, or "?")
_FALLBACK = {
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>", "\u21d0": "<=", "\u21d4": "<=>", "\u2191": "^",
    "\u2193": "v", "\u279c": "->", "\u2794": "->", "\u27f6": "-->", "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u2248": "~", "\u2243": "~", "\u223c": "~", "\u2212": "-", "\u221e": "inf", "\u221a": "sqrt", "\u2211": "sum",
    "\u2206": "delta", "\u0394": "delta", "\u03c0": "pi", "\u03bc": "\u00b5", "\u2713": "v", "\u2714": "v",
    "\u2717": "x", "\u2718": "x", "\u2705": "[v]", "\u274c": "[x]", "\u26a0": "(!)", "\u2139": "(i)", "\u2605": "*",
    "\u2606": "*", "\u25cf": "\u2022", "\u25e6": "o", "\u25aa": "\u2022", "\u25a0": "#", "\u25a1": "[ ]",
    "\u25b6": ">", "\u25ba": ">", "\u25c0": "<", "\u2023": ">", "\u2219": "\u00b7", "\u22c5": "\u00b7",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2015": "\u2014", "\u2043": "-", "\u201b": "'", "\u2032": "'",
    "\u2035": "'", "\u201f": '"', "\u2033": '"', "\u2036": '"', "\u2502": "|", "\u2503": "|", "\u2551": "|",
    "\u2500": "-", "\u2501": "-", "\u2550": "=", "\u2116": "No.",
    **{c: "+" for c in "\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c"},
    **{chr(c): " " for c in (*range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000)},
}


def _ansi(text: str) -> str:
    """`text` in WinAnsi (Windows-1252): kept when it is there, else its plain equivalent, without its accent,
    or "?"; control and invisible characters dropped, tabs as four spaces."""
    try:
        text.encode("cp1252")
        if text.isprintable():
            return text
    except UnicodeEncodeError:
        pass
    text = unicodedata.normalize("NFC", text)               # e + a combining accent: one letter, kept when it can be
    out = []
    for ch in text:
        if ch == "\t":
            out.append("    ")
            continue
        if ch in _FALLBACK:
            out.append(_FALLBACK[ch])
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Mn", "Me"):
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            base.encode("cp1252")
            out.append(base or "?")
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def _width(text: str, font: str, size: float) -> float:
    table = _WIDTHS[font]
    data = text.encode("cp1252", errors="replace")
    if table is None:
        return len(data) * 0.6 * size
    return sum(table[b - 32] if b >= 32 else 0 for b in data) * size / 1000.0


def _font(bold: bool, italic: bool, code: bool) -> str:
    if code:
        return "F6" if bold else "F5"
    return {(False, False): "F1", (True, False): "F2", (False, True): "F3", (True, True): "F4"}[(bold, italic)]


def _n(v: float) -> bytes:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return (s if s not in ("-0", "") else "0").encode()


def _rgb(c: tuple[float, float, float]) -> bytes:
    return b"%s %s %s" % (_n(c[0]), _n(c[1]), _n(c[2]))


def _pdf_str(s: str) -> bytes:
    b = s.encode("cp1252", errors="replace")
    for raw, escaped in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)"), (b"\r", b"\\r"), (b"\n", b"\\n")):
        b = b.replace(raw, escaped)
    return b"(" + b + b")"


def _pdf_text_string(s: str) -> bytes:
    """A text string of the document (outline, info): UTF-16 with its byte order mark, any character."""
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode() + b">"


PW, PH = 595.28, 841.89          # A4 portrait
ML = MR = 51.0                   # 18 mm
MT, MB = 54.0, 62.0
CW = PW - ML - MR
TOP = PH - MT
FOOT_Y = 30.0
BODY = 10.5
LEAD = 14.6
INK = (0.12, 0.16, 0.2)
MUTED = (0.42, 0.47, 0.51)
QUOTE_INK = (0.29, 0.35, 0.4)
ACCENT = (0.12, 0.43, 0.55)
LINK = (0.1, 0.36, 0.68)
CODE_INK = (0.17, 0.21, 0.25)
INLINE_CODE = (0.42, 0.18, 0.16)
CODE_BG = (0.945, 0.957, 0.965)
HEAD_BG = (0.9, 0.93, 0.945)
BORDER = (0.74, 0.78, 0.81)
QUOTE_BAR = (0.63, 0.68, 0.72)


@dataclass
class _Frag:
    text: str            # WinAnsi already
    font: str
    size: float
    color: tuple
    href: str | None
    width: float


class _Canvas:
    """One page of the PDF: the operations of its content stream, its link areas, its images."""

    def __init__(self, footer: bool = True, size: tuple[float, float] = (PW, PH)) -> None:
        self.ops: list[bytes] = []
        self.links: list[tuple[float, float, float, float, Any]] = []
        self.images: list[tuple[str, int, int, bytes]] = []
        self.footer = footer
        self.size = size

    def text(self, x: float, y: float, s: str, font: str, size: float, color: tuple) -> None:
        if s:
            self.ops.append(b"BT /%s %s Tf %s rg 1 0 0 1 %s %s Tm %s Tj ET" % (
                font.encode(), _n(size), _rgb(color), _n(x), _n(y), _pdf_str(s)))

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple | None = None, stroke: tuple | None = None,
             lw: float = 0.5) -> None:
        if fill is not None:
            self.ops.append(b"q %s rg %s %s %s %s re f Q" % (_rgb(fill), _n(x), _n(y), _n(w), _n(h)))
        if stroke is not None:
            self.ops.append(b"q %s w %s RG %s %s %s %s re S Q" % (_n(lw), _rgb(stroke), _n(x), _n(y), _n(w), _n(h)))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: tuple, lw: float = 0.5) -> None:
        self.ops.append(b"q %s w %s RG %s %s m %s %s l S Q" % (_n(lw), _rgb(color), _n(x1), _n(y1), _n(x2), _n(y2)))


def _flow(runs: list[Run], max_w: float, size: float, color: tuple, bold: bool = False,
          italic: bool = False) -> list[list[_Frag]]:
    """The lines of a paragraph `max_w` points wide: words kept whole (a word wider than a line is cut), spaces
    between them, line breaks kept."""
    items: list[Any] = []                 # "\n", a space (_Frag), or a word (list of _Frag: no break inside)
    word: list[_Frag] = []

    def flush() -> None:
        if word:
            items.append(list(word))
            word.clear()

    for r in runs:
        font = _font(bold or r.bold, italic or r.italic, r.code)
        fsize = size * 0.9 if r.code else size
        col = LINK if (r.href and _safe_url(r.href)) else (INLINE_CODE if r.code else color)
        href = r.href if _safe_url(r.href) else None
        for piece in re.findall(r"\n| +|[^ \n]+", r.text):
            if piece == "\n":
                flush()
                items.append("\n")
            elif piece[0] == " ":
                flush()
                items.append(_Frag(" ", font, fsize, col, href, _width(" ", font, fsize)))
            else:
                text = _ansi(piece)
                if text:
                    word.append(_Frag(text, font, fsize, col, href, _width(text, font, fsize)))
    flush()
    lines: list[list[_Frag]] = []
    line: list[_Frag] = []
    used = 0.0
    space: _Frag | None = None
    for it in items:
        if isinstance(it, str):
            lines.append(line)
            line, used, space = [], 0.0, None
            continue
        if isinstance(it, _Frag):
            if line:
                space = it
            continue
        ww = sum(f.width for f in it)
        sw = space.width if (space is not None and line) else 0.0
        if line and used + sw + ww > max_w:
            lines.append(line)
            line, used, sw = [], 0.0, 0.0
        if not line and ww > max_w:                 # a word wider than the line: cut where it must be
            chunks = _cut(it, max_w)
            lines.extend(chunks[:-1])
            line, used, space = chunks[-1], sum(f.width for f in chunks[-1]), None
            continue
        if line and sw and space is not None:
            line.append(space)
            used += sw
        line.extend(it)
        used += ww
        space = None
    if line or not lines:
        lines.append(line)
    if lines == [[]]:
        return []
    return lines


def _cut(frags: list[_Frag], max_w: float) -> list[list[_Frag]]:
    out: list[list[_Frag]] = [[]]
    used = 0.0
    for f in frags:
        start = 0
        for i, ch in enumerate(f.text):
            cw = _width(ch, f.font, f.size)
            if used + cw > max_w and (out[-1] or i > start):
                if i > start:
                    out[-1].append(_Frag(f.text[start:i], f.font, f.size, f.color, f.href,
                                         _width(f.text[start:i], f.font, f.size)))
                out.append([])
                used, start = 0.0, i
            used += cw
        if start < len(f.text):
            rest = f.text[start:]
            out[-1].append(_Frag(rest, f.font, f.size, f.color, f.href, _width(rest, f.font, f.size)))
    return [c for c in out if c] or [[]]


def _merged(line: list[_Frag]) -> list[_Frag]:
    out: list[_Frag] = []
    for f in line:
        if out and (out[-1].font, out[-1].size, out[-1].color, out[-1].href) == (f.font, f.size, f.color, f.href):
            last = out[-1]
            out[-1] = _Frag(last.text + f.text, f.font, f.size, f.color, f.href, last.width + f.width)
        else:
            out.append(f)
    return out


@dataclass
class _TocEntry:
    level: int
    title: str
    key: tuple


class _Writer:
    """The pages of a PDF being laid out, top to bottom; `dests` keeps where each section and page starts."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: list[_Canvas] = []
        self.page: _Canvas | None = None
        self.y = TOP
        self.dests: dict[tuple, tuple[int, float]] = {}

    # ---- pages and space
    def new_page(self, footer: bool = True) -> None:
        self.page = _Canvas(footer)
        self.pages.append(self.page)
        self.y = TOP

    @property
    def at_top(self) -> bool:
        return self.y >= TOP - 0.5

    def room(self, h: float) -> None:
        if self.page is None or (self.y - h < MB and not self.at_top):
            self.new_page()

    def gap(self, h: float) -> None:
        if not self.at_top:
            self.y -= h

    # ---- text
    def draw(self, line: list[_Frag], x: float, base: float, w: float = 0.0, align: str = "left") -> None:
        frags = _merged(line)
        total = sum(f.width for f in frags)
        if align == "right" and w:
            x += max(0.0, w - total)
        elif align == "center" and w:
            x += max(0.0, (w - total) / 2)
        for f in frags:
            self.page.text(x, base, f.text, f.font, f.size, f.color)
            if f.href and f.text.strip():
                self.page.line(x, base - 1.3, x + f.width, base - 1.3, LINK, 0.45)
                self.page.links.append((x, base - 0.25 * f.size, x + f.width, base + 0.8 * f.size, f.href))
            x += f.width

    def lines(self, lines: list[list[_Frag]], x: float, w: float, size: float, lead: float,
              marker: tuple[str, str, tuple] | None = None, align: str = "left") -> None:
        for i, ln in enumerate(lines):
            self.room(lead)
            base = self.y - lead + 0.25 * size
            if i == 0 and marker is not None:
                text, font, col = marker
                mw = _width(text, font, size)
                self.page.text(x - 5 - mw, base, text, font, size, col)
            self.draw(ln, x, base, w, align)
            self.y -= lead

    def para(self, runs: list[Run], x: float = ML, w: float = CW, size: float = BODY, lead: float = LEAD,
             color: tuple = INK, bold: bool = False, italic: bool = False, after: float = 6.0,
             marker: tuple[str, str, tuple] | None = None) -> None:
        lines = _flow(runs, w, size, color, bold, italic)
        if not lines and marker is not None:
            lines = [[]]
        self.lines(lines, x, w, size, lead, marker)
        if lines:
            self.y -= after

    # ---- blocks
    def blocks(self, blocks: list[Any], x: float = ML, w: float = CW, color: tuple = INK, depth: int = 0) -> None:
        for b in blocks:
            if isinstance(b, Heading):
                self.heading(b.runs, b.level, x, w)
            elif isinstance(b, Para):
                self.para(b.runs, x, w, color=color)
            elif isinstance(b, ListBlock):
                self.list(b, x, w, color, depth)
            elif isinstance(b, Table):
                self.table(b, x, w)
            elif isinstance(b, Code):
                self.code(b.text, x, w)
            elif isinstance(b, Quote):
                self.quote(b, x, w)
            elif isinstance(b, Rule):
                self.rule(x, w)

    def heading(self, runs: list[Run], level: int, x: float = ML, w: float = CW) -> None:
        size = {1: 15.0, 2: 13.0, 3: 11.5}.get(level, 10.5)
        lead = size * 1.3
        self.gap({1: 10.0, 2: 8.0, 3: 6.0}.get(level, 5.0))
        lines = _flow(runs, w, size, INK, bold=True, italic=level >= 4)
        self.room(lead * len(lines) + LEAD * 2)           # never alone at the bottom of a page
        self.lines(lines, x, w, size, lead)
        self.y -= 3.0

    def list(self, lst: ListBlock, x: float, w: float, color: tuple, depth: int) -> None:
        indent = 17.0
        ix, iw = x + indent, w - indent
        for k, item in enumerate(lst.items):
            if lst.ordered:
                marker = (f"{lst.start + k}.", "F1", color)
            else:
                marker = (("\u2022", "\u2013", "\u00b7")[depth % 3], "F2" if depth % 3 == 2 else "F1", color)
            if item and isinstance(item[0], Para):
                self.para(item[0].runs, ix, iw, color=color, after=2.5, marker=marker)
                rest = item[1:]
            else:
                self.lines([[]], ix, iw, BODY, LEAD, marker)
                rest = item
            self.blocks(rest, ix, iw, color, depth + 1)
        self.y -= 3.5

    def code(self, text: str, x: float, w: float) -> None:
        size, lead, pad = 8.6, 11.2, 6.0
        per_line = max(10, int((w - 2 * pad) / (0.6 * size)))
        rows: list[str] = []
        for raw in text.split("\n"):
            raw = _ansi(raw.replace("\t", "    "))
            rows.extend([raw[i:i + per_line] for i in range(0, len(raw), per_line)] or [""])
        self.gap(4.0)
        i = 0
        while i < len(rows):
            fit = int((self.y - MB - 2 * pad) // lead)
            if fit < min(2, len(rows) - i) and not self.at_top:
                self.new_page()
                continue
            take = max(1, min(fit, len(rows) - i))
            h = take * lead + 2 * pad
            self.page.rect(x, self.y - h, w, h, fill=CODE_BG)
            for j in range(take):
                self.page.text(x + pad, self.y - pad - (j + 1) * lead + 0.27 * size + 1.0, rows[i + j], "F5", size,
                               CODE_INK)
            self.y -= h
            i += take
            if i < len(rows):
                self.new_page()
        self.y -= 8.0

    def quote(self, q: Quote, x: float, w: float) -> None:
        self.gap(2.0)
        self.room(LEAD)
        first, top = len(self.pages) - 1, self.y
        self.blocks(q.blocks, x + 14, w - 14, color=QUOTE_INK)
        last, bottom = len(self.pages) - 1, self.y + 4.0
        for p in range(first, last + 1):
            hi = top if p == first else TOP
            lo = bottom if p == last else MB
            if hi > lo:
                self.pages[p].rect(x + 2, lo, 2.2, hi - lo, fill=QUOTE_BAR)
        self.y -= 2.0

    def rule(self, x: float, w: float) -> None:
        self.gap(4.0)
        self.room(10.0)
        self.page.line(x, self.y - 5, x + w, self.y - 5, BORDER, 0.8)
        self.y -= 12.0

    def table(self, t: Table, x: float, w: float) -> None:
        ncols = max([len(t.header)] + [len(r) for r in t.rows])
        if not ncols:
            return
        size, lead, pad = 9.0, 12.0, 4.0
        rows = t.rows
        widths = _col_widths(t, ncols, w, size, pad)
        aligns = (t.aligns + ["left"] * ncols)[:ncols]

        def cells(row: list[list[Run]], bold: bool) -> list[list[list[_Frag]]]:
            return [_flow(row[i] if i < len(row) else [], widths[i] - 2 * pad, size, INK, bold=bold)
                    for i in range(ncols)]

        head = cells(t.header, True) if t.header else None
        head_n = (max(len(c) for c in head) or 1) if head else 0
        head_h = head_n * lead + 2 * pad if head else 0.0
        self.gap(4.0)
        if head:
            self.room(head_h + lead + 2 * pad)
            self._row(head, widths, x, size, lead, pad, aligns, 0, head_n, HEAD_BG)
        for row in rows:
            body = cells(row, False)
            n = max((len(c) for c in body), default=1) or 1
            h = n * lead + 2 * pad
            if self.y - h < MB and h <= (TOP - MB) - head_h:            # on the next page, under the header again
                self.new_page()
                if head:
                    self._row(head, widths, x, size, lead, pad, aligns, 0, head_n, HEAD_BG)
            done = 0
            while done < n:                                              # a row taller than a page: in parts
                fit = int((self.y - MB - 2 * pad) // lead)
                if fit <= 0:
                    self.new_page()
                    if head:
                        self._row(head, widths, x, size, lead, pad, aligns, 0, head_n, HEAD_BG)
                    continue
                take = min(fit, n - done)
                self._row(body, widths, x, size, lead, pad, aligns, done, take, None)
                done += take
                if done < n:
                    self.new_page()
                    if head:
                        self._row(head, widths, x, size, lead, pad, aligns, 0, head_n, HEAD_BG)
        self.y -= 9.0

    def _row(self, cells: list[list[list[_Frag]]], widths: list[float], x: float, size: float, lead: float,
             pad: float, aligns: list[str], start: int, count: int, fill: tuple | None) -> None:
        h = count * lead + 2 * pad
        top = self.y
        if fill is not None:
            self.page.rect(x, top - h, sum(widths), h, fill=fill)
        cx = x
        for i, lines in enumerate(cells):
            for j, ln in enumerate(lines[start:start + count]):
                base = top - pad - (j + 1) * lead + 0.27 * size + 0.5
                self.draw(ln, cx + pad, base, widths[i] - 2 * pad, aligns[i])
            cx += widths[i]
        cx = x
        for wc in widths:
            self.page.rect(cx, top - h, wc, h, stroke=BORDER, lw=0.5)
            cx += wc
        self.y -= h

    # ---- the parts of the document
    def section_title(self, title: str) -> None:
        lines = _flow([Run(title)], CW, 21.0, ACCENT, bold=True)
        self.lines(lines, ML, CW, 21.0, 26.0)
        self.page.rect(ML, self.y - 6, CW, 1.6, fill=ACCENT)
        self.y -= 22.0

    def page_title(self, title: str, note: str) -> None:
        lines = _flow([Run(title)], CW, 16.0, INK, bold=True)
        self.lines(lines, ML, CW, 16.0, 20.5)
        self.y -= 2.0
        if note:
            self.para([Run(note, italic=True)], size=9.0, lead=12.0, color=MUTED, after=0.0)
        self.y -= 10.0

    def sources(self, sources: list[str]) -> None:
        self.gap(8.0)
        self.room(12.0 + 2 * 11.5)
        self.para([Run("Sources", bold=True)], size=9.0, lead=12.0, color=MUTED, after=1.0)
        for s in sources:
            self.para([Run(str(s))], ML + 12, CW - 12, size=8.6, lead=11.5, color=MUTED, after=0.5,
                      marker=("\u2013", "F1", MUTED))


def _share(want: list[float], total: float) -> list[float]:
    """Widths for columns that want `want` each (their text on one line), `total` in all. When they all fit, each
    gets what it wants and a share of the rest in proportion; else the narrow ones get what they want (never
    wrapped) and the wide ones share what is left equally (a water level): numbers, dates and codes stay whole, long
    texts wrap."""
    if not want:
        return []
    if sum(want) <= total:
        extra = total - sum(want)
        return [x + extra * x / sum(want) for x in want]
    out = [0.0] * len(want)
    order = sorted(range(len(want)), key=lambda i: want[i])
    left = total
    for k, i in enumerate(order):
        level = left / (len(order) - k)
        if want[i] > level:
            for j in order[k:]:
                out[j] = level
            break
        out[i] = want[i]
        left -= want[i]
    return out


def _col_widths(t: Table, ncols: int, w: float, size: float, pad: float) -> list[float]:
    """Each column as wide as its widest cell on one line would make it, shared out over the text's width (_share)."""
    rows = ([t.header] if t.header else []) + t.rows
    natural = [2 * pad + 8.0] * ncols
    for k, row in enumerate(rows):
        font = "F2" if (t.header and k == 0) else "F1"
        for i in range(ncols):
            for line in _plain(row[i] if i < len(row) else []).split("\n"):
                natural[i] = max(natural[i], _width(_ansi(line), font, size) + 2 * pad + 1)
    return _share(natural, w)


def _toc_plan(entries: list[_TocEntry]) -> list[list[tuple[_TocEntry, float]]]:
    """The table of contents' pages: where each entry goes (its top), the first page under its heading."""
    pages: list[list[tuple[_TocEntry, float]]] = [[]]
    y = TOP - 40.0
    for e in entries:
        h = 19.0 if e.level == 0 else 15.0
        if y - h < MB:
            pages.append([])
            y = TOP
        pages[-1].append((e, y))
        y -= h
    return pages


def _draw_toc(w: _Writer, first: int, plan: list[list[tuple[_TocEntry, float]]]) -> None:
    for k, part in enumerate(plan):
        page = w.pages[first + k]
        if k == 0:
            page.text(ML, TOP - 22.0, "Contents", "F2", 18.0, INK)
        for e, y in part:
            index, _ytop = w.dests.get(e.key, (0, TOP))
            number = str(index + 1)
            size, font = (11.0, "F2") if e.level == 0 else (10.0, "F1")
            indent = 0.0 if e.level == 0 else 16.0
            base = y - (19.0 if e.level == 0 else 15.0) + 4.5
            nw = _width(number, font, size)
            room = CW - indent - 36.0
            title = _ansi(" ".join(e.title.split())) or "(untitled)"
            if _width(title, font, size) > room:
                while title and _width(title + "...", font, size) > room:
                    title = title[:-1]
                title = title.rstrip() + "..."
            tw = _width(title, font, size)
            page.text(ML + indent, base, title, font, size, INK)
            dot_w = _width(" .", "F1", 9.0)
            gap = (CW - nw - 6) - (indent + tw + 6)
            if gap > dot_w:
                page.text(ML + indent + tw + 6, base, " ." * int(gap / dot_w), "F1", 9.0, MUTED)
            page.text(ML + CW - nw, base, number, font, size, INK)
            page.links.append((ML, base - 4, ML + CW, base + size, ("dest", index, _ytop)))


def _cover(w: _Writer, doc: Document) -> None:
    w.new_page(footer=False)
    w.y = PH * 0.64
    w.page.rect(ML, w.y + 18, 64, 3.2, fill=ACCENT)
    w.lines(_flow([Run(doc.title or "Document")], CW, 27.0, INK, bold=True), ML, CW, 27.0, 33.0)
    if doc.subtitle:
        w.y -= 4.0
        w.lines(_flow([Run(doc.subtitle)], CW, 14.0, MUTED), ML, CW, 14.0, 19.0)
    if doc.meta:
        w.y -= 26.0
        for m in doc.meta:
            w.lines(_flow([Run(str(m))], CW, 10.0, MUTED), ML, CW, 10.0, 14.5)


def to_pdf(doc: Document) -> bytes:
    """The document as a PDF (A4): a cover, the table of contents, each section and each of its pages from a new
    page, page numbers, bookmarks."""
    w = _Writer(doc.title or "Document")
    _cover(w, doc)
    entries: list[_TocEntry] = []
    for si, s in enumerate(doc.sections):
        entries.append(_TocEntry(0, s.title, ("s", si)))
        entries += [_TocEntry(1, p.title, ("p", si, pi)) for pi, p in enumerate(s.pages)]
    plan = _toc_plan(entries) if doc.toc and entries else []
    first_toc = len(w.pages)
    for _part in plan:
        w.new_page()
    for si, s in enumerate(doc.sections):
        w.new_page()
        w.dests[("s", si)] = (len(w.pages) - 1, w.y)
        w.section_title(s.title)
        for pi, p in enumerate(s.pages):
            if pi:
                w.new_page()
            w.dests[("p", si, pi)] = (len(w.pages) - 1, w.y)
            w.page_title(p.title, p.note)
            w.blocks(page_blocks(p))
            if p.sources:
                w.sources(p.sources)
    if not doc.sections:
        w.new_page()
        w.para([Run("This document has no page yet.", italic=True)], color=MUTED)
    _draw_toc(w, first_toc, plan)
    outline = [(s.title, w.dests[("s", si)], [(p.title, w.dests[("p", si, pi)]) for pi, p in enumerate(s.pages)])
               for si, s in enumerate(doc.sections)]
    return _pdf_file(w.pages, doc.title or "Document", outline)


# --------------------------------------------------------------------------------------------- #
# PDF: the file
# --------------------------------------------------------------------------------------------- #
def _uri(url: str) -> bytes:
    safe = quote(url, safe=":/?#[]@!$&'*+,;=%~-._")
    return _pdf_str(safe)


def _footer(page: _Canvas, title: str, number: int, total: int) -> list[bytes]:
    c = _Canvas()
    w, _h = page.size
    c.line(ML, FOOT_Y + 11, w - MR, FOOT_Y + 11, BORDER, 0.4)
    label = _ansi(" ".join(title.split()))
    room = (w - ML - MR) * 0.62
    if _width(label, "F1", 8.0) > room:
        while label and _width(label + "...", "F1", 8.0) > room:
            label = label[:-1]
        label = label.rstrip() + "..."
    c.text(ML, FOOT_Y, label, "F1", 8.0, MUTED)
    right = f"Page {number} of {total}"
    c.text(w - MR - _width(right, "F1", 8.0), FOOT_Y, right, "F1", 8.0, MUTED)
    return c.ops


def _pdf_file(pages: list[_Canvas], title: str,
              outline: list[tuple[str, tuple[int, float], list]] | None = None) -> bytes:
    objs: dict[int, bytes] = {}
    count = 0

    def num() -> int:
        nonlocal count
        count += 1
        return count

    catalog, root, info, resources = num(), num(), num(), num()
    fonts = {k: num() for k in FONTS}
    page_ids = [num() for _ in pages]
    content_ids = [num() for _ in pages]
    images: list[tuple[int, int, str, tuple[str, int, int, bytes]]] = []
    for i, p in enumerate(pages):
        for img in p.images:
            images.append((num(), i, img[0], img))
    for k, name in FONTS.items():
        widths = _WIDTHS[k]
        arr = b" ".join(str(v).encode() for v in (widths if widths is not None else [600] * 224))
        objs[fonts[k]] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding "
                          b"/FirstChar 32 /LastChar 255 /Widths [%s] >>" % (name.encode(), arr))
    font_dict = b"<< " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in fonts.items()) + b" >>"
    objs[resources] = b"<< /Font %s /ProcSet [/PDF /Text /ImageC] >>" % font_dict
    for oid, _page, _name, (_n2, wpx, hpx, data) in images:
        objs[oid] = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                     b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (wpx, hpx, len(data))
                     + data + b"\nendstream")
    total = len(pages)
    for i, p in enumerate(pages):
        ops = list(p.ops) + (_footer(p, title, i + 1, total) if p.footer else [])
        stream = zlib.compress(b"\n".join(ops), 9)
        objs[content_ids[i]] = (b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream
                                + b"\nendstream")
        annots = []
        for x1, y1, x2, y2, target in p.links:
            rect = b"[%s %s %s %s]" % (_n(x1), _n(y1), _n(x2), _n(y2))
            if isinstance(target, tuple):
                _kind, index, ytop = target
                annots.append(b"<< /Type /Annot /Subtype /Link /Rect %s /Border [0 0 0] "
                              b"/Dest [%d 0 R /XYZ 0 %s null] >>" % (rect, page_ids[index], _n(ytop + 6)))
            else:
                annots.append(b"<< /Type /Annot /Subtype /Link /Rect %s /Border [0 0 0] /A << /S /URI /URI %s >> >>"
                              % (rect, _uri(target)))
        mine = [(oid, name) for oid, page, name, _img in images if page == i]
        if mine:
            res = b"<< /Font %s /XObject << %s >> /ProcSet [/PDF /Text /ImageC] >>" % (
                font_dict, b" ".join(b"/%s %d 0 R" % (name.encode(), oid) for oid, name in mine))
        else:
            res = b"%d 0 R" % resources
        pw, ph = p.size
        objs[page_ids[i]] = (b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources %s /Contents %d 0 R%s >>"
                             % (root, _n(pw), _n(ph), res, content_ids[i],
                                (b" /Annots [" + b" ".join(annots) + b"]") if annots else b""))
    objs[root] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % x for x in page_ids), total)
    outline_id = None
    if outline:
        outline_id = num()
        top: list[int] = []
        all_items = 0
        for _title, _dest, kids in outline:
            top.append(num())
            all_items += 1 + len(kids)
        kid_ids = [[num() for _k in kids] for _t, _d, kids in outline]

        def dest(d: tuple[int, float]) -> bytes:
            return b"[%d 0 R /XYZ 0 %s null]" % (page_ids[d[0]], _n(d[1] + 6))

        for k, (stitle, sdest, kids) in enumerate(outline):
            parts = [b"/Title %s /Parent %d 0 R /Dest %s" % (_pdf_text_string(stitle), outline_id, dest(sdest))]
            if k:
                parts.append(b"/Prev %d 0 R" % top[k - 1])
            if k < len(outline) - 1:
                parts.append(b"/Next %d 0 R" % top[k + 1])
            if kids:
                parts.append(b"/First %d 0 R /Last %d 0 R /Count %d" % (kid_ids[k][0], kid_ids[k][-1], len(kids)))
            objs[top[k]] = b"<< " + b" ".join(parts) + b" >>"
            for j, (ptitle, pdest) in enumerate(kids):
                kp = [b"/Title %s /Parent %d 0 R /Dest %s" % (_pdf_text_string(ptitle), top[k], dest(pdest))]
                if j:
                    kp.append(b"/Prev %d 0 R" % kid_ids[k][j - 1])
                if j < len(kids) - 1:
                    kp.append(b"/Next %d 0 R" % kid_ids[k][j + 1])
                objs[kid_ids[k][j]] = b"<< " + b" ".join(kp) + b" >>"
        objs[outline_id] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (top[0], top[-1], all_items)
    outlines = b" /Outlines %d 0 R /PageMode /UseOutlines" % outline_id if outline_id else b""
    objs[catalog] = b"<< /Type /Catalog /Pages %d 0 R%s >>" % (root, outlines)
    now = dt.datetime.now(dt.timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    objs[info] = (b"<< /Title %s /Producer (supagent) /Creator (supagent) /CreationDate (%s) /ModDate (%s) >>"
                  % (_pdf_text_string(title), now.encode(), now.encode()))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (count + 1)
    for i in range(1, count + 1):
        offsets[i] = out.tell()
        out.write(b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (count + 1))
    for i in range(1, count + 1):
        out.write(b"%010d 00000 n \n" % offsets[i])
    out.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n" % (count + 1, catalog, info, xref))
    out.write(b"%%EOF\n")
    return out.getvalue()


def image_pdf(png: bytes, title: str, note: str = "", dpi: float | None = None) -> bytes:
    """An image (the system map) on one landscape page: A4, or A3 / A2 when the image would shrink by more than
    30 % on the smaller page (its text unreadable); the title above it, and the note. The image is read at `dpi`
    (a 2x capture: 192), else at the dpi its PNG says, else at 96."""
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    im.load()
    dpi = (dpi, dpi) if dpi else (im.info.get("dpi") or (96, 96))   # a 2x capture: 192 dpi; else 96 (a screen's)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        ground = Image.new("RGB", im.size, (255, 255, 255))
        ground.paste(im, mask=im.split()[-1])
        im = ground
    else:
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92, subsampling=0, optimize=True)
    wpx, hpx = im.size
    try:
        per_px = 72.0 / max(48.0, min(600.0, float(dpi[0])))
    except (TypeError, ValueError, IndexError):
        per_px = 0.75
    margin, head = 36.0, (44.0 if note else 30.0)
    for pw, ph in ((841.89, 595.28), (1190.55, 841.89), (1683.78, 1190.55)):     # A4, A3, A2 landscape
        aw, ah = pw - 2 * margin, ph - 2 * margin - head
        scale = min(aw / (wpx * per_px), ah / (hpx * per_px), 2.0)
        if scale >= 0.7:                               # its text still readable: this page is large enough
            break
    dw, dh = wpx * per_px * scale, hpx * per_px * scale
    page = _Canvas(footer=False, size=(pw, ph))
    label = _ansi(" ".join((title or "").split()))
    page.text(margin, ph - margin - 14, label, "F2", 15.0, INK)
    if note:
        page.text(margin, ph - margin - 30, _ansi(" ".join(note.split())), "F3", 9.5, MUTED)
    x = (pw - dw) / 2
    y = margin + (ah - dh) / 2
    page.images.append(("Im1", wpx, hpx, buf.getvalue()))
    page.ops.append(b"q %s 0 0 %s %s %s cm /Im1 Do Q" % (_n(dw), _n(dh), _n(x), _n(y)))
    return _pdf_file([page], title or "Image")


# --------------------------------------------------------------------------------------------- #
# Word (.docx)
# --------------------------------------------------------------------------------------------- #
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TEXT_W = 9864                      # twips between the margins (A4, 18 mm)
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")


def _x(text: str) -> str:
    text = _BAD_XML.sub("", text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _wrun(text: str, rpr: str = "") -> str:
    parts = []
    for i, line in enumerate((text or "").split("\n")):
        if i:
            parts.append("<w:br/>")
        for j, seg in enumerate(line.split("\t")):
            if j:
                parts.append("<w:tab/>")
            if seg:
                parts.append(f'<w:t xml:space="preserve">{_x(seg)}</w:t>')
    if not parts:
        return ""
    return f"<w:r>{f'<w:rPr>{rpr}</w:rPr>' if rpr else ''}{''.join(parts)}</w:r>"


def _wp(content: str, style: str | None = None, ppr: str = "") -> str:
    props = (f'<w:pStyle w:val="{style}"/>' if style else "") + ppr
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{content}</w:p>"


class _Docx:
    def __init__(self) -> None:
        self.body: list[str] = []
        self.links: dict[str, str] = {}
        self.ordered: list[tuple[int, int]] = []          # each numbered list: (its start, its level)
        self.marks = 0
        self.toc: list[tuple[int, str, str]] = []

    def link(self, url: str) -> str:
        if url not in self.links:
            self.links[url] = f"rIdL{len(self.links) + 1}"
        return self.links[url]

    def numbered(self, start: int, level: int) -> int:
        self.ordered.append((start, level))
        return len(self.ordered) + 1                       # numId 1 is the bullets'

    def runs(self, runs: list[Run], bold: bool = False) -> str:
        out = []
        for r in runs:
            linked = bool(r.href) and _safe_url(r.href)
            rpr = '<w:rStyle w:val="CodeChar"/>' if r.code else ('<w:rStyle w:val="Hyperlink"/>' if linked else "")
            if r.bold or bold:
                rpr += "<w:b/>"
            if r.italic:
                rpr += "<w:i/>"
            xml = _wrun(r.text, rpr)
            if linked and xml:
                xml = f'<w:hyperlink r:id="{self.link(r.href)}" w:history="1">{xml}</w:hyperlink>'
            out.append(xml)
        return "".join(out)

    def heading(self, text: str, style: str, level: int, page_break: bool = False) -> None:
        self.marks += 1
        name = f"_Toc{self.marks:05d}"
        ppr = "<w:pageBreakBefore/>" if page_break else ""
        self.body.append(f'<w:p><w:pPr><w:pStyle w:val="{style}"/>{ppr}</w:pPr>'
                         f'<w:bookmarkStart w:id="{self.marks}" w:name="{name}"/>{_wrun(text)}'
                         f'<w:bookmarkEnd w:id="{self.marks}"/></w:p>')
        self.toc.append((level, text, name))

    def blocks(self, blocks: list[Any], depth: int = 0, quote: bool = False) -> None:
        for b in blocks:
            if isinstance(b, Heading):
                self.body.append(_wp(self.runs(b.runs), "Heading3" if b.level <= 2 else "Heading4"))
            elif isinstance(b, Para):
                self.body.append(_wp(self.runs(b.runs), "Quote" if quote else None))
            elif isinstance(b, ListBlock):
                self.list(b, depth, quote)
            elif isinstance(b, Table):
                self.table(b)
            elif isinstance(b, Code):
                self.code(b.text)
            elif isinstance(b, Quote):
                self.blocks(b.blocks, depth, quote=True)
            elif isinstance(b, Rule):
                self.body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
                                 'w:color="C3CBD0"/></w:pBdr></w:pPr></w:p>')

    def list(self, lst: ListBlock, depth: int, quote: bool) -> None:
        level = min(depth, 8)
        num = self.numbered(lst.start, level) if lst.ordered else 1
        for item in lst.items:
            numpr = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>'
            first, rest = (item[0], item[1:]) if item and isinstance(item[0], Para) else (None, item)
            self.body.append(_wp(self.runs(first.runs) if first is not None else "", "ListParagraph", numpr))
            for b in rest:
                if isinstance(b, ListBlock):
                    self.list(b, depth + 1, quote)
                elif isinstance(b, Para):
                    self.body.append(_wp(self.runs(b.runs), "ListParagraph", f'<w:ind w:left="{720 * (level + 1)}"/>'))
                else:
                    self.blocks([b], depth + 1, quote)

    def code(self, text: str) -> None:
        parts = []
        for i, line in enumerate(text.split("\n")):
            if i:
                parts.append("<w:br/>")
            if line:
                parts.append(f'<w:t xml:space="preserve">{_x(line.replace(chr(9), "    "))}</w:t>')
        self.body.append(_wp(f"<w:r>{''.join(parts)}</w:r>" if parts else "", "Code"))

    def table(self, t: Table) -> None:
        ncols = max([len(t.header)] + [len(r) for r in t.rows])
        if not ncols:
            return
        rows = ([t.header] if t.header else []) + t.rows
        natural = [400.0] * ncols                          # twips: about 100 a character, and the cell's margins
        for k, row in enumerate(rows):
            for i in range(ncols):
                text = _plain(row[i]) if i < len(row) else ""
                chars = max((len(x) for x in text.split("\n")), default=0)
                natural[i] = max(natural[i], chars * (108 if (t.header and k == 0) else 100) + 200)
        widths = [max(1, int(x)) for x in _share(natural, TEXT_W)]
        widths[-1] += TEXT_W - sum(widths)
        aligns = (t.aligns + ["left"] * ncols)[:ncols]
        border = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="BFC8CE"/>'
                         for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
        xml = ['<w:tbl><w:tblPr><w:tblStyle w:val="SupagentTable"/><w:tblW w:w="5000" w:type="pct"/>'
               f'<w:tblBorders>{border}</w:tblBorders><w:tblLayout w:type="fixed"/>'
               '<w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="85" w:type="dxa"/>'
               '<w:bottom w:w="40" w:type="dxa"/><w:right w:w="85" w:type="dxa"/></w:tblCellMar>'
               '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" '
               'w:noHBand="1" w:noVBand="1"/></w:tblPr>',
               "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{x}"/>' for x in widths) + "</w:tblGrid>"]
        for k, row in enumerate(rows):
            head = bool(t.header) and k == 0
            tr = ["<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if head else "")]
            for i in range(ncols):
                runs = row[i] if i < len(row) else []
                tcpr = f'<w:tcW w:w="{widths[i]}" w:type="dxa"/>' + (
                    '<w:shd w:val="clear" w:color="auto" w:fill="E6EDF1"/>' if head else "")
                jc = {"right": "right", "center": "center"}.get(aligns[i])
                ppr = '<w:spacing w:before="0" w:after="0"/>' + (f'<w:jc w:val="{jc}"/>' if jc else "")
                tr.append(f"<w:tc><w:tcPr>{tcpr}</w:tcPr>{_wp(self.runs(runs, bold=head), 'TableText', ppr)}</w:tc>")
            tr.append("</w:tr>")
            xml.append("".join(tr))
        xml.append("</w:tbl>")
        self.body.append("".join(xml))
        self.body.append(_wp("", None, '<w:spacing w:after="60"/>'))


def to_docx(doc: Document) -> bytes:
    """The document as Word (.docx): a cover, the table of contents (Word fills in its page numbers when the fields
    are updated), each section from a new page, the page numbers in the footer."""
    w = _Docx()
    for si, s in enumerate(doc.sections):
        w.heading(s.title, "Heading1", 0, page_break=True)
        for p in s.pages:
            w.heading(p.title, "Heading2", 1)
            if p.note:
                w.body.append(_wp(_wrun(p.note), "PageNote"))
            w.blocks(page_blocks(p))
            if p.sources:
                w.body.append(_wp(_wrun("Sources"), "SourcesHeading"))
                for src in p.sources:
                    w.body.append(_wp(_wrun(str(src)), "Source"))
    if not doc.sections:
        w.body.append(_wp(_wrun("This document has no page yet.", "<w:i/>"), None, "<w:pageBreakBefore/>"))
    cover = [_wp(_wrun(doc.title or "Document"), "Title")]
    if doc.subtitle:
        cover.append(_wp(_wrun(doc.subtitle), "Subtitle"))
    cover += [_wp(_wrun(str(m)), "Meta") for m in doc.meta]
    toc: list[str] = []
    if doc.toc and w.toc:
        toc.append(_wp(_wrun("Contents"), "TOCHeading", "<w:pageBreakBefore/>"))
        for k, (level, text, name) in enumerate(w.toc):
            begin = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o '
                     '"1-2" \\h \\z \\u </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                     if k == 0 else "")
            end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>' if k == len(w.toc) - 1 else ""
            toc.append(_wp(f'{begin}<w:hyperlink w:anchor="{name}" w:history="1">{_wrun(text)}</w:hyperlink>{end}',
                           "TOC1" if level == 0 else "TOC2"))
    sect = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter1"/><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1077" w:right="1021" w:bottom="1134" w:left="1021" w:header="567" w:footer="510" '
            'w:gutter="0"/><w:titlePg/></w:sectPr>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="{W_NS}" '
                f'xmlns:r="{R_NS}"><w:body>{"".join(cover + toc + w.body)}<w:p/>{sect}</w:body></w:document>')
    rels = [(f"{REL}/styles", "styles.xml", "rIdStyles", False),
            (f"{REL}/numbering", "numbering.xml", "rIdNumbering", False),
            (f"{REL}/settings", "settings.xml", "rIdSettings", False),
            (f"{REL}/footer", "footer1.xml", "rIdFooter1", False)]
    rels += [(f"{REL}/hyperlink", url, rid, True) for url, rid in w.links.items()]
    rels_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships">' + "".join(
                    f'<Relationship Id="{rid}" Type="{kind}" Target="{_x(target)}"'
                    + (' TargetMode="External"' if external else "") + "/>" for kind, target, rid, external in rels)
                + "</Relationships>")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _PACKAGE_RELS,
        "docProps/core.xml": _CORE.format(title=_x(doc.title or "Document"), now=now),
        "docProps/app.xml": _APP,
        "word/document.xml": document,
        "word/_rels/document.xml.rels": rels_xml,
        "word/styles.xml": _STYLES,
        "word/numbering.xml": _numbering(w.ordered),
        "word/settings.xml": _SETTINGS,
        "word/footer1.xml": _footer_xml(" ".join((doc.title or "Document").split())),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


def _numbering(ordered: list[tuple[int, int]]) -> str:
    bullets = "".join(
        f'<w:lvl w:ilvl="{lvl}"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="'
        f'{("•", "–", "•")[lvl % 3]}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{720 * (lvl + 1)}" '
        f'w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/></w:rPr></w:lvl>'
        for lvl in range(9))
    decimals = "".join(
        f'<w:lvl w:ilvl="{lvl}"><w:start w:val="1"/><w:numFmt w:val="'
        f'{("decimal", "lowerLetter", "lowerRoman")[lvl % 3]}"/><w:lvlText w:val="%{lvl + 1}."/><w:lvlJc w:val="left"/>'
        f'<w:pPr><w:ind w:left="{720 * (lvl + 1)}" w:hanging="360"/></w:pPr></w:lvl>' for lvl in range(9))
    nums = '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>' + "".join(
        f'<w:num w:numId="{k + 2}"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="{lvl}"><w:startOverride '
        f'w:val="{max(0, start)}"/></w:lvlOverride></w:num>' for k, (start, lvl) in enumerate(ordered))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="{W_NS}">'
            f'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>{bullets}</w:abstractNum>'
            f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>{decimals}</w:abstractNum>'
            f"{nums}</w:numbering>")


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.numbering+xml"/>'
    '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.settings+xml"/>'
    '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.footer+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.'
    'extended-properties+xml"/></Types>')
_PACKAGE_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
    'core-properties" Target="docProps/core.xml"/>'
    f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/></Relationships>')
_CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:title>{title}</dc:title><dc:creator>supagent</dc:creator><cp:lastModifiedBy>supagent</cp:lastModifiedBy>'
    '<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
    '<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
_APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>supagent</Application></Properties>')
_SETTINGS = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="{W_NS}">'
             '<w:defaultTabStop w:val="720"/><w:characterSpacingControl w:val="doNotCompress"/>'
             '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" '
             'w:val="15"/></w:compat></w:settings>')


def _field(code: str, shown: str) -> str:
    return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            f'<w:r><w:t>{shown}</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>')


def _footer_xml(title: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:ftr xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
            f'<w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr><w:r><w:t xml:space="preserve">{_x(title)}</w:t></w:r>'
            '<w:r><w:tab/></w:r><w:r><w:t xml:space="preserve">Page </w:t></w:r>' + _field("PAGE", "1")
            + '<w:r><w:t xml:space="preserve"> of </w:t></w:r>' + _field("NUMPAGES", "1") + "</w:p></w:ftr>")


def _style(kind: str, sid: str, name: str, body: str, based: str | None = "Normal", custom: bool = False,
           extra: str = "") -> str:
    custom_attr = ' w:customStyle="1"' if custom else ""
    based_xml = f'<w:basedOn w:val="{based}"/>' if based else ""
    return (f'<w:style w:type="{kind}"{custom_attr} w:styleId="{sid}"><w:name w:val="{name}"/>{based_xml}{extra}{body}'
            "</w:style>")


_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="{W_NS}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" '
    'w:cs="Calibri"/><w:color w:val="1F2A33"/><w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="en-US" '
    'w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" '
    'w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph '
    'Font"/><w:uiPriority w:val="1"/><w:semiHidden/><w:unhideWhenUsed/></w:style>'
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority '
    'w:val="99"/><w:semiHidden/><w:unhideWhenUsed/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top '
    'w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" '
    'w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/><w:uiPriority w:val="99"/>'
    '<w:semiHidden/><w:unhideWhenUsed/></w:style>'
    + _style("paragraph", "Title", "Title", '<w:pPr><w:spacing w:before="2880" w:after="240" w:line="240" '
             'w:lineRule="auto"/></w:pPr><w:rPr><w:b/><w:color w:val="1F3B4D"/><w:sz w:val="56"/><w:szCs w:val="56"/>'
             '</w:rPr>', extra='<w:next w:val="Normal"/><w:uiPriority w:val="10"/><w:qFormat/>')
    + _style("paragraph", "Subtitle", "Subtitle", '<w:pPr><w:spacing w:after="480"/></w:pPr><w:rPr><w:color '
             'w:val="4B5A66"/><w:sz w:val="30"/><w:szCs w:val="30"/></w:rPr>',
             extra='<w:next w:val="Normal"/><w:uiPriority w:val="11"/><w:qFormat/>')
    + _style("paragraph", "Meta", "Meta", '<w:pPr><w:spacing w:after="60"/></w:pPr><w:rPr><w:color w:val="6B7A86"/>'
             '<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>', custom=True, extra="<w:qFormat/>")
    + _style("paragraph", "Heading1", "heading 1", '<w:pPr><w:keepNext/><w:keepLines/><w:pBdr><w:bottom w:val="single" '
             'w:sz="8" w:space="4" w:color="1F6F8B"/></w:pBdr><w:spacing w:before="240" w:after="240"/><w:outlineLvl '
             'w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="1F6F8B"/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr>',
             extra='<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>')
    + _style("paragraph", "Heading2", "heading 2", '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="360" '
             'w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="1F3B4D"/><w:sz w:val="30"/>'
             '<w:szCs w:val="30"/></w:rPr>', extra='<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>')
    + _style("paragraph", "Heading3", "heading 3", '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="240" '
             'w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="25"/><w:szCs w:val="25"/>'
             '</w:rPr>', extra='<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>')
    + _style("paragraph", "Heading4", "heading 4", '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="200" '
             'w:after="60"/><w:outlineLvl w:val="3"/></w:pPr><w:rPr><w:b/><w:i/><w:sz w:val="22"/><w:szCs w:val="22"/>'
             '</w:rPr>', extra='<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>')
    + _style("paragraph", "TOCHeading", "TOC Heading", '<w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/>'
             '<w:color w:val="1F3B4D"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>',
             extra='<w:next w:val="Normal"/><w:uiPriority w:val="39"/><w:qFormat/>')
    + _style("paragraph", "TOC1", "toc 1", '<w:pPr><w:spacing w:before="120" w:after="40"/></w:pPr><w:rPr><w:b/>'
             '</w:rPr>', extra='<w:next w:val="Normal"/><w:uiPriority w:val="39"/><w:unhideWhenUsed/>')
    + _style("paragraph", "TOC2", "toc 2", '<w:pPr><w:spacing w:after="30"/><w:ind w:left="360"/></w:pPr>',
             extra='<w:next w:val="Normal"/><w:uiPriority w:val="39"/><w:unhideWhenUsed/>')
    + _style("paragraph", "ListParagraph", "List Paragraph", '<w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/>'
             '<w:contextualSpacing/></w:pPr>', extra='<w:uiPriority w:val="34"/><w:qFormat/>')
    + _style("paragraph", "Code", "Code", '<w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F1F4F6"/><w:spacing '
             'w:before="60" w:after="160" w:line="240" w:lineRule="auto"/><w:ind w:left="113" w:right="113"/></w:pPr>'
             '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Courier New"/><w:color w:val="2B3640"/>'
             '<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>', custom=True)
    + _style("character", "CodeChar", "Code Char", '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
             'w:cs="Courier New"/><w:color w:val="6B2E29"/><w:sz w:val="19"/><w:szCs w:val="19"/><w:shd w:val="clear" '
             'w:color="auto" w:fill="F1F4F6"/></w:rPr>', based="DefaultParagraphFont", custom=True)
    + _style("paragraph", "Quote", "Quote", '<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" '
             'w:color="A0AEB8"/></w:pBdr><w:spacing w:after="120"/><w:ind w:left="360"/></w:pPr><w:rPr><w:i/>'
             '<w:color w:val="4B5A66"/></w:rPr>', extra='<w:uiPriority w:val="29"/><w:qFormat/>')
    + _style("character", "Hyperlink", "Hyperlink", '<w:rPr><w:color w:val="1F6FB8"/><w:u w:val="single"/></w:rPr>',
             based="DefaultParagraphFont", extra='<w:uiPriority w:val="99"/><w:unhideWhenUsed/>')
    + _style("paragraph", "PageNote", "Page Note", '<w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:i/><w:color '
             'w:val="6B7A86"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>', custom=True)
    + _style("paragraph", "SourcesHeading", "Sources Heading", '<w:pPr><w:keepNext/><w:spacing w:before="240" '
             'w:after="60"/></w:pPr><w:rPr><w:b/><w:color w:val="6B7A86"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
             '</w:rPr>', custom=True)
    + _style("paragraph", "Source", "Source", '<w:pPr><w:spacing w:after="20"/><w:ind w:left="284"/></w:pPr><w:rPr>'
             '<w:color w:val="4B5A66"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr>', custom=True)
    + _style("paragraph", "TableText", "Table Text", '<w:pPr><w:spacing w:before="0" w:after="0" w:line="240" '
             'w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr>', custom=True)
    + _style("paragraph", "Footer", "footer", '<w:pPr><w:tabs><w:tab w:val="right" w:pos="9864"/></w:tabs><w:spacing '
             'w:after="0"/></w:pPr><w:rPr><w:color w:val="6B7A86"/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>',
             extra='<w:uiPriority w:val="99"/><w:unhideWhenUsed/>')
    + _style("table", "SupagentTable", "Supagent Table",
             '<w:pPr><w:spacing w:after="0"/></w:pPr><w:tblPr><w:tblBorders>'
             + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="BFC8CE"/>'
                       for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
             + '</w:tblBorders></w:tblPr><w:tblStylePr w:type="firstRow"><w:rPr><w:b/></w:rPr><w:tcPr><w:shd '
             'w:val="clear" w:color="auto" w:fill="E6EDF1"/></w:tcPr></w:tblStylePr>', based="TableNormal",
             extra='<w:uiPriority w:val="59"/>')
    + "</w:styles>")
