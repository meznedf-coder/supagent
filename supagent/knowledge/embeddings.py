"""Vectors of the knowledge: an OpenAI-compatible /embeddings API (e.g. bge-m3 on the company
gateway, with the LLM's authentication: fixed token or middleware), stored as float16 in
Superset's database (no extension) and compared in memory with numpy, or kept in a Qdrant
server (search.vector_store = qdrant, through its REST API: no extra package)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import numpy as np
import requests

from supagent import settings

log = logging.getLogger(__name__)


class EmbedError(Exception):
    pass


def model() -> str:
    return (settings.get("embed.model") or "").strip()


def enabled() -> bool:
    return bool(model())


def embed(texts: list[str]) -> list[np.ndarray]:
    """One vector per text (normalised)."""
    from supagent.llm import LLMConfig, auth_headers

    cfg = LLMConfig.from_settings()
    base = (settings.get("embed.base_url") or cfg.base_url or "").rstrip("/")
    if not base:
        raise EmbedError("no embedding API: set embed.base_url or llm.base_url")
    out: list[np.ndarray] = []
    batch = max(1, int(settings.get("embed.batch")))
    for i in range(0, len(texts), batch):
        part = [t[:6000] for t in texts[i:i + batch]]
        for attempt in range(2):
            headers = {"Content-Type": "application/json", **(cfg.extra_headers or {}),
                       **auth_headers(cfg, force=attempt > 0)}
            try:
                r = requests.post(base + "/embeddings", data=json.dumps({"model": model(), "input": part}),
                                  headers=headers, verify=cfg.verify(), timeout=min(cfg.timeout, 300))
            except requests.exceptions.RequestException as ex:
                raise EmbedError(f"embeddings {base}: {ex}") from ex
            if r.status_code == 401 and cfg.auth == "middleware" and attempt == 0:
                continue
            if r.status_code >= 400:
                raise EmbedError(f"embeddings {base}: HTTP {r.status_code}: {r.text[:300]}")
            break
        data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(part):
            raise EmbedError(f"embeddings: {len(data)} vectors for {len(part)} texts")
        for d in data:
            v = np.asarray(d["embedding"], dtype=np.float32)
            n = float(np.linalg.norm(v)) or 1.0
            out.append(v / n)
    return out


def to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float16).tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


# --------------------------------------------------------------------------- #
# in-memory matrix of the database's vectors (per process), reloaded when they change
# --------------------------------------------------------------------------- #
_MATRIX: dict[str, Any] = {"key": None, "ids": None, "matrix": None}
_LOCK = threading.Lock()


def _matrix() -> tuple[np.ndarray | None, np.ndarray | None]:
    from sqlalchemy import func
    from superset import db

    from supagent.models import Chunk

    m = model()
    key = db.session.query(func.count(Chunk.id), func.max(Chunk.updated_at)).filter(
        Chunk.embed_model == m, Chunk.vector.isnot(None)).one()
    key = (m, key[0], str(key[1]))
    with _LOCK:
        if _MATRIX["key"] == key:
            return _MATRIX["ids"], _MATRIX["matrix"]
        rows = db.session.query(Chunk.id, Chunk.vector).filter(Chunk.embed_model == m, Chunk.vector.isnot(None)).all()
        if not rows:
            _MATRIX.update(key=key, ids=None, matrix=None)
            return None, None
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        matrix = np.vstack([np.frombuffer(r[1], dtype=np.float16) for r in rows]).astype(np.float32)
        _MATRIX.update(key=key, ids=ids, matrix=matrix)
        return ids, matrix


def nearest(query_vec: np.ndarray, allowed_ids: set[int] | None, k: int = 50) -> list[tuple[int, float]]:
    """(chunk id, cosine) of the closest vectors among the allowed chunks."""
    if settings.get("search.vector_store") == "qdrant":
        return qdrant_search(query_vec, allowed_ids, k)
    ids, matrix = _matrix()
    if ids is None or matrix is None or matrix.shape[1] != query_vec.shape[0]:
        return []
    scores = matrix @ query_vec
    if allowed_ids is not None:
        mask = np.isin(ids, np.fromiter(allowed_ids, dtype=np.int64, count=len(allowed_ids)))
        scores = np.where(mask, scores, -np.inf)
    top = np.argsort(-scores)[:k]
    return [(int(ids[i]), float(scores[i])) for i in top if np.isfinite(scores[i])]


# --------------------------------------------------------------------------- #
# Qdrant (optional): REST API, one point per chunk (id = chunk id), permissions filtered by id
# --------------------------------------------------------------------------- #
def _qdrant() -> tuple[str, dict[str, str], str]:
    url = (settings.get("qdrant.url") or "").rstrip("/")
    if not url:
        raise EmbedError("search.vector_store is qdrant but qdrant.url is empty")
    headers = {"Content-Type": "application/json"}
    key = settings.get("qdrant.api_key")
    if key:
        headers["api-key"] = key
    return url, headers, settings.get("qdrant.collection") or "supagent"


def qdrant_upsert(points: list[tuple[int, np.ndarray]]) -> None:
    if not points:
        return
    url, headers, coll = _qdrant()
    dim = int(points[0][1].shape[0])
    r = requests.get(f"{url}/collections/{coll}", headers=headers, timeout=30)
    if r.status_code == 404:
        r = requests.put(f"{url}/collections/{coll}", headers=headers, timeout=30,
                         data=json.dumps({"vectors": {"size": dim, "distance": "Cosine"}}))
        if r.status_code >= 400:
            raise EmbedError(f"qdrant: cannot create {coll}: {r.text[:200]}")
    body = {"points": [{"id": cid, "vector": [float(x) for x in v]} for cid, v in points]}
    r = requests.put(f"{url}/collections/{coll}/points?wait=true", headers=headers, data=json.dumps(body), timeout=120)
    if r.status_code >= 400:
        raise EmbedError(f"qdrant upsert: HTTP {r.status_code}: {r.text[:200]}")


def qdrant_delete(ids: list[int]) -> None:
    if not ids or settings.get("search.vector_store") != "qdrant":
        return
    url, headers, coll = _qdrant()
    requests.post(f"{url}/collections/{coll}/points/delete?wait=true", headers=headers,
                  data=json.dumps({"points": ids}), timeout=60)


def qdrant_search(query_vec: np.ndarray, allowed_ids: set[int] | None, k: int) -> list[tuple[int, float]]:
    url, headers, coll = _qdrant()
    body: dict[str, Any] = {"vector": [float(x) for x in query_vec], "limit": k, "with_payload": False}
    if allowed_ids is not None:
        body["filter"] = {"must": [{"has_id": sorted(allowed_ids)}]}
    r = requests.post(f"{url}/collections/{coll}/points/search", headers=headers, data=json.dumps(body), timeout=30)
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        raise EmbedError(f"qdrant search: HTTP {r.status_code}: {r.text[:200]}")
    return [(int(p["id"]), float(p["score"])) for p in r.json().get("result") or []]
