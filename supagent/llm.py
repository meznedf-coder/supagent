"""The LLM: an OpenAI-compatible chat completions API with tool calls.

Authentication (setting llm.auth):
  none        no header (a local server)
  token       a fixed access token: Authorization: Bearer <llm.token>
  middleware  a token from the company middleware (OAuth2 client credentials): POST
              grant_type=client_credentials to llm.middleware.token_url, with the consumer
              key / secret as HTTP Basic and, when set, a client certificate (mutual TLS); the
              answer's access_token is used until expires_in minus llm.middleware.renew_before
              seconds, then a new one is asked for. An LLM answer 401 renews it once.
Only `requests` is needed (it comes with Superset).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
# a busy or restarting server (llama.cpp answers 503 "Loading model" for a minute or two): about 3 minutes
_TEMPLATE_KWARGS: dict[str, bool] = {}     # base URL -> accepts chat_template_kwargs
RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0)


class LLMError(Exception):
    pass


@dataclass
class LLMConfig:
    base_url: str
    model: str = ""
    auth: str = "none"
    token: str = ""
    token_url: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""
    cert_path: str = ""
    key_path: str = ""
    scope: str = ""
    renew_before: int = 60
    ca_bundle: str = ""
    verify_tls: bool = True
    timeout: int = 900
    temperature: float = 0.2
    thinking: bool = False
    extra_headers: dict | None = None

    @classmethod
    def from_settings(cls) -> "LLMConfig":
        from supagent import settings as st

        return cls(base_url=st.get("llm.base_url"), model=st.get("llm.model"), auth=st.get("llm.auth"),
                   token=st.get("llm.token"), token_url=st.get("llm.middleware.token_url"),
                   consumer_key=st.get("llm.middleware.consumer_key"),
                   consumer_secret=st.get("llm.middleware.consumer_secret"),
                   cert_path=st.get("llm.middleware.cert_path"), key_path=st.get("llm.middleware.key_path"),
                   scope=st.get("llm.middleware.scope"), renew_before=st.get("llm.middleware.renew_before"),
                   ca_bundle=st.get("llm.ca_bundle"), verify_tls=st.get("llm.verify_tls"),
                   timeout=st.get("llm.timeout"), temperature=st.get("llm.temperature"),
                   thinking=st.get("llm.thinking"), extra_headers=st.get("llm.extra_headers") or {})

    def verify(self) -> Any:
        if not self.verify_tls:
            return False
        return self.ca_bundle or True


# --------------------------------------------------------------------------- #
# middleware tokens: one per configuration, shared by the threads of a process
# --------------------------------------------------------------------------- #
_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def _token_key(cfg: LLMConfig) -> str:
    raw = "\0".join([cfg.token_url, cfg.consumer_key, cfg.consumer_secret, cfg.cert_path, cfg.key_path, cfg.scope])
    return hashlib.sha256(raw.encode()).hexdigest()


def middleware_token(cfg: LLMConfig, force: bool = False) -> str:
    """A valid access token from the middleware (cached until it is about to expire)."""
    key = _token_key(cfg)
    with _TOKEN_LOCK:
        cached = _TOKENS.get(key)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        if not cfg.token_url:
            raise LLMError("llm.auth is middleware but llm.middleware.token_url is empty")
        data = {"grant_type": "client_credentials"}
        if cfg.scope:
            data["scope"] = cfg.scope
        cert = (cfg.cert_path, cfg.key_path) if cfg.cert_path else None
        try:
            r = requests.post(cfg.token_url, data=data, auth=(cfg.consumer_key, cfg.consumer_secret), cert=cert,
                              verify=cfg.verify(), timeout=60,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        except requests.exceptions.SSLError as ex:
            raise LLMError(f"middleware {cfg.token_url}: TLS refused ({ex}); check llm.middleware.cert_path / "
                           "key_path (client certificate) and llm.ca_bundle") from ex
        except requests.exceptions.RequestException as ex:
            raise LLMError(f"middleware {cfg.token_url} not reachable: {ex}") from ex
        if r.status_code != 200:
            raise LLMError(f"middleware {cfg.token_url} refused the token request (HTTP {r.status_code}): "
                           f"{r.text[:300]}; check the consumer key / secret and the certificate")
        try:
            body = r.json()
        except ValueError as ex:
            raise LLMError(f"middleware answer is not JSON: {r.text[:200]}") from ex
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise LLMError("middleware answer without access_token")
        expires_in = _seconds(body.get("expires_in"))
        margin = min(float(cfg.renew_before), expires_in / 2)
        _TOKENS[key] = (token, time.time() + expires_in - margin)
        return token


DEFAULT_TOKEN_SECONDS = 300.0


def _seconds(value: Any) -> float:
    """expires_in as a number or a numeric string ("3599", as some gateways send it); missing or
    unreadable: DEFAULT_TOKEN_SECONDS (a 401 renews the token anyway)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TOKEN_SECONDS
    return seconds if seconds > 0 else DEFAULT_TOKEN_SECONDS


