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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
# a busy or restarting server (llama.cpp answers 503 "Loading model" for a minute or two): about 3 minutes
_TEMPLATE_KWARGS: dict[str, bool] = {}     # base URL -> accepts chat_template_kwargs
RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0)


class LLMError(Exception):
    pass


class EmptyAnswer(LLMError):
    """The LLM answered nothing (no text, no tool call), even when asked again."""


EMPTY_RETRIES = 2                # an empty answer is asked again this many times (the same request)
_BACKGROUND = threading.local()


@contextmanager
def background() -> Iterator[None]:
    """LLM calls made inside wait while answers are being computed (daily learning, learned
    answers, memory from a Helpful): the people waiting for an answer are served first."""
    before = getattr(_BACKGROUND, "on", False)
    _BACKGROUND.on = True
    try:
        yield
    finally:
        _BACKGROUND.on = before


def _usage(body: dict, seconds: float) -> dict[str, Any]:
    """Seconds and tokens of one call (OpenAI usage; llama.cpp timings: prompt tokens processed,
    the rest came from its prompt cache; vLLM / OpenAI: prompt_tokens_details.cached_tokens)."""
    usage = body.get("usage") or {}
    timings = body.get("timings") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None and timings.get("prompt_n") is not None and prompt:
        cached = max(0, prompt - int(timings["prompt_n"]))
    return {"calls": 1, "seconds": round(seconds, 2), "prompt_tokens": prompt,
            "completion_tokens": int(usage.get("completion_tokens") or 0), "cached_tokens": int(cached or 0)}


def add_usage(total: dict[str, Any], one: dict[str, Any] | None) -> dict[str, Any]:
    for k, v in (one or {}).items():
        total[k] = round(total.get(k, 0) + v, 2) if isinstance(v, float) else total.get(k, 0) + v
    return total


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
        self.last_usage: dict[str, Any] | None = None     # seconds and tokens of the last chat()

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
        """One chat completion; returns the assistant message (content and/or tool_calls). An empty
        answer (seen with local models, often right after a big tool result) is asked again
        EMPTY_RETRIES times before EmptyAnswer. Inside background(), waits first while answers
        are being computed."""
        if getattr(_BACKGROUND, "on", False):
            try:
                from supagent.priority import wait_for_answers

                wait_for_answers()
            except Exception:  # pylint: disable=broad-except   (never blocks the work)
                pass
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.cfg.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if max_tokens:
            body["max_tokens"] = max_tokens
        if not self.cfg.thinking and _TEMPLATE_KWARGS.get(self.base, True):
            body["chat_template_kwargs"] = {"enable_thinking": False}   # Qwen3 and the like: no reasoning
        self.last_usage = None
        for _attempt in range(EMPTY_RETRIES + 1):
            t0 = time.time()
            try:
                r = self._request("POST", "/chat/completions", data=json.dumps(body))
            except LLMError as ex:
                if "chat_template_kwargs" not in body or not re.search(r"HTTP (400|422)", str(ex)):
                    raise
                _TEMPLATE_KWARGS[self.base] = False          # a gateway that refuses unknown fields
                body.pop("chat_template_kwargs")
                r = self._request("POST", "/chat/completions", data=json.dumps(body))
            try:
                data = r.json()
                msg = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as ex:
                raise LLMError(f"unexpected LLM answer: {r.text[:300]}") from ex
            self.last_usage = add_usage(self.last_usage or {}, _usage(data, time.time() - t0))
            text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
            if text.strip() or msg.get("tool_calls"):
                return msg
        raise EmptyAnswer("the LLM returned an empty answer")

    def _raw(self, body: dict) -> tuple[dict, float]:
        body = {"model": self.model, "temperature": 0, **body}
        t0 = time.time()
        r = self._request("POST", "/chat/completions", data=json.dumps(body))
        return r.json(), round(time.time() - t0, 2)

    def profile(self) -> dict:
        """What this LLM server is like for the agent (superset supagent test-llm --profile): an
        answer without and with thinking, a tool call, and whether its prompt cache keeps a shared
        beginning of the prompt (the agent's instructions and tools are the same from one question
        to the next: the cache saves their processing on every question)."""
        out: dict[str, Any] = {"model": self.model}
        ask = [{"role": "user", "content": "Reply with the single word OK."}]
        no_think = {"chat_template_kwargs": {"enable_thinking": False}} if _TEMPLATE_KWARGS.get(self.base, True) else {}
        data, sec = self._raw({"messages": ask, "max_tokens": 16, **no_think})
        out["answer"] = {"seconds": sec, "text": ((data.get("choices") or [{}])[0].get("message") or {}).get(
            "content", "")[:40]}
        try:
            data, sec = self._raw({"messages": ask, "max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": True}})
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            out["thinking"] = {"seconds": sec, "completion_tokens": (data.get("usage") or {}).get("completion_tokens"),
                               "answered": bool(re.sub(r"<think>.*?</think>", "", msg.get("content") or "",
                                                       flags=re.S).strip())}
        except LLMError as ex:
            out["thinking"] = {"error": str(ex)[:200]}
        tool = {"type": "function", "function": {"name": "get_time", "description": "The current time",
                                                  "parameters": {"type": "object", "properties": {}}}}
        try:
            data, sec = self._raw({"messages": [{"role": "user", "content": "What time is it? Use the tool."}],
                                   "tools": [tool], "tool_choice": "auto", "max_tokens": 200, **no_think})
            calls = ((data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
            out["tool_calls"] = {"seconds": sec, "works": any((c.get("function") or {}).get("name") == "get_time"
                                                              for c in calls)}
        except LLMError as ex:
            out["tool_calls"] = {"error": str(ex)[:200]}
        shared = "\n".join(f"Rule {i}: the answer to question {i} is found in table t{i}, column c{i}, "
                            f"filtered on the day and grouped by application." for i in range(150))
        cache = []
        for q in ("Which table answers question 7?", "Which column answers question 12?"):
            data, sec = self._raw({"messages": [{"role": "system", "content": shared}, {"role": "user", "content": q}],
                                   "max_tokens": 1, **no_think})
            u = _usage(data, sec)
            cache.append({"seconds": sec, "prompt_tokens": u["prompt_tokens"], "cached_tokens": u["cached_tokens"]})
        out["prompt_cache"] = {"first": cache[0], "second": cache[1],
                               "works": cache[1]["cached_tokens"] >= 0.5 * max(1, cache[1]["prompt_tokens"])}
        return out

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
