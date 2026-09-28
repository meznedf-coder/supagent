"""What the learner adds on top of the measured facts.

* categories of the metrics from their names (cpu, memory, disk...), marked as inferred;
* descriptions written by the LLM for what nobody described (no catalog text, no HELP text
  of the exporter): marked "AI-written, unverified" wherever they are shown, never written
  over what people or the backend wrote, approved or corrected in the data dictionary page.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from superset import db

from supagent import settings
from supagent.models import KObject, Run

log = logging.getLogger(__name__)
BATCH = 10
CATEGORY_RULES: list[tuple[str, str]] = [
    (r"(^|_)(http|grpc|request|requests|response|latency|api)(_|$)", "requests"),
    (r"(^|_)cpu(_|$)|(^|_)load[0-9]*(_|$)", "cpu"),
    (r"(^|_)(memory|mem|heap|rss|oom|swap|meminfo)(_|$)", "memory"),
    (r"(^|_)(disk|filesystem|fs|io|volume|storage|inode|inodes)(_|$)", "disk"),
    (r"(^|_)(network|net|tcp|udp|socket|sockets|netstat)(_|$)", "network"),
    (r"(^|_)(job|jobs|task|tasks|queue|queued|batch|batches)(_|$)", "jobs"),
    (r"(^|_)(licence|license|licences|licenses)(_|$)", "licences"),
    (r"(^|_)(up|scrape|target|targets|exporter)(_|$)", "monitoring"),
    (r"^(process|go|jvm|python|dotnet)_|(^|_)(gc|threads?)(_|$)", "runtime"),
    (r"(^|_)(temperature|power|voltage|fan|hwmon|celsius)(_|$)", "hardware"),
    (r"(^|_)(error|errors|failure|failures|failed)(_|$)", "errors"),
]
KIND_ORDER = {"metric": 0, "index": 1, "family": 2, "field": 3, "label": 4}
PROMPT = """You write a data dictionary for business users of Apache Superset.
For each item of the JSON list, write what it measures or holds, in one or two short
sentences: plain words, the unit when there is one, what a high or low value means when it
is obvious. Use only what the name, the type, the labels, the values and the context show;
when the meaning is a guess, start with "Probably". Never invent business rules. Do not
repeat counts, ranges, dates or fill rates from the input: they are measured every day and
shown next to the description.
Also give a short category (one or two words, lower case, e.g. cpu, memory, disk, network,
requests, jobs, errors, business) for metrics and indices.
Answer with a JSON list only, one object per item:
[{"id": <id>, "description": "...", "category": "..."}]"""


def category_from_name(name: str) -> str | None:
    low = name.lower()
    for pattern, category in CATEGORY_RULES:
        if re.search(pattern, low):
            return category
    return None


def infer_categories() -> int:
    """Categories of metrics and families from their names (never over one set by people)."""
    n = 0
    for obj in db.session.query(KObject).filter(KObject.kind.in_(("metric", "family")),
                                                KObject.category.is_(None)):
        cat = category_from_name(obj.name)
        if cat:
            obj.category = cat
            n += 1
    db.session.commit()
    return n


def _few(values: Any, n: int = 10) -> list[str]:
    return [str(v)[:40] for v in (values or [])[:n]]


def _context(obj: KObject) -> dict[str, Any]:
    st = obj.stats or {}
    item: dict[str, Any] = {"id": obj.id, "kind": obj.kind, "name": obj.name}
    if obj.kind == "metric":
        item.update(type=obj.metric_type, unit=obj.unit or None)
        labels = db.session.query(KObject).filter_by(source_id=obj.source_id, kind="label", parent=obj.name).all()
        item["labels"] = {lb.name: _few((lb.stats or {}).get("values") or (lb.stats or {}).get("sample"), 6)
                          for lb in labels}
        for key in ("series", "rate_total", "min", "max", "avg", "p50", "p95"):
            if st.get(key) is not None:
                item[key] = st[key]
    elif obj.kind == "family":
        item.update(type=obj.metric_type, parts=st.get("parts"))
    elif obj.kind == "label":
        metric = db.session.query(KObject).filter_by(source_id=obj.source_id, kind="metric", name=obj.parent).first()
        item.update(metric=obj.parent, metric_description=(metric.description if metric else None),
                    values=_few(st.get("values") or st.get("sample")), cardinality=st.get("cardinality"))
    elif obj.kind == "index":
        fields = db.session.query(KObject.name).filter_by(source_id=obj.source_id, kind="field", parent=obj.name)
        item.update(documents=st.get("docs"), time_range=st.get("time_range"),
                    fields=sorted(f[0] for f in fields)[:60])
    elif obj.kind == "field":
        index = db.session.query(KObject).filter_by(source_id=obj.source_id, kind="index", name=obj.parent).first()
        item.update(index=obj.parent, index_description=(index.description if index else None), type=obj.data_type,
                    cardinality=st.get("cardinality"), values=_few(st.get("values")),
                    filled_pct=st.get("filled_pct"))
        for key in ("min", "max", "avg"):
            if st.get(key) is not None:
                item[key] = st[key]
    return {k: v for k, v in item.items() if v not in (None, [], {})}


def _parse(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def candidates(limit: int) -> list[KObject]:
    """What nobody described yet, metrics and indices first."""
    rows = (db.session.query(KObject)
            .filter(KObject.gone_at.is_(None))
            .filter((KObject.description.is_(None)) | (KObject.description == ""))
            .all())
    rows = [o for o in rows if not (o.kind == "label" and o.name in ("le", "quantile", "__tenant_id__"))]
    rows.sort(key=lambda o: (KIND_ORDER.get(o.kind, 9), o.parent or "", o.name))
    return rows[:limit]


def enrich(run: Run | None, deadline: float, llm: Any = None) -> dict[str, Any]:
    """LLM descriptions for at most learn.llm_per_run objects."""
    from supagent.llm import LLM

    todo = candidates(int(settings.get("learn.llm_per_run")))
    out: dict[str, Any] = {"asked": len(todo), "written": 0}
    if not todo:
        return out
    try:
        llm = llm or LLM()
    except Exception as ex:  # pylint: disable=broad-except
        out["error"] = str(ex)[:300]
        return out
    ids = [o.id for o in todo]
    for i in range(0, len(ids), BATCH):
        if time.time() > deadline:
            out["stopped"] = "time limit"
            break
        batch = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ids[i:i + BATCH]))}
        items = [_context(o) for o in batch.values()]
        try:
            msg = llm.chat([{"role": "system", "content": PROMPT},
                            {"role": "user", "content": json.dumps(items, ensure_ascii=False, default=str)}],
                           max_tokens=250 * len(items) + 200)
        except Exception as ex:  # pylint: disable=broad-except
            out["error"] = str(ex)[:300]
            log.warning("supagent learn: LLM descriptions stopped: %s", ex)
            break
        for entry in _parse(msg.get("content") or ""):
            try:
                obj = batch.get(int(entry.get("id")))
            except (TypeError, ValueError):
                continue
            text = str(entry.get("description") or "").strip()
            if obj is None or not text or obj.description:
                continue
            obj.description = text[:600]
            obj.description_source = "llm"
            obj.verified = False
            cat = str(entry.get("category") or "").strip().lower()[:64]
            if cat and not obj.category and obj.kind in ("metric", "index", "family"):
                obj.category = cat
            out["written"] += 1
        db.session.commit()
    return out
