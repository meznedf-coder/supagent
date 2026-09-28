"""A stand-in for a company LLM gateway, to test supagent's middleware authentication end to end.

  token endpoint  https://127.0.0.1:9460/oauth2/token
                  mutual TLS (client certificate signed by the lab CA) + HTTP Basic (consumer
                  key / secret), POST grant_type=client_credentials -> {"access_token", "expires_in"}
  LLM endpoint    https://127.0.0.1:9461/openai/...  (models, chat/completions, embeddings)
                  Authorization: Bearer <token> valid and not expired, else 401; forwarded to
                  UPSTREAM (an OpenAI-compatible server), the model name mapped to the upstream one

    python mock_llm_gateway.py DIR        (DIR: certificates and consumer.env, see make_certs())

Only for a lab: it listens on 127.0.0.1 and keeps its tokens in memory.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8080/v1").rstrip("/")
EMBED_UPSTREAM = os.environ.get("EMBED_UPSTREAM", "http://127.0.0.1:8091/v1").rstrip("/")   # /embeddings
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "120"))
TOKENS: dict[str, float] = {}
STATS = {"tokens": 0, "refused_tokens": 0, "llm_calls": 0, "embedding_calls": 0, "refused_calls": 0}
LOCK = threading.Lock()


def make_certs(d: str) -> None:
    """Lab CA, server certificate for 127.0.0.1, client certificate, consumer key / secret."""
    os.makedirs(d, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    if not os.path.exists(os.path.join(d, "ca.pem")):
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "365",
            "-subj", "/CN=supagent lab CA")
        with open(os.path.join(d, "server.ext"), "w", encoding="utf-8") as fh:
            fh.write("subjectAltName=IP:127.0.0.1,DNS:localhost\n")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=127.0.0.1")
        run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "server.pem", "-days", "365", "-extfile", "server.ext")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "client.key", "-out", "client.csr",
            "-subj", "/CN=superset-agent")
        run("x509", "-req", "-in", "client.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "client.pem", "-days", "365")
    env = os.path.join(d, "consumer.env")
    if not os.path.exists(env):
        with open(env, "w", encoding="utf-8") as fh:
            fh.write(f"CONSUMER_KEY=agent-{secrets.token_hex(4)}\nCONSUMER_SECRET={secrets.token_urlsafe(24)}\n")
    for name in os.listdir(d):
        if name.endswith((".key", ".env")):
            os.chmod(os.path.join(d, name), 0o600)


def consumer(d: str) -> tuple[str, str]:
    values = dict(line.strip().split("=", 1) for line in open(os.path.join(d, "consumer.env"), encoding="utf-8")
                  if "=" in line)
    return values["CONSUMER_KEY"], values["CONSUMER_SECRET"]


class TokenHandler(BaseHTTPRequestHandler):
    key = secret = ""

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = self.rfile.read(length).decode()
        auth = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        if self.path != "/oauth2/token" or auth != expected or "grant_type=client_credentials" not in form:
            with LOCK:
                STATS["refused_tokens"] += 1
            self._send(401, {"error": "invalid_client"})
            return
        token = secrets.token_urlsafe(32)
        with LOCK:
            TOKENS[token] = time.time() + TOKEN_TTL
            STATS["tokens"] += 1
        self._send(200, {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL})


class LLMHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _refuse(self) -> None:
        with LOCK:
            STATS["refused_calls"] += 1
        data = b'{"error": {"message": "invalid or expired token"}}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        with LOCK:
            return TOKENS.get(token, 0) > time.time()

    def _forward(self, method: str) -> None:
        if not self.path.startswith("/openai/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if not self._authorised():
            self._refuse()
            return
        path = self.path[len("/openai"):]
        if body and path.startswith("/chat/completions"):
            req = json.loads(body)
            req["model"] = upstream_model()               # "qwen3.6-27b" -> the lab model
            body = json.dumps(req).encode()
        base = UPSTREAM
        if path.startswith("/embeddings"):                # "bge-m3" -> the lab's embedding server
            base = EMBED_UPSTREAM
            with LOCK:
                STATS["embedding_calls"] += 1
        r = urllib.request.Request(base + path, data=body, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=900) as resp:
                data, code = resp.read(), resp.status
        except urllib.error.HTTPError as ex:
            data, code = ex.read(), ex.code
        if path.startswith("/models"):
            data = json.dumps({"object": "list", "data": [{"id": "qwen3.6-27b", "object": "model"}]}).encode()
        with LOCK:
            STATS["llm_calls"] += 1
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/stats":
            data = json.dumps(STATS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._forward("POST")


_MODEL: dict[str, str] = {}


def upstream_model() -> str:
    if "id" not in _MODEL:
        with urllib.request.urlopen(UPSTREAM + "/models", timeout=30) as resp:
            _MODEL["id"] = json.load(resp)["data"][0]["id"]
    return _MODEL["id"]


def serve(d: str) -> None:
    make_certs(d)
    TokenHandler.key, TokenHandler.secret = consumer(d)
    tctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tctx.load_cert_chain(os.path.join(d, "server.pem"), os.path.join(d, "server.key"))
    tctx.load_verify_locations(os.path.join(d, "ca.pem"))
    tctx.verify_mode = ssl.CERT_REQUIRED                   # the middleware wants the client certificate
    lctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    lctx.load_cert_chain(os.path.join(d, "server.pem"), os.path.join(d, "server.key"))
    token_srv = ThreadingHTTPServer(("127.0.0.1", 9460), TokenHandler)
    token_srv.socket = tctx.wrap_socket(token_srv.socket, server_side=True)
    llm_srv = ThreadingHTTPServer(("127.0.0.1", 9461), LLMHandler)
    llm_srv.socket = lctx.wrap_socket(llm_srv.socket, server_side=True)
    threading.Thread(target=token_srv.serve_forever, daemon=True).start()
    print(f"token https://127.0.0.1:9460/oauth2/token (mTLS + Basic), LLM https://127.0.0.1:9461/openai "
          f"-> {UPSTREAM}; tokens last {TOKEN_TTL} s", flush=True)
    llm_srv.serve_forever()


if __name__ == "__main__":
    serve(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/supagent-lab/mw"))
