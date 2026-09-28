"""The guide to store or send: README.md as one standalone HTML file (opens offline) and a
PDF printed by Chromium.

    python lab/make_guide.py [--out dist/docs]
"""

from __future__ import annotations

import argparse
import os
import re

import markdown
from playwright.sync_api import sync_playwright

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CSS = """
:root { --ink:#1f2a33; --ink2:#4b5a66; --muted:#74828c; --line:#e1e7eb; --accent:#1f86a8; --code:#f1f4f6; }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: var(--ink); font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 30px; margin: 0 0 12px; text-wrap: balance; }
h2 { font-size: 21px; margin: 36px 0 10px; padding-top: 12px; border-top: 1px solid var(--line); }
h3 { font-size: 17px; margin: 24px 0 8px; }
p, li { max-width: 72ch; }
a { color: var(--accent); }
code { font-family: "SFMono-Regular", Menlo, Consolas, monospace; font-size: 13px; background: var(--code);
  padding: 1px 4px; border-radius: 4px; }
pre { background: var(--code); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px;
  overflow-x: auto; line-height: 1.45; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #f6f8f9; }
figure { margin: 22px 0; }
figure img { width: 100%; border: 1px solid var(--line); border-radius: 8px; }
figcaption { color: var(--ink2); font-size: 13.5px; margin-top: 6px; }
.meta { color: var(--muted); font-size: 13px; }
@media print {
  main { max-width: none; padding: 0; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; }
  figure, tr, pre { page-break-inside: avoid; }
  h2, h3 { page-break-after: avoid; }
}
"""


def version() -> str:
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
    return m.group(1) if m else "?"


def build() -> str:
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>supagent guide</title><style>{CSS}</style></head><body><main>"
            f"<p class=\"meta\">supagent {version()} · installation and operation guide</p>{body}</main></body></html>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "docs"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    page = build()
    out_html = os.path.join(args.out, "supagent-guide.html")
    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write(page)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto("file://" + os.path.abspath(out_html))
        pg.pdf(path=os.path.join(args.out, "supagent-guide.pdf"), format="A4", print_background=True,
               margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
        b.close()
    print(out_html, os.path.getsize(out_html), "bytes; pdf written")


if __name__ == "__main__":
    main()
