"""Documents and sites (0.8): a sign-in sent to the document's own site only, the Confluence and Bitbucket readers
(Data Center and Cloud, through their REST APIs, paging included), text files only, max_pages, the secret never
said, and the search's pieces page by page. Every site here is a local mock HTTP server that answers like the
documented APIs."""

from __future__ import annotations

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

SECRET = "s3cr3t-Token-0815"


class Mock:
    """A local HTTP server: route(path, query, headers) -> (status, headers, body); it keeps what it was asked."""

    def __init__(self, route) -> None:
        self.route = route
        self.got: list[dict] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                query = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                headers = {k.lower(): v for k, v in self.headers.items()}
                mock.got.append({"path": u.path, "query": query, "headers": headers})
                status, extra, body = mock.route(u.path, query, headers)
                if isinstance(body, (dict, list)):
                    body = json.dumps(body)
                    extra = {"Content-Type": "application/json", **extra}
                body = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def servers():
    made: list[Mock] = []

    def make(route) -> Mock:
        m = Mock(route)
        made.append(m)
        return m

    yield make
    for m in made:
        m.close()


@pytest.fixture()
def env(ctx, monkeypatch):
    """The mock servers' address allowed (docs.allowed_domains), settings a test may change (`env[key] = ...`);
    the documents made here are removed after."""
    from superset.extensions import db

    from supagent import settings
    from supagent.models import Chunk, Doc

    over = {"docs.allowed_domains": ["127.0.0.1"]}
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k: over[k] if k in over else real_get(k))
    before = {i for (i,) in db.session.query(Doc.id)}
    yield over
    db.session.rollback()
    for d in db.session.query(Doc).filter(Doc.id.notin_(before or {-1})):
        db.session.query(Chunk).filter(Chunk.ref.like(f"doc:{d.id}#%")).delete(synchronize_session=False)
        db.session.delete(d)
    db.session.commit()


def _doc(url: str, max_pages: int = 10, **kw):
    from superset.extensions import db

    from supagent.models import Doc

    d = Doc(kind="url", url=url, max_pages=max_pages, **kw)
    db.session.add(d)
    db.session.commit()
    return d


def _html(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


HTML = {"Content-Type": "text/html; charset=utf-8"}


# --------------------------------------------------------------------------------------------- #
# the address says which reader
# --------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,reader", [
    ("https://wiki.example.com/display/OPS/Run+book", "confluence"),
    ("https://wiki.example.com/confluence/pages/viewpage.action?pageId=123", "confluence"),
    ("https://wiki.example.com/spaces/OPS/pages/123/Run+book", "confluence"),
    ("https://example.atlassian.net/wiki/spaces/OPS/pages/123/Run+book", "confluence"),
    ("https://example.atlassian.net/wiki/spaces/OPS/overview", "confluence"),
    ("https://wiki.example.com/display/OPS", "confluence"),
    ("https://git.example.com/projects/OPS/repos/runbooks/browse/docs?at=refs%2Fheads%2Fmain", "bitbucket"),
    ("https://git.example.com/bitbucket/users/alice/repos/notes/browse", "bitbucket"),
    ("https://bitbucket.org/acme/runbooks/src/main/docs/", "bitbucket"),
    ("https://bitbucket.org/acme/runbooks", "bitbucket"),
    ("https://github.com/acme/runbooks/tree/main/docs", "web"),
    ("https://wiki.example.com/ops/runbooks/", "web"),
    ("https://wiki.example.com/pages/viewpage.action", "web"),            # no page id: an ordinary page
])
def test_the_reader_is_detected_from_the_address(app, url, reader):
    from supagent.knowledge import docs as D

    assert D.detect_reader(url) == reader


