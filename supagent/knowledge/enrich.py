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

from supagent.models import KObject, Run

log = logging.getLogger(__name__)
BATCH = 10                      # objects described per LLM request
CHUNK = 100                     # objects taken from the database at a time (no limit per run: the time limit)
SKIPPED_LABELS = ("le", "quantile", "__tenant_id__", "__name__")
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


def _undescribed(kind: str, source_id: int | None = None) -> Any:
    q = (db.session.query(KObject).filter(KObject.kind == kind, KObject.gone_at.is_(None))
         .filter((KObject.description.is_(None)) | (KObject.description == "")))
    if source_id is not None:
        q = q.filter(KObject.source_id == source_id)
    return q.filter(KObject.name.notin_(SKIPPED_LABELS)) if kind == "label" else q


def left_to_describe(source_id: int | None = None) -> int:
    """Objects no one described yet, of one database or of all (a label name counts once per
    database)."""
    n = sum(_undescribed(k, source_id).count() for k in ("metric", "index", "family", "field"))
    n += len({(s, nm) for s, nm in _undescribed("label", source_id).with_entities(KObject.source_id, KObject.name)
              .distinct()})
    return int(n or 0)


def _chunks(kind: str, source_id: int | None = None) -> Any:
    """Ids of the objects of this kind to describe, CHUNK at a time (read again for each chunk:
    what the run describes leaves the list). A label is described once per database and name:
    "instance" means the same on ten thousand metrics."""
    from sqlalchemy import func

    after = 0
    while True:
        if kind == "label":
            rows = (_undescribed("label", source_id).with_entities(func.min(KObject.id))
                    .group_by(KObject.source_id, KObject.name).having(func.min(KObject.id) > after)
                    .order_by(func.min(KObject.id)).limit(CHUNK).all())
        else:
            rows = _undescribed(kind, source_id).filter(KObject.id > after).with_entities(KObject.id) \
                .order_by(KObject.id).limit(CHUNK).all()
        ids = [r[0] for r in rows]
        if not ids:
            return
        yield ids
        after = ids[-1]


def _label_context(obj: KObject) -> dict[str, Any]:
    """A label for every metric that has it: its values and a few of its metrics."""
    siblings = (db.session.query(KObject.parent).filter(KObject.source_id == obj.source_id, KObject.kind == "label",
                                                        KObject.name == obj.name, KObject.gone_at.is_(None)))
    metrics = [m for (m,) in siblings.limit(6)]
    st = obj.stats or {}
    item = {"id": obj.id, "kind": "label", "name": obj.name, "on_metrics": siblings.count(),
            "metrics": metrics, "values": _few(st.get("values") or st.get("sample"))}
    return {k: v for k, v in item.items() if v not in (None, [], {})}


def _write(obj: KObject, entry: dict[str, Any]) -> int:
    """The LLM's description on this object (a label: on every metric that has it); how many."""
    text = str(entry.get("description") or "").strip()[:600]
    if not text or obj.description:
        return 0
    if obj.kind == "label":
        return (_undescribed("label").filter(KObject.source_id == obj.source_id, KObject.name == obj.name)
                .update({"description": text, "description_source": "llm", "verified": False},
                        synchronize_session=False))
    obj.description, obj.description_source, obj.verified = text, "llm", False
    cat = str(entry.get("category") or "").strip().lower()[:64]
    if cat and not obj.category and obj.kind in ("metric", "index", "family"):
        obj.category = cat
    return 1


def enrich(run: Run | None, deadline: float, llm: Any = None, source_id: int | None = None,
           pause: Any = None) -> dict[str, Any]:
    """LLM descriptions for what nobody described (metrics and indices first), of one database
    (`source_id`) or of all, CHUNK objects at a time, BATCH per request, each batch saved at once,
    until the time limit: the next run goes on where this one stopped. No limit of objects per
    run. Stops at once when an admin stops the run, and after the batch in progress when `pause`
    (a threading.Event) is set. "by_source": what was written in each database (source id)."""
    from supagent.knowledge.stopping import check
    from supagent.llm import LLM

    out: dict[str, Any] = {"written": 0, "requests": 0, "by_source": {}}
    try:
        llm = llm or LLM()
    except Exception as ex:  # pylint: disable=broad-except
        out["error"] = str(ex)[:300]
        return out
    for kind in sorted(KIND_ORDER, key=KIND_ORDER.get):
        for ids in _chunks(kind, source_id):
            for i in range(0, len(ids), BATCH):
                check()
                if pause is not None and pause.is_set():
                    out["paused"] = True
                    return out
                if time.time() > deadline:
                    out["stopped"] = "time limit: the next run goes on"
                    out["left"] = left_to_describe(source_id)
                    return out
                batch = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ids[i:i + BATCH]))}
                items = [_label_context(o) if o.kind == "label" else _context(o) for o in batch.values()]
                db.session.commit()                          # no metadata connection held during the LLM call
                try:
                    msg = llm.chat([{"role": "system", "content": PROMPT},
                                    {"role": "user", "content": json.dumps(items, ensure_ascii=False, default=str)}],
                                   max_tokens=250 * len(items) + 200)
                except Exception as ex:  # pylint: disable=broad-except
                    out["error"] = str(ex)[:300]
                    log.warning("supagent learn: LLM descriptions stopped: %s", ex)
                    out["left"] = left_to_describe(source_id)
                    return out
                out["requests"] += 1
                batch = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(list(batch)))}
                for entry in _parse(msg.get("content") or ""):
                    try:
                        obj = batch.get(int(entry.get("id")))
                    except (TypeError, ValueError):
                        continue
                    if obj is not None:
                        n = _write(obj, entry)
                        out["written"] += n
                        if n:
                            out["by_source"][obj.source_id] = out["by_source"].get(obj.source_id, 0) + n
                db.session.commit()
    out["left"] = 0
    return out
