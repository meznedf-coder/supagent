"""The cross-encoder reranker of the knowledge search: off until a URL is set; the first pieces ordered by both
ranks (the search's and the reranker's), with the search's scale of scores (the decider reads it); both APIs (Jina /
Cohere style and text-embeddings-inference); a failure or a slow reply: the search's own order and a pause; the same
question scored once; the router never waits for it."""

from __future__ import annotations

import json

import pytest

PIECES = [{"ref": f"object:{i}", "kind": "metric", "title": name, "text": text, "score": score}
          for i, (name, text, score) in enumerate([
              ("node_cpu_seconds_total", "Seconds of CPU per mode", 0.05),
              ("node_memory_MemAvailable_bytes", "Memory available on the server", 0.04),
              ("http_requests_total", "HTTP requests by status", 0.035),
              ("jvm_heap_used_bytes", "Heap used by the JVM of each application", 0.03)])]


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self.data


@pytest.fixture()
def rr(ctx, monkeypatch):
    from supagent import settings
    from supagent.knowledge import rerank as R, search as S

    conf = {"rerank.url": "http://reranker:8092/v1/rerank", "rerank.api": "jina", "rerank.depth": 40,
            "rerank.weight": 1.0, "rerank.timeout": 2.0, "rerank.auth": "none"}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key: conf[key] if key in conf else real(key))
    monkeypatch.setattr(S, "_search", lambda q, k, kinds, lower, skip: [dict(p) for p in PIECES][:k])
    calls = []

    def post(url, data=None, headers=None, verify=None, timeout=None):
        body = json.loads(data)
        calls.append({"url": url, "body": body, "timeout": timeout})
        texts = body.get("documents") or body.get("texts")
        score = [5.0 if "Memory available" in t else 1.0 if "Heap" in t else 0.0 for t in texts]  # memory first
        if conf["rerank.api"] == "tei":
            return Reply([{"index": i, "score": s} for i, s in sorted(enumerate(score), key=lambda x: -x[1])])
        return Reply({"results": [{"index": i, "relevance_score": s} for i, s in enumerate(score)]})

    monkeypatch.setattr(R.requests, "post", post)
    R._CACHE.clear()
    R._STATE.update(pause_until=0.0, last_error=None, calls=0, failures=0, seconds=0.0)
    yield {"conf": conf, "calls": calls, "R": R, "S": S}


def test_off_until_a_url_is_set(rr):
    rr["conf"]["rerank.url"] = ""
    got = rr["S"].search("how much memory is left on the servers", k=3)
    assert [p["ref"] for p in got] == ["object:0", "object:1", "object:2"] and not rr["calls"]


def test_the_first_pieces_ordered_by_both_ranks_in_the_search_scale(rr):
    got = rr["S"].search("how much memory is left on the servers", k=4)
    assert got[0]["ref"] == "object:1" and got[0]["rerank"] == 5.0            # the reranker's first comes up
    assert sorted(p["score"] for p in got) == sorted(p["score"] for p in PIECES)  # the same scale of scores
    assert [p["score"] for p in got] == sorted((p["score"] for p in got), reverse=True)
    body = rr["calls"][0]["body"]
    assert body["query"].startswith("how much memory") and len(body["documents"]) == 4
    assert "node cpu seconds total" in body["documents"][0]                  # names read as words
    rr["conf"]["rerank.weight"] = 0.0                                        # the reranker's order not counted
    rr["R"]._CACHE.clear()
    assert rr["S"].search("how much memory is left on the servers", k=4)[0]["ref"] == "object:0"


def test_the_tei_api_too(rr):
    rr["conf"]["rerank.api"] = "tei"
    got = rr["S"].search("memory left", k=2)
    assert got[0]["ref"] == "object:1" and "texts" in rr["calls"][0]["body"]


def test_a_failure_keeps_the_search_order_and_pauses(rr, monkeypatch):
    R = rr["R"]
    monkeypatch.setattr(R.requests, "post", lambda *a, **kw: Reply({"error": "overloaded"}, 503))
    got = rr["S"].search("how much memory is left", k=4)
    assert [p["ref"] for p in got] == [p["ref"] for p in PIECES] and "rerank" not in got[0]
    st = R.status()
    assert st["failures"] == 1 and "503" in st["last_error"] and st["paused_for_s"] > 50
    called = []
    monkeypatch.setattr(R.requests, "post", lambda *a, **kw: called.append(1))
    rr["S"].search("how much memory is left", k=4)
    assert not called                                                        # paused: no call


def test_the_same_question_scored_once_and_the_router_never_waits(rr, monkeypatch):
    rr["S"].search("memory left on the servers", k=4)
    rr["S"].search("memory left on the servers", k=3)                      # the prompt's search, the decider's
    assert len(rr["calls"]) == 1
    assert rr["calls"][0]["timeout"] == 2.0
    from supagent import router

    monkeypatch.setattr(rr["R"], "scores", lambda *a, **kw: pytest.fail("the router called the reranker"))
    router._found("which server is out of memory")