def test_what_an_address_names(app):
    from supagent.knowledge import docs as D

    t = D.confluence_target("https://wiki.example.com/confluence/display/OPS/Run+book")
    assert t == {"kind": "title", "api": "https://wiki.example.com/confluence/rest/api",
                 "web": "https://wiki.example.com/confluence", "space": "OPS", "title": "Run book"}
    t = D.confluence_target("https://x.atlassian.net/wiki/spaces/OPS/pages/42/Title")
    assert (t["kind"], t["id"], t["api"]) == ("page", "42", "https://x.atlassian.net/wiki/rest/api")
    t = D.confluence_target("https://wiki.example.com/pages/viewpage.action?pageId=7")
    assert (t["kind"], t["id"], t["api"]) == ("page", "7", "https://wiki.example.com/rest/api")
    t = D.bitbucket_target("https://git.example.com/bb/projects/OPS/repos/runbooks/browse/docs/howto"
                           "?at=refs%2Fheads%2Fdev")
    assert t["api"] == "https://git.example.com/bb/rest/api/1.0/projects/OPS/repos/runbooks"
    assert (t["at"], t["path"], t["cloud"]) == ("refs/heads/dev", "docs/howto", False)
    t = D.bitbucket_target("https://git.example.com/users/alice/repos/notes/browse")
    assert t["api"] == "https://git.example.com/rest/api/1.0/projects/~alice/repos/notes" and t["path"] == ""
    t = D.bitbucket_target("https://bitbucket.org/acme/runbooks/src/main/docs/")
    assert t["api"] == "https://api.bitbucket.org/2.0/repositories/acme/runbooks/src"
    assert (t["ref"], t["path"], t["cloud"]) == ("main", "docs", True)


# --------------------------------------------------------------------------------------------- #
# the sign-in
# --------------------------------------------------------------------------------------------- #
def test_the_sign_in_goes_to_the_documents_own_site_only(ctx):
    from supagent.knowledge import docs as D
    from supagent.models import Doc

    d = Doc(kind="url", url="https://wiki.example.com/ops/", auth={"type": "bearer"}, secret=SECRET)
    s = D.sign_in(d)
    assert s.for_url("https://wiki.example.com/ops/a?x=1") == {"Authorization": f"Bearer {SECRET}"}
    for other in ("http://wiki.example.com/ops/a", "https://wiki.example.com:8443/ops/a", "https://evil.example.com/",
                  "https://wiki.example.com.evil.example.org/"):
        assert s.for_url(other) == {}, other
    basic = Doc(kind="url", url="https://wiki.example.com/", auth={"type": "basic", "user": "bob@example.com"},
                secret=SECRET)
    pair = base64.b64encode(f"bob@example.com:{SECRET}".encode()).decode()
    assert D.sign_in(basic).for_url("https://wiki.example.com/") == {"Authorization": f"Basic {pair}"}
    header = Doc(kind="url", url="https://wiki.example.com/", auth={"type": "header", "header": "X-Api-Key"},
                 secret=SECRET)
    assert D.sign_in(header).for_url("https://wiki.example.com/") == {"X-Api-Key": SECRET}
    with pytest.raises(D.DocError, match="header name"):
        D.sign_in(Doc(kind="url", url="https://w.example.com/", auth={"type": "header", "header": "Bad Name:"},
                      secret=SECRET))
    with pytest.raises(D.DocError, match="no token or password"):
        D.sign_in(Doc(kind="url", url="https://w.example.com/", auth={"type": "bearer"}))
    assert D.sign_in(Doc(kind="url", url="https://w.example.com/")) is None
    cloud = Doc(kind="url", url="https://bitbucket.org/acme/runbooks/src/main/", auth={"type": "bearer"},
                secret=SECRET)
    sc = D.sign_in(cloud)                                    # its pages' site and its API's
    assert sc.for_url("https://api.bitbucket.org/2.0/repositories/acme/runbooks/src/main/") and \
        sc.for_url("https://bitbucket.org/acme/runbooks") and not sc.for_url("https://evil.example.com/")
    shown = D.describe_auth(basic)
    assert shown == {"type": "basic", "user": "bob@example.com", "header": None, "secret_set": True}
    assert SECRET not in json.dumps(shown) and pair not in json.dumps(shown)
    plain = D.sign_in(Doc(kind="url", url="http://wiki.example.com/ops/", auth={"type": "bearer"}, secret=SECRET))
    assert plain.for_url("https://wiki.example.com/ops/") and not plain.for_url("http://wiki.example.com:8080/")
    assert not s.for_url("http://wiki.example.com/ops/")             # https to http: never
    assert D.describe_auth(Doc(kind="url", url="https://w.example.com/")) == {"type": None, "user": None,
                                                                             "header": None, "secret_set": False}
    said = D.scrub(f"failed: {SECRET} / Basic {pair}", basic)
    assert SECRET not in said and pair not in said and said.startswith("failed: ***")


