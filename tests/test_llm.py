"""The LLM client and its authentication modes, against local HTTPS mock servers: a middleware
(OAuth2 client credentials, HTTP Basic + client certificate, short-lived tokens) and an
OpenAI-compatible LLM that only accepts the tokens the middleware issued, until they expire."""

from __future__ import annotations

import base64
import json
import secrets
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from supagent import llm as L

KEY, SECRET = "consumer-1", "s3cret-value"


def _openssl(*args: str, cwd: str) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> dict[str, str]:
    d = str(tmp_path_factory.mktemp("pki"))
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
             "-subj", "/CN=test CA", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=127.0.0.1",
             cwd=d)
    open(f"{d}/srv.ext", "w").write("subjectAltName=IP:127.0.0.1\n")
    _openssl("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
             "srv.pem", "-days", "2", "-extfile", "srv.ext", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "cli.key", "-out", "cli.csr", "-subj", "/CN=agent",
             cwd=d)
    _openssl("x509", "-req", "-in", "cli.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
             "cli.pem", "-days", "2", cwd=d)
    return {k: f"{d}/{k}" for k in ("ca.pem", "srv.pem", "srv.key", "cli.pem", "cli.key")}


class State:
    def __init__(self) -> None:
        self.tokens: dict[str, float] = {}      # token -> expiry
        self.token_requests = 0
        self.chat_calls = 0
        self.expires_in = 2
        self.bad_answer = False