def auth_headers(cfg: LLMConfig, force: bool = False) -> dict[str, str]:
    if cfg.auth == "token":
        if not cfg.token:
            raise LLMError("llm.auth is token but llm.token is empty")
        return {"Authorization": f"Bearer {cfg.token}"}
    if cfg.auth == "middleware":
        return {"Authorization": f"Bearer {middleware_token(cfg, force=force)}"}
    return {}


class LLM:
    def __init__(self, cfg: LLMConfig | None = None) -> None:
        self.cfg = cfg or LLMConfig.from_settings()
        if not self.cfg.base_url:
            raise LLMError("the LLM is not configured: set llm.base_url (AI agent settings)")
        self.base = self.cfg.base_url.rstrip("/")
        self.session = requests.Session()
        self._model = self.cfg.model

    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        renewed = False
        for attempt in range(len(RETRY_DELAYS) + 1):
            headers = {"Content-Type": "application/json", **(self.cfg.extra_headers or {}),
                       **auth_headers(self.cfg, force=renewed)}
            try:
                r = self.session.request(method, self.base + path, headers=headers, verify=self.cfg.verify(),
                                         timeout=self.cfg.timeout, **kw)
            except requests.exceptions.SSLError as ex:
                raise LLMError(f"LLM {self.base}: TLS refused ({ex}); check llm.ca_bundle") from ex
            except requests.exceptions.RequestException as ex:
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise LLMError(f"LLM {self.base} not reachable: {ex}") from ex
            if r.status_code == 401 and self.cfg.auth == "middleware" and not renewed:
                renewed = True                   # the token expired early or was revoked: once more
                continue
            if r.status_code in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            if r.status_code in (401, 403):
                raise LLMError(f"LLM {self.base} refused the call (HTTP {r.status_code}): {r.text[:200]}; "
                               "check llm.auth and its token / middleware settings")
            if r.status_code >= 400:
                raise LLMError(f"LLM {self.base}{path}: HTTP {r.status_code}: {r.text[:300]}")
            return r
        raise LLMError(f"LLM {self.base}{path}: no answer")

    @property
    def model(self) -> str:
        if not self._model:
            data = self._request("GET", "/models").json().get("data") or []
            if not data:
                raise LLMError("the LLM server lists no model: set llm.model")
            self._model = data[0]["id"]
        return self._model

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> dict:
        """One chat completion; returns the assistant message (content and/or tool_calls)."""
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.cfg.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if max_tokens:
            body["max_tokens"] = max_tokens
        if not self.cfg.thinking and _TEMPLATE_KWARGS.get(self.base, True):
            body["chat_template_kwargs"] = {"enable_thinking": False}   # Qwen3 and the like: no reasoning
        try:
            r = self._request("POST", "/chat/completions", data=json.dumps(body))
        except LLMError as ex:
            if "chat_template_kwargs" not in body or not re.search(r"HTTP (400|422)", str(ex)):
                raise
            _TEMPLATE_KWARGS[self.base] = False          # a gateway that refuses unknown fields
            body.pop("chat_template_kwargs")
            r = self._request("POST", "/chat/completions", data=json.dumps(body))
        try:
            msg = r.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as ex:
            raise LLMError(f"unexpected LLM answer: {r.text[:300]}") from ex
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
            raise LLMError("the LLM returned an empty answer")
        return msg

    def check(self) -> dict:
        """What `superset supagent test-llm` and the admin page's test button show."""
        t0 = time.time()
        out: dict[str, Any] = {"base_url": self.base, "auth": self.cfg.auth}
        if self.cfg.auth == "middleware":
            token = middleware_token(self.cfg)
            out["token"] = f"{token[:6]}... ({len(token)} characters)"
        out["model"] = self.model
        msg = self.chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=16)
        out["answer"] = (msg.get("content") or "").strip()[:80]
        out["seconds"] = round(time.time() - t0, 2)
        return out