def test_the_page_sets_the_reader_and_the_sign_in(ctx):
    from supagent.knowledge import docs as D
    from supagent.models import Doc

    d = Doc(kind="url", url="https://wiki.example.com/ops/")
    D.configure(d, {"reader": "", "auth": {"type": "bearer"}, "secret": f"  {SECRET} "})
    assert d.auth == {"type": "bearer"} and d.secret == SECRET and d.reader is None
    D.configure(d, {"auth": {"type": "bearer"}, "secret": ""})            # an empty box keeps the secret
    assert d.secret == SECRET
    D.configure(d, {"reader": "confluence"})
    assert d.reader == "confluence" and d.secret == SECRET
    old = d.url
    d.url = "https://elsewhere.example.org/ops/"                          # another site: never its secret
    with pytest.raises(D.DocError, match="another site"):
        D.configure(d, {"auth": {"type": "bearer"}}, previous_url=old)
    assert d.secret is None
    D.configure(d, {"auth": {"type": "bearer"}, "secret": "new-token"}, previous_url=old)
    assert d.secret == "new-token"
    D.configure(d, {"auth": {"type": "none"}})
    assert d.auth is None and d.secret is None
    with pytest.raises(D.DocError, match="user"):
        D.configure(d, {"auth": {"type": "basic"}, "secret": "x"})
    with pytest.raises(D.DocError, match="header"):
        D.configure(Doc(kind="url", url="https://w.example.com/"), {"auth": {"type": "header", "header": ""},
                                                                    "secret": "x"})
    with pytest.raises(D.DocError, match="reader"):
        D.configure(Doc(kind="url", url="https://w.example.com/"), {"reader": "github"})
    with pytest.raises(D.DocError, match="how the token is sent"):
        D.configure(Doc(kind="url", url="https://w.example.com/"), {"secret": "x"})
    with pytest.raises(D.DocError, match="write the token"):
        D.configure(Doc(kind="url", url="https://w.example.com/"), {"auth": {"type": "bearer"}})