def _serve(handler_cls, pki: dict[str, str], require_client_cert: bool) -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(pki["srv.pem"], pki["srv.key"])
    if require_client_cert:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(pki["ca.pem"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture(scope="module")
def servers(pki):
    state = State()

    class Token(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            auth = self.headers.get("Authorization", "")
            ok = auth == "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
            if "grant_type=client_credentials" not in body or not ok:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error": "invalid_client"}')
                return
            state.token_requests += 1
            token = secrets.token_hex(16)
            state.tokens[token] = time.time() + state.expires_in
            answer = {"access_token": token, "token_type": "Bearer"}
            if not state.bad_answer:
                answer["expires_in"] = state.expires_in
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(answer).encode())

    class Chat(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer fixed-token":
                return True
            token = auth.removeprefix("Bearer ")
            return state.tokens.get(token, 0) > time.time()

        def _send(self, code: int, obj: dict) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._send(401, {"error": "invalid token"})
            self._send(200, {"data": [{"id": "test-model"}]})

        def do_POST(self):  # noqa: N802
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not self._authorized():
                return self._send(401, {"error": "invalid token"})
            state.chat_calls += 1
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": f"OK ({req['model']})"}}]})

    tsrv, tport = _serve(Token, pki, require_client_cert=True)
    csrv, cport = _serve(Chat, pki, require_client_cert=False)
    yield state, f"https://127.0.0.1:{tport}/oauth/token", f"https://127.0.0.1:{cport}/v1/openai"
    tsrv.shutdown()
    csrv.shutdown()


def _cfg(pki, url, base, **kw) -> L.LLMConfig:
    values = dict(base_url=base, auth="middleware", token_url=url, consumer_key=KEY, consumer_secret=SECRET,
                  cert_path=pki["cli.pem"], key_path=pki["cli.key"], ca_bundle=pki["ca.pem"], renew_before=60)
    values.update(kw)
    return L.LLMConfig(**values)


def test_fixed_token(servers, pki):
    _state, _token_url, base = servers
    client = L.LLM(L.LLMConfig(base_url=base, auth="token", token="fixed-token", ca_bundle=pki["ca.pem"]))
    assert client.chat([{"role": "user", "content": "hi"}])["content"] == "OK (test-model)"
    with pytest.raises(L.LLMError, match="HTTP 401"):
        L.LLM(L.LLMConfig(base_url=base, auth="token", token="wrong", ca_bundle=pki["ca.pem"])).chat(
            [{"role": "user", "content": "hi"}])


def test_middleware_token_is_cached_then_renewed(servers, pki):
    state, token_url, base = servers
    L._TOKENS.clear()
    state.expires_in = 3
    client = L.LLM(_cfg(pki, token_url, base))
    before = state.token_requests
    for _ in range(3):
        client.chat([{"role": "user", "content": "hi"}])
    assert state.token_requests == before + 1            # one token for the three calls
    time.sleep(2.2)                                      # renewed before expiry (margin: half of 3 s)
    client.chat([{"role": "user", "content": "hi"}])
    assert state.token_requests == before + 2


def test_middleware_401_renews_once(servers, pki):
    state, token_url, base = servers
    L._TOKENS.clear()
    state.expires_in = 30
    client = L.LLM(_cfg(pki, token_url, base))
    client.chat([{"role": "user", "content": "hi"}])
    state.tokens.clear()                                 # the gateway forgot every token (revoked)
    before = state.token_requests
    assert client.chat([{"role": "user", "content": "hi"}])["content"].startswith("OK")
    assert state.token_requests == before + 1


@pytest.mark.parametrize("change, words", [
    ({"consumer_secret": "wrong"}, ["HTTP 401", "consumer key / secret"]),
    ({"cert_path": "", "key_path": ""}, ["TLS refused", "cert_path"]),
    ({"ca_bundle": ""}, ["TLS refused"]),
    ({"token_url": ""}, ["token_url is empty"]),
])
def test_middleware_refusals_say_why(servers, pki, change, words):
    _state, token_url, base = servers
    L._TOKENS.clear()
    with pytest.raises(L.LLMError) as err:
        L.LLM(_cfg(pki, token_url, base, **change)).chat([{"role": "user", "content": "hi"}])
    for w in words:
        assert w in str(err.value)


def test_middleware_answer_without_expires_in(servers, pki):
    """No expires_in: the token is kept DEFAULT_TOKEN_SECONDS (minus the renewal margin), and a
    401 of the LLM renews it earlier if it expires before."""
    state, token_url, base = servers
    L._TOKENS.clear()
    state.bad_answer = True
    try:
        cfg = _cfg(pki, token_url, base)
        L.LLM(cfg).chat([{"role": "user", "content": "hi"}])
        (_token, until), = L._TOKENS.values()
        left = until - time.time()
        assert L.DEFAULT_TOKEN_SECONDS - cfg.renew_before - 5 < left <= L.DEFAULT_TOKEN_SECONDS
    finally:
        state.bad_answer = False
        L._TOKENS.clear()


def test_check_reports_without_the_token(servers, pki):
    _state, token_url, base = servers
    L._TOKENS.clear()
    out = L.LLM(_cfg(pki, token_url, base)).check()
    assert out["answer"].startswith("OK") and out["model"] == "test-model" and "..." in out["token"]


def test_a_restarting_server_is_waited_for(monkeypatch):
    """llama.cpp answers 503 "Loading model" while it restarts: the call waits, then succeeds."""
    calls = {"n": 0}

    class Loading(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls["n"] += 1
            if calls["n"] <= 2:
                body, code = b'{"error": {"message": "Loading model", "code": 503}}', 503
            else:
                body, code = json.dumps({"choices": [{"message": {"role": "assistant", "content": "OK"}}]}).encode(), 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Loading)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(L, "RETRY_DELAYS", (0.05, 0.05, 0.05))
    try:
        client = L.LLM(L.LLMConfig(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="m"))
        assert client.chat([{"role": "user", "content": "hi"}])["content"] == "OK"
        assert calls["n"] == 3
    finally:
        srv.shutdown()


@pytest.mark.parametrize("value, expected", [(3599, 3599.0), ("3599", 3599.0), ("120.5", 120.5), (None, 300.0),
                                             ("soon", 300.0), (0, 300.0)])
def test_expires_in_as_number_string_or_missing(value, expected):
    assert L._seconds(value) == expected


def test_a_gateway_sending_expires_in_as_a_string(monkeypatch):
    class Answer:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"access_token": "tok-str", "expires_in": "3599"}

    monkeypatch.setattr(L.requests, "post", lambda *a, **k: Answer())
    cfg = L.LLMConfig(base_url="https://llm.example/openai", auth="middleware",
                      token_url="https://mw.example/oauth2/token-string", consumer_key="k", consumer_secret="s")
    assert L.auth_headers(cfg) == {"Authorization": "Bearer tok-str"}
