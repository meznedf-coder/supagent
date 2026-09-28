"""Documents and sites for the agent: uploaded text, Markdown or HTML files, and web pages
fetched again every refresh_days days (a site: up to max_pages pages under the same address).

Fetching is safe by default: http(s) only, at most docs.max_kb per page, 20 s per request, and
every address (redirects included) must be in docs.allowed_domains; with no allowed domain,
only public addresses are fetched (no intranet, no localhost). Text only: PDF would need a
package Superset does not have.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from superset import db

from supagent import settings
from supagent.models import Doc

log = logging.getLogger(__name__)
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre",
              "table", "ul", "ol", "dd", "dt", "blockquote"}


class DocError(Exception):
    pass


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg", "nav", "footer"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")                  # the cells of a row stay apart

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg", "nav", "footer") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str, list[str]]:
    """(title, text, links) of an HTML page."""
    p = _Text()
    p.feed(html or "")
    lines = [" ".join(line.split()) for line in "".join(p.parts).split("\n")]
    lines = [line[2:] if line.startswith("| ") else line for line in lines]
    text = "\n".join(line for line in lines if line and line != "|")
    return p.title.strip(), text, p.links


def check_url(url: str) -> None:
    """http(s), and an allowed domain (or, with no allowed domain, a public address)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise DocError(f"{url}: only http and https addresses")
    allowed = [d.strip().lower() for d in settings.get("docs.allowed_domains") or [] if d.strip()]
    host = u.hostname.lower()
    if allowed:
        if not any(host == d or host.endswith("." + d) for d in allowed):
            raise DocError(f"{host} is not in docs.allowed_domains")
        return
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as ex:
        raise DocError(f"{host}: unknown host") from ex
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise DocError(f"{host} is a private address: add its domain to docs.allowed_domains to allow it")


def fetch(url: str) -> tuple[str, str, list[str]]:
    """(title, text, links) of one page, following redirects only to allowed addresses."""
    limit = int(settings.get("docs.max_kb")) * 1024
    for _hop in range(5):
        check_url(url)
        r = requests.get(url, timeout=20, allow_redirects=False, stream=True,
                         headers={"User-Agent": "supagent (Superset AI agent) document reader"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            continue
        if r.status_code >= 400:
            raise DocError(f"{url}: HTTP {r.status_code}")
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > limit:
                raise DocError(f"{url}: larger than {limit // 1024} KB")
        ctype = r.headers.get("Content-Type", "")
        text = body.decode(r.encoding or "utf-8", errors="replace")
        if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
            title, content, links = html_to_text(text)
            return title, content, [urljoin(url, link) for link in links]
        if ctype and not ctype.startswith(("text/", "application/json", "application/xml")):
            raise DocError(f"{url}: {ctype} is not text (text, Markdown and HTML only)")
        return "", text, []
    raise DocError(f"{url}: too many redirects")


def refresh(doc: Doc) -> dict[str, Any]:
    """Fetch a site again (its first page, then pages under the same address, up to max_pages)."""
    start = doc.url or ""
    base = start.rsplit("/", 1)[0] + "/"
    todo, seen, texts, pages = [start], set(), [], []
    try:
        while todo and len(pages) < max(1, int(doc.max_pages or 1)):
            url = todo.pop(0).split("#")[0]
            if url in seen:
                continue
            seen.add(url)
            title, text, links = fetch(url)
            pages.append({"url": url, "title": title, "chars": len(text)})
            texts.append((f"# {title}\n" if title else "") + text)
            todo += [link for link in links if link.startswith(base) and link.split("#")[0] not in seen]
        content = "\n\n".join(texts)
        h = hashlib.sha256(content.encode()).hexdigest()[:40]
        changed = h != doc.content_hash
        doc.content, doc.pages, doc.content_hash = content, pages, h
        doc.title = doc.title or (pages[0]["title"] if pages else None) or start
        doc.status, doc.error = "ok", None
    except (DocError, requests.exceptions.RequestException) as ex:
        doc.status, doc.error, changed = "error", str(ex)[:500], False
    doc.fetched_at = dt.datetime.utcnow()
    db.session.commit()
    return {"id": doc.id, "status": doc.status, "pages": len(doc.pages or []), "changed": changed, "error": doc.error}


def due_docs() -> list[Doc]:
    now = dt.datetime.utcnow()
    return [d for d in db.session.query(Doc).filter(Doc.kind == "url", Doc.enabled.is_(True))
            if d.fetched_at is None or now - d.fetched_at > dt.timedelta(days=max(1, d.refresh_days or 7))]


def refresh_due() -> list[dict[str, Any]]:
    return [refresh(d) for d in due_docs()]