def test_a_site_with_a_sign_in_and_redirects(env, servers):
    """Same site: the header follows; another site (a redirect there): never; links outside the address: not read."""
    from supagent.knowledge import docs as D

    other = servers(lambda path, q, h: (200, HTML, _html("Public", "<p>Public page text.</p>")))

    def site(path, q, h):
        if h.get("x-api-key") != SECRET:
            return 401, {}, "sign in"
        if path == "/docs/":
            return 200, HTML, _html("Ops docs", "<p>Start.</p><a href='/docs/a'>a</a> <a href='/docs/b'>b</a>"
                                                "<a href='/docs/c#top'>c</a> <a href='/elsewhere/x'>x</a>")
        if path == "/docs/a":
            return 302, {"Location": "/docs/a2"}, ""
        if path == "/docs/a2":
            return 200, HTML, _html("A", "<p>Pool sizing for workers.</p>")
        if path == "/docs/b":
            return 302, {"Location": f"{other.base}/public"}, ""
        if path == "/docs/c":
            return 200, HTML, _html("C", "<p>Restart order: db, then web.</p>")
        return 404, {}, "no"

    a = servers(site)
    d = _doc(a.base + "/docs/", auth={"type": "header", "header": "X-Api-Key"}, secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "ok", out
    assert out["reader"] == "web" and out["pages"] == 4
    assert all(r["headers"].get("x-api-key") == SECRET for r in a.got)               # its own site: signed in
    assert other.got and all("x-api-key" not in r["headers"] and "authorization" not in r["headers"]
                             for r in other.got)                                   # another site: never
    assert not any(r["path"] == "/elsewhere/x" for r in a.got)
    for text in ("Pool sizing for workers.", "Public page text.", "Restart order: db, then web."):
        assert text in d.content
    for p in d.pages:                                          # each page: where its text is in the document
        assert d.content[p["at"]:p["at"] + p["chars"]].startswith(f"# {p['title']}\n")
    assert d.title == "Ops docs"


def test_a_login_page_or_401_is_a_clear_error_that_never_says_the_secret(env, servers, caplog, monkeypatch):
    from supagent.knowledge import docs as D

    def site(path, q, h):
        if path == "/wiki/":
            return 302, {"Location": "/login.action?os_destination=%2Fwiki%2F"}, ""
        return 401, {"WWW-Authenticate": "Bearer"}, "no"

    s = servers(site)
    caplog.set_level(logging.DEBUG)
    d = _doc(s.base + "/wiki/", auth={"type": "bearer"}, secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "error" and "asked to sign in" in d.error and "login page" in d.error
    assert "check the token" in d.error
    d2 = _doc(s.base + "/api/x", auth={"type": "basic", "user": "bob"}, secret=SECRET)
    D.refresh(d2)
    assert d2.status == "error" and "HTTP 401" in d2.error and "asked to sign in" in d2.error
    d3 = _doc(s.base + "/api/y")                                     # no sign-in: it says to give one
    D.refresh(d3)
    assert "give this document a sign-in" in d3.error
    monkeypatch.setattr(D, "_read_web", lambda doc, sign: (_ for _ in ()).throw(
        D.DocError(f"the server echoed {SECRET} and {sign.headers['Authorization']}")))
    D.refresh(d2)                                                     # whatever an error says: never the secret
    pair = base64.b64encode(f"bob:{SECRET}".encode()).decode()
    assert d2.status == "error" and "***" in d2.error and SECRET not in d2.error and pair not in d2.error
    for doc in (d, d2, d3):
        assert SECRET not in (doc.error or "")
    assert SECRET not in caplog.text and pair not in caplog.text


# --------------------------------------------------------------------------------------------- #
# Confluence
# --------------------------------------------------------------------------------------------- #
STORAGE = ("<h1>Runbooks</h1><p>How the batch runs.</p>"
           '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sqlite</ac:parameter>'
           "<ac:plain-text-body><![CDATA[SELECT count(*) FROM jobs\nWHERE status = 'failed';]]></ac:plain-text-body>"
           "</ac:structured-macro><table><tbody><tr><th>Pool</th><th>Size</th></tr><tr><td>p7</td><td>32</td></tr>"
           "</tbody></table>")


def _confluence(prefix: str, token_ok, base_of):
    """A Confluence REST API under `prefix` (Data Center: the context path; Cloud: /wiki): pages 100 (Runbooks, its
    children 101, 102, 103, paged by `limit`), 101's child 104, a space OPS (pages 100-104), a page titled Run book."""
    titles = {"100": "Runbooks", "101": "Restart", "102": "Pools", "103": "Alerts", "104": "Old alerts",
              "300": "Run book"}
    children = {"100": ["101", "102", "103"], "101": ["104"]}

    def page(pid: str, body: bool = True) -> dict:
        item = {"id": pid, "type": "page", "title": titles[pid], "status": "current",
                "_links": {"webui": f"/display/OPS/{titles[pid].replace(' ', '+')}",
                           "self": f"/rest/api/content/{pid}"}}
        if body:
            text = STORAGE if pid == "100" else f"<p>The page {titles[pid]} of space OPS.</p>"
            item.update(body={"storage": {"value": text, "representation": "storage"}}, version={"number": 3},
                        space={"key": "OPS"})
        return item

    def route(path, q, h):
        if not token_ok(h):
            return 401, {}, {"statusCode": 401, "message": "not signed in"}
        api = prefix + "/rest/api"
        if not path.startswith(api):
            return 404, {}, {"statusCode": 404}
        rest = path[len(api):]
        limit, start = int(q.get("limit") or 25), int(q.get("start") or 0)
        links = {"base": base_of(), "context": prefix}
        if rest.startswith("/content/") and rest.endswith("/child/page"):
            pid = rest.split("/")[2]
            kids = children.get(pid, [])
            part = kids[start:start + limit]
            more = start + limit < len(kids)
            return 200, {}, {"results": [page(k, body=False) for k in part], "start": start, "limit": limit,
                             "size": len(part), "_links": {**links, **({"next": f"/rest/api/content/{pid}/child/page?"
                                                                                f"limit={limit}&start={start + limit}"}
                                                                       if more else {})}}
        if rest.startswith("/content/"):
            pid = rest.split("/")[2]
            if pid not in titles:
                return 404, {}, {"statusCode": 404}
            return 200, {}, {**page(pid), "_links": {**page(pid)["_links"], "base": base_of()}}
        if rest == "/content" and q.get("title"):
            found = [k for k, v in titles.items() if v == q["title"]]
            return 200, {}, {"results": [page(k) for k in found], "start": 0, "limit": 25, "size": len(found),
                             "_links": links}
        if rest == "/content" and q.get("spaceKey") == "OPS":
            space = ["100", "101", "102", "103", "104"]
            part = space[start:start + limit]
            more = start + limit < len(space)
            return 200, {}, {"results": [page(k) for k in part], "start": start, "limit": limit, "size": len(part),
                             "_links": {**links, **({"next": "/rest/api/content?next"} if more else {})}}
        return 404, {}, {"statusCode": 404}

    return route


def test_a_confluence_data_center_page_and_the_pages_under_it(env, servers, monkeypatch):
    from supagent.knowledge import docs as D

    monkeypatch.setattr(D, "CHILDREN_LIST", 2)                       # paging of the children
    holder: dict = {}
    s = servers(_confluence("/confluence", lambda h: h.get("authorization") == f"Bearer {SECRET}",
                            lambda: holder["base"] + "/confluence"))
    holder["base"] = s.base
    d = _doc(f"{s.base}/confluence/pages/viewpage.action?pageId=100", max_pages=4, auth={"type": "bearer"},
             secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "ok" and out["reader"] == "confluence", out
    assert [p["title"] for p in d.pages] == ["Runbooks", "Restart", "Pools", "Alerts"]     # breadth first, 4 at most
    assert d.pages[1]["url"] == f"{s.base}/confluence/display/OPS/Restart"
    assert "SELECT count(*) FROM jobs\nWHERE status = 'failed';" in d.content           # a code macro's text
    assert "Pool | Size" in d.content and "p7 | 32" in d.content                         # table cells
    assert "sqlite" not in d.content                                                     # a macro's parameter
    asked = [(r["path"], r["query"].get("start")) for r in s.got]
    assert ("/confluence/rest/api/content/100/child/page", "0") in asked and \
        ("/confluence/rest/api/content/100/child/page", "2") in asked                    # pages of children
    assert not any(r["path"] == "/confluence/rest/api/content/104" for r in s.got)       # beyond max_pages
    assert d.title == "Runbooks"
    t = _doc(f"{s.base}/confluence/display/OPS/Run+book", auth={"type": "bearer"}, secret=SECRET)
    out = D.refresh(t)                                               # by its title
    assert out["status"] == "ok" and [p["title"] for p in t.pages] == ["Run book"], out
    bad = _doc(f"{s.base}/confluence/pages/viewpage.action?pageId=100", auth={"type": "bearer"}, secret="wrong")
    D.refresh(bad)
    assert bad.status == "error" and "asked to sign in" in bad.error and "wrong" not in bad.error


def test_a_confluence_cloud_space_with_a_user_and_api_token(env, servers, monkeypatch):
    from supagent.knowledge import docs as D

    monkeypatch.setattr(D, "CONFLUENCE_LIST", 2)                     # paging of the space's pages
    pair = "Basic " + base64.b64encode(f"alice@example.com:{SECRET}".encode()).decode()
    holder: dict = {}
    s = servers(_confluence("/wiki", lambda h: h.get("authorization") == pair, lambda: holder["base"] + "/wiki"))
    holder["base"] = s.base
    d = _doc(f"{s.base}/wiki/spaces/OPS/overview", auth={"type": "basic", "user": "alice@example.com"},
             secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "ok" and out["pages"] == 5, out
    assert [r["query"].get("start") for r in s.got] == ["0", "2", "4"]
    assert d.title == "Confluence space OPS" and d.pages[0]["url"] == f"{s.base}/wiki/display/OPS/Runbooks"
    d2 = _doc(f"{s.base}/wiki/spaces/OPS/overview", max_pages=3, auth={"type": "basic", "user": "alice@example.com"},
              secret=SECRET)
    assert D.refresh(d2)["pages"] == 3                                # max_pages


# --------------------------------------------------------------------------------------------- #
# Bitbucket
# --------------------------------------------------------------------------------------------- #
def test_a_bitbucket_data_center_folder_documentation_first(env, servers, monkeypatch):
    from supagent.knowledge import docs as D

    monkeypatch.setattr(D, "REPO_LIST", 3)                           # paging of the listing
    env["docs.max_kb"] = 1                                            # big.md is larger: skipped
    files = {"guide/setup.md": "# Setup\nInstall the agent.", "README.md": "# Runbooks\nStart here.",
             "img/logo.png": "\x89PNG", "jobs.sql": "SELECT * FROM jobs;", "big.md": "x" * 3000,
             "notes.txt": "Night shift notes.", "config/app.yaml": "pool: 7", "tool.exe": "MZ"}
    api = "/bb/rest/api/1.0/projects/OPS/repos/runbooks"

    def route(path, q, h):
        if h.get("authorization") != f"Bearer {SECRET}":
            return 401, {}, {"errors": [{"message": "Authentication required"}]}
        if q.get("at") != "refs/heads/main":
            return 404, {}, {"errors": [{"message": "no such ref"}]}
        if path == api + "/files/docs":
            names, limit, start = list(files), int(q["limit"]), int(q.get("start") or 0)
            part = names[start:start + limit]
            last = start + limit >= len(names)
            return 200, {}, {"size": len(part), "limit": limit, "isLastPage": last, "values": part, "start": start,
                             **({} if last else {"nextPageStart": start + limit})}
        if path.startswith(api + "/raw/docs/"):
            name = path[len(api + "/raw/docs/"):]
            return (200, {"Content-Type": "text/plain"}, files[name]) if name in files else (404, {}, "no")
        return 404, {}, "no"

    s = servers(route)
    d = _doc(f"{s.base}/bb/projects/OPS/repos/runbooks/browse/docs?at=refs%2Fheads%2Fmain", max_pages=4,
             auth={"type": "bearer"}, secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "ok" and out["reader"] == "bitbucket", out
    assert [p["title"] for p in d.pages] == ["docs/README.md", "docs/notes.txt", "docs/guide/setup.md",
                                            "docs/jobs.sql"]          # documentation first; 4 at most
    assert out["skipped"] == 1 and "x" * 2000 not in d.content       # the large one skipped
    raw = [r["path"] for r in s.got if "/raw/" in r["path"]]
    assert not any(p.endswith((".png", ".exe", "app.yaml")) for p in raw)   # text files only; beyond max_pages
    assert [r["query"].get("start") for r in s.got if r["path"].endswith("/files/docs")] == ["0", "3", "6"]
    assert d.pages[0]["url"] == f"{s.base}/bb/projects/OPS/repos/runbooks/browse/docs/README.md?at=refs%2Fheads%2Fmain"
    assert d.title == "OPS/runbooks docs"
    one = _doc(f"{s.base}/bb/projects/OPS/repos/runbooks/browse/docs/notes.txt?at=refs%2Fheads%2Fmain",
               auth={"type": "bearer"}, secret=SECRET)              # the address of one file: that file
    listed = len([r for r in s.got if "/files/" in r["path"]])
    out = D.refresh(one)
    assert out["status"] == "ok" and [p["title"] for p in one.pages] == ["docs/notes.txt"], out
    assert len([r for r in s.got if "/files/" in r["path"]]) == listed
    gone = _doc(f"{s.base}/bb/projects/OPS/repos/runbooks/browse/docs/missing.md?at=refs%2Fheads%2Fmain",
                auth={"type": "bearer"}, secret=SECRET)
    D.refresh(gone)
    assert gone.status == "error" and "not found, or not visible to this document's sign-in" in gone.error


def test_a_bitbucket_cloud_folder_through_its_api_host(env, servers, monkeypatch):
    """Bitbucket Cloud: the pages' site never asked, the API host (another origin) signed in."""
    from supagent.knowledge import docs as D

    pair = "Basic " + base64.b64encode(f"bob:{SECRET}".encode()).decode()
    web = servers(lambda path, q, h: (500, {}, "the pages' site must not be asked"))
    holder: dict = {}

    def route(path, q, h):
        if h.get("authorization") != pair:
            return 401, {}, {"type": "error"}
        b = holder["base"] + "/2.0/repositories/acme/runbooks/src/main"
        root = "/2.0/repositories/acme/runbooks/src/main"
        if path == root + "/docs/" and q.get("page") != "2":
            return 200, {}, {"pagelen": 2, "page": 1, "next": f"{b}/docs/?pagelen=2&page=2", "values": [
                {"type": "commit_directory", "path": "docs/howto", "links": {"self": {"href": f"{b}/docs/howto/"}}},
                {"type": "commit_file", "path": "docs/a.md", "size": 20, "commit": {"hash": "abc123"},
                 "links": {"self": {"href": f"{b}/docs/a.md"}}}]}
        if path == root + "/docs/":
            return 200, {}, {"pagelen": 2, "page": 2, "values": [
                {"type": "commit_file", "path": "docs/logo.png", "size": 5,
                 "links": {"self": {"href": f"{b}/docs/logo.png"}}},
                {"type": "commit_file", "path": "docs/b.rst", "size": 20,
                 "links": {"self": {"href": f"{b}/docs/b.rst"}}}]}
        if path == root + "/docs/howto/":
            return 200, {}, {"pagelen": 100, "page": 1, "values": [
                {"type": "commit_file", "path": "docs/howto/c.md", "size": 20,
                 "links": {"self": {"href": f"{b}/docs/howto/c.md"}}}]}
        texts = {root + "/docs/a.md": "A: alerts.", root + "/docs/b.rst": "B: backups.",
                 root + "/docs/howto/c.md": "C: capacity."}
        return (200, {"Content-Type": "text/plain"}, texts[path]) if path in texts else (404, {}, "no")

    api = servers(route)
    holder["base"] = api.base
    monkeypatch.setattr(D, "CLOUD_APIS", {web.base.split("//", 1)[1]: api.base + "/2.0"})
    d = _doc(f"{web.base}/acme/runbooks/src/main/docs/", auth={"type": "basic", "user": "bob"}, secret=SECRET)
    out = D.refresh(d)
    assert out["status"] == "ok" and out["reader"] == "bitbucket", out
    assert [p["title"] for p in d.pages] == ["docs/a.md", "docs/b.rst", "docs/howto/c.md"]
    assert not web.got and all(r["headers"].get("authorization") == pair for r in api.got)
    assert not any(r["path"].endswith("logo.png") for r in api.got)
    assert d.pages[0]["url"] == f"{web.base}/acme/runbooks/src/main/docs/a.md"


# --------------------------------------------------------------------------------------------- #
# the search: pieces page by page
# --------------------------------------------------------------------------------------------- #
def test_the_pieces_are_made_page_by_page_with_their_address(env, servers):
    from superset.extensions import db

    from supagent.knowledge import docs as D
    from supagent.knowledge.index import pieces
    from supagent.models import Doc

    long = " ".join(f"Sentence {i} about pool sizing." for i in range(200))       # several pieces
    s = servers(lambda path, q, h: {
        "/kb/": (200, HTML, _html("KB", "<p>Index.</p><a href='/kb/pools'>p</a> <a href='/kb/restart'>r</a>")),
        "/kb/pools": (200, HTML, _html("Pools", f"<p>{long}</p>")),
        "/kb/restart": (200, HTML, _html("Restart", "<p>Restart order: db, then web.</p>")),
    }.get(path, (404, {}, "no")))
    d = _doc(s.base + "/kb/", category="Runbooks")
    assert D.refresh(d)["status"] == "ok"
    mine = [p for p in pieces(("doc:",)) if p["ref"].startswith(f"doc:{d.id}#")]
    assert [p["ref"] for p in mine] == [f"doc:{d.id}#{i}" for i in range(len(mine))]
    pools = [p for p in mine if p["title"] == "KB › Pools (Runbooks)"]
    assert len(pools) >= 2 and all(p["text"].startswith(f"Source: {s.base}/kb/pools\n") for p in pools)
    restart = [p for p in mine if "Restart order" in p["text"]]
    assert len(restart) == 1 and restart[0]["title"] == "KB › Restart (Runbooks)"
    assert not any("Restart order" in p["text"] and "pool sizing" in p["text"] for p in mine)   # never two pages
    assert mine[0]["title"] == "KB (Runbooks)"                     # the page named like its document: once
    old = Doc(kind="url", url="https://w.example.com/", title="Old", content="Old text. " * 300, status="ok",
              pages=[{"url": "https://w.example.com/", "title": "Old", "chars": 3000}])   # read by 0.7: one text
    up = Doc(kind="upload", title="Upload", content="Uploaded text.", status="ok")
    db.session.add_all([old, up])
    db.session.commit()
    got = {p["ref"]: p for p in pieces(("doc:",))}
    assert got[f"doc:{old.id}#0"]["title"] == "Old" and not got[f"doc:{old.id}#0"]["text"].startswith("Source:")
    assert got[f"doc:{up.id}#0"] == {"ref": f"doc:{up.id}#0", "kind": "doc", "title": "Upload",
                                     "text": "Uploaded text."}


def test_an_intranet_address_says_which_setting_allows_it(app, ctx):
    from supagent.knowledge import docs as D

    with pytest.raises(D.DocError) as ex:
        D.check_url("http://127.0.0.1:8088/wiki/")
    assert "private" in str(ex.value) and "docs.allowed_domains" in str(ex.value)
