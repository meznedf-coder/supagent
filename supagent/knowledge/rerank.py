"""A cross-encoder reranker for the knowledge search (0.6). The first pieces the search found (words, meaning,
near spellings: rerank.depth of them) are scored against the question by a reranker model served over HTTP, each
read whole with the question (a cross-encoder: what the search's vectors and words cannot do), then ordered by both
ranks, the search's and the reranker's (rank fusion: a reranker's scores do not compare from one question to the
next). Off until rerank.url is set. Any failure, or no reply within rerank.timeout: the search's own order, and no
call for a minute (a reranker down never slows the answers).

The APIs (rerank.api):
  jina  POST url {"model", "query", "documents": [...], "top_n"} -> {"results": [{"index", "relevance_score"}]}
        (Jina, Cohere, vLLM, llama.cpp's server with --reranking, Infinity, LocalAI)
  tei   POST url {"query", "texts": [...], "truncate": true} -> [{"index", "score"}]
        (Hugging Face text-embeddings-inference)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

import requests

from supagent import settings

log = logging.getLogger(__name__)

PAUSE_S = 60.0             # after a failure: no call for this long
CACHE_S = 120.0            # the same question and pieces scored again within this: the scores kept (the prompt's
CACHE_N = 256              # search and the decider's search of the same question)
RRF = 60

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"pause_until": 0.0, "last_error": None, "calls": 0, "failures": 0, "seconds": 0.0}
_CACHE: OrderedDict[tuple, tuple[float, list[float]]] = OrderedDict()


def enabled() -> bool:
    return bool((settings.get("rerank.url") or "").strip())


def status() -> dict[str, Any]:
    with _LOCK:
        paused = max(0.0, _STATE["pause_until"] - time.time())
        return {"enabled": enabled(), "url": settings.get("rerank.url") or None, "calls": _STATE["calls"],
                "failures": _STATE["failures"], "last_error": _STATE["last_error"],
                "avg_seconds": round(_STATE["seconds"] / _STATE["calls"], 3) if _STATE["calls"] else None,
                "paused_for_s": round(paused, 1)}


def text_of(piece: dict[str, Any]) -> str:
    """What the reranker reads of a piece: its title with its names cut into words (node_cpu_seconds_total:
    node cpu seconds total), then its text, at most rerank.max_chars."""
    from supagent.knowledge.pgstore import _split_names

    title = str(piece.get("title") or "")
    split = _split_names(title)
    head = title if split.lower() == title.lower() else f"{title} ({split})"
    return f"{head}\n{piece.get('text') or ''}"[:max(200, int(settings.get("rerank.max_chars") or 1500))]


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    how = settings.get("rerank.auth") or "none"
    if how == "token" and settings.get("rerank.token"):
        h["Authorization"] = f"Bearer {settings.get('rerank.token')}"
    elif how == "llm":                            # on the LLM's gateway: its token or middleware
        from supagent.llm import LLMConfig, auth_headers

        cfg = LLMConfig.from_settings()
        h.update({**(cfg.extra_headers or {}), **auth_headers(cfg)})
    return h


def scores(query: str, texts: list[str]) -> list[float] | None:
    """The reranker's score of each text for the query (higher: more relevant), in the texts' order; None when
    it cannot say (off, paused after a failure, an error, too slow)."""
    if not enabled() or not texts:
        return None
    key = (query, tuple(texts))
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < CACHE_S:
            _CACHE.move_to_end(key)
            return list(hit[1])
        if now < _STATE["pause_until"]:
            return None
    url = settings.get("rerank.url").strip()
    api = settings.get("rerank.api") or "jina"
    if api == "tei":
        body: dict[str, Any] = {"query": query, "texts": texts, "truncate": True}
    else:
        body = {"query": query, "documents": texts, "top_n": len(texts)}
        if settings.get("rerank.model"):
            body["model"] = settings.get("rerank.model")
    t0 = time.time()
    try:
        from supagent.llm import LLMConfig

        r = requests.post(url, data=json.dumps(body), headers=_headers(), verify=LLMConfig.from_settings().verify(),
                          timeout=float(settings.get("rerank.timeout") or 2.0))
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
        out = [float("-inf")] * len(texts)
        for x in rows:
            i = int(x.get("index"))
            if 0 <= i < len(texts):
                out[i] = float(x.get("relevance_score", x.get("score")))
        if any(v == float("-inf") for v in out):
            raise ValueError(f"{sum(v == float('-inf') for v in out)} of {len(texts)} texts without a score")
    except Exception as ex:  # pylint: disable=broad-except   (fail open: the search's own order)
        with _LOCK:
            _STATE["failures"] += 1
            _STATE["last_error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
            _STATE["pause_until"] = time.time() + PAUSE_S
        log.warning("supagent rerank: %s (the search's order for %d s)", str(ex)[:200], PAUSE_S)
        return None
    with _LOCK:
        _STATE["calls"] += 1
        _STATE["seconds"] += time.time() - t0
        _CACHE[key] = (time.time(), out)
        while len(_CACHE) > CACHE_N:
            _CACHE.popitem(last=False)
    return out


def rerank(query: str, pieces: list[dict[str, Any]], k: int | None = None) -> list[dict[str, Any]]:
    """The pieces in a better order (the first rerank.depth by rank fusion of the search's order and the
    reranker's, the others after them as they were), each reranked one with "rerank" (its score) and "score"
    (the fused one, in the scale of the search's); unchanged when the reranker cannot say."""
    depth = max(2, int(settings.get("rerank.depth") or 40))
    head, tail = pieces[:depth], pieces[depth:]
    if len(head) < 2:
        return pieces[:k] if k else pieces
    got = scores(query, [text_of(p) for p in head])
    if got is None:
        return pieces[:k] if k else pieces
    w = settings.get("rerank.weight")
    weight = 1.0 if w is None or w == "" else float(w)          # 0: the reranker's order not counted
    by_rerank = sorted(range(len(head)), key=lambda i: -got[i])
    place = {i: r for r, i in enumerate(by_rerank)}
    fused = sorted(range(len(head)), key=lambda i: (-(1.0 / (RRF + i) + weight / (RRF + place[i])), i))
    kept = sorted((p.get("score") or 0.0 for p in head), reverse=True)   # the search's scores, best first
    out = []
    for j, i in enumerate(fused):
        q = dict(head[i])
        q["rerank"] = round(got[i], 4)
        q["search_score"] = head[i].get("score")
        q["score"] = kept[j]                        # the fused order, with the search's scale (the decider's)
        out.append(q)
    out += tail
    return out[:k] if k else out
