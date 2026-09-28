"""Where the data of a question is, found before the LLM starts (no LLM call, a few milliseconds
once the lists are cached): the metrics, indices and fields whose names (split on _ : . - and
camelCase), HELP texts, descriptions and synonyms share words with the question, the words
earlier answers used them for (associations), and a few built-in synonyms (cpu / processor,
mem / memory, es / elasticsearch...). The agent gets the best ones with their database id, type,
unit, labels and a SQL to adapt, so that it does not have to search.

It works without the data dictionary (the live list of metric names of each metrics database,
cached five minutes), and only on the databases the agent may use (agent.databases) and the user
may query (Superset's database access)."""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import Any

from superset import db

from supagent.knowledge.describe import STOP, stem

log = logging.getLogger(__name__)
TTL = 300.0                       # seconds the lists of names are kept
DETAILED = 4                      # candidates given with their details and a SQL
NAMED = 6                         # more candidates, by name only
LIVE_LOOKUPS = 4                  # label lookups for metrics the dictionary does not know yet
BUDGET_S = 3.0

SYNONYMS: dict[str, set[str]] = {
    "cpu": {"cpu", "processor", "proc"}, "processor": {"cpu", "processor"}, "processeur": {"cpu", "processor"},
    "load": {"load"}, "charge": {"load"},
    "memory": {"memory", "mem", "heap", "ram", "rss"}, "mem": {"memory", "mem"}, "ram": {"memory", "mem", "ram"},
    "memoire": {"memory", "mem"}, "heap": {"heap", "memory"},
    "disk": {"disk", "fs", "filesystem", "storage", "volume"}, "disque": {"disk", "fs", "filesystem"},
    "storage": {"storage", "disk", "fs"}, "filesystem": {"filesystem", "fs", "disk"},
    "network": {"network", "net", "tcp", "udp"}, "reseau": {"network", "net"},
    "elasticsearch": {"elasticsearch", "es", "opensearch"}, "opensearch": {"opensearch", "elasticsearch", "es"},
    "es": {"elasticsearch", "es"},
    "error": {"error", "err", "failed", "failure", "fail"}, "erreur": {"error", "failed", "failure"},
    "fail": {"failed", "failure", "fail", "error"}, "failed": {"failed", "failure", "fail", "error"},
    "failure": {"failed", "failure", "fail"}, "echec": {"failed", "failure", "fail"}, "echoue": {"failed", "fail"},
    "request": {"request", "req"}, "requete": {"request", "req"}, "http": {"http"},
    "latency": {"latency", "duration", "second"}, "duration": {"duration", "second", "latency"},
    "duree": {"duration", "second"}, "time": {"time", "duration", "second"},
    "queue": {"queue", "pending", "backlog", "waiting"}, "file": {"queue", "file"},
    "jvm": {"jvm", "java", "heap"}, "java": {"jvm", "java"}, "gc": {"gc", "garbage"},
    "thread": {"thread"}, "temperature": {"temperature", "temp"},
    "license": {"license", "licence", "token"}, "licence": {"license", "licence", "token"},
    "job": {"job", "task", "batch"}, "task": {"task", "job"}, "tache": {"task", "job"},
    "server": {"node", "server", "host", "instance"}, "serveur": {"node", "server", "host", "instance"},
    "node": {"node", "server", "host", "instance"}, "host": {"host", "node", "server", "instance"},
}

# words that say what to do with the data, not which data: never matched on names
GENERIC = set("""over during since until per rate rates count number total totals average avg sum max min maximum
minimum value values chart graph plot table need see want show give get last past next hour minute day week
month year today yesterday now please can could would top highest lowest most least trend evolution compare list
what which much many time times current currently moyenne nombre taux graphique courbe tableau dernier derniere
heure jour semaine mois annee aujourd hier maintenant plus moin evolution liste""".split())

_LOCK = threading.Lock()
_CACHE: dict[Any, tuple[float, Any]] = {}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def terms(text: str) -> list[str]:
    """The words of a question that can name data (stop words and one-letter words dropped)."""
    out = []
    for w in re.findall(r"[a-z0-9]+", norm(text)):
        if w in STOP or len(w) < 2 or w.isdigit():
            continue
        w = stem(w)
        if w not in GENERIC:
            out.append(w)
    return list(dict.fromkeys(out))[:16]


def name_tokens(name: str) -> list[str]:
    parts = re.split(r"[_:./\-\s]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name or ""))
    return [stem(p) for p in (norm(p) for p in parts) if p]


def _cached(key: Any, make: Any) -> Any:
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    value = make()
    with _LOCK:
        _CACHE[key] = (now, value)
    return value


def _metric_names(database: Any) -> list[tuple[str, list[str]]]:
    """(name, its tokens) of every metric of a metrics database, from the live list."""
    from supagent.tools import _promagg_connection

    def make() -> list[tuple[str, list[str]]]:
        conn = _promagg_connection(database)
        try:
            return [(n, name_tokens(n)) for n in conn.list_tables()]
        finally:
            conn.close()

    return _cached(("metrics", database.id), make)


def _dictionary(source_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    """The dictionary's metrics, indices and fields of these sources, with their words."""
    from supagent.models import KObject

    def make() -> list[dict[str, Any]]:
        rows = (db.session.query(KObject).filter(KObject.source_id.in_(source_ids),
                                                 KObject.kind.in_(("metric", "index", "field")),
                                                 KObject.gone_at.is_(None)).all())
        out = []
        for o in rows:
            text = " ".join(str(x) for x in (o.description, o.backend_help, o.category,
                                              " ".join(map(str, o.synonyms or []))) if x)
            st = o.stats or {}
            out.append({"source_id": o.source_id, "kind": o.kind, "parent": o.parent or "", "name": o.name,
                        "words": set(terms(text)), "metric_type": o.metric_type, "unit": o.unit,
                        "data_type": o.data_type, "about": (o.description or o.backend_help or "")[:120],
                        "series": st.get("series"), "labels": st.get("labels"), "values": st.get("values"),
                        "time_field": st.get("time_field")})
        return out

    return _cached(("dictionary", source_ids), make)


def _score(q: list[str], tokens: list[str], words: set[str], labels: set[str] | None = None) -> float:
    """The word itself in the name 3, a synonym 2, the start of a name word 1.5, the description 1;
    a word naming one of the metric's labels ("per application") 1; two name matches 1 more."""
    score, matched = 0.0, 0
    tset = set(tokens)
    for t in q:
        alts = SYNONYMS.get(t, set()) - {t}
        if t in tset:
            score += 3
            matched += 1
        elif alts & tset:
            score += 2
            matched += 1
        elif any(len(a) >= 4 and len(n) >= 4 and (n.startswith(a) or a.startswith(n)) for a in alts | {t} for n in tset):
            score += 1.5
            matched += 1
        elif ({t} | alts) & words:
            score += 1
        if labels and t in labels:
            score += 1
    if matched >= 2:
        score += 1
    return score - 0.1 * max(0, len(tset) - matched)


def _databases() -> list[Any]:
    from superset.models.core import Database

    from supagent.security import can_use_database
    from supagent.tools import agent_databases

    return agent_databases([d for d in db.session.query(Database).all() if can_use_database(d)])


def resolve(question: str) -> list[dict[str, Any]]:
    """The metrics, indices and fields a question most likely needs, best first."""
    from supagent.knowledge.store import source_for

    q = terms(question)
    if not q:
        return []
    t0 = time.time()
    found: dict[tuple, dict[str, Any]] = {}
    databases = _databases()
    by_source = {}
    for d in databases:
        try:
            by_source[source_for(d).id] = d
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
    db.session.commit()
    known = _dictionary(tuple(sorted(by_source)))
    for o in known:
        d = by_source.get(o["source_id"])
        labels = {stem(norm(str(x))) for x in o.get("labels") or []}
        score = _score(q, name_tokens(o["name"]), o["words"], labels)
        if d is None or score < 2:
            continue
        found[(d.id, o["kind"], o["parent"], o["name"])] = {**o, "database": d, "score": score}
    for d in databases:
        if d.backend != "promagg" or time.time() - t0 > BUDGET_S:
            continue
        try:
            names = _metric_names(d)
        except Exception as ex:  # pylint: disable=broad-except
            log.info("supagent resolve: metrics of %s: %s", d.database_name, ex)
            continue
        for name, tokens in names:
            key = (d.id, "metric", "", name)
            if key in found:
                continue
            score = _score(q, tokens, set())
            if score >= 2:
                found[key] = {"kind": "metric", "parent": "", "name": name, "database": d, "score": score,
                              "words": set()}
    _associations(q, found, databases)
    known_sources = {o["source_id"] for o in known}
    learned = {d.id for sid, d in by_source.items() if sid in known_sources}
    try:
        from supagent.tools import _catalog

        preferred = (_catalog().get("metrics") or {}).get("database")
    except Exception:  # pylint: disable=broad-except
        preferred = None
    # one entry per metric or index name: the preferred database, then a learned one, then the first
    best: dict[tuple, dict[str, Any]] = {}
    order = sorted(found.values(), key=lambda c: (-c["score"], c["database"].database_name != preferred,
                                                  c["database"].id not in learned, c["database"].id, len(c["name"])))
    for c in order:
        key = (c["kind"], c["parent"], c["name"])
        if key in best:
            best[key].setdefault("elsewhere", []).append(c["database"].id)
            continue
        best[key] = c
    ranked = sorted(best.values(), key=lambda c: -c["score"])
    return ranked[:DETAILED + NAMED]


def _associations(q: list[str], found: dict[tuple, dict[str, Any]], databases: list[Any]) -> None:
    """What earlier successful answers used for these words: a boost (or a new candidate)."""
    from supagent import settings
    from supagent.models import Association

    if not settings.get("learn.associations"):
        return
    ids = {d.id: d for d in databases}
    try:
        rows = db.session.query(Association).filter(Association.word.in_(q)).all()
    except Exception:  # pylint: disable=broad-except   (table not created yet: superset supagent init)
        db.session.rollback()
        return
    for a in rows:
        d = ids.get(a.database_id)
        if d is None:
            continue
        key = (d.id, a.kind, a.parent or "", a.name)
        c = found.setdefault(key, {"kind": a.kind, "parent": a.parent or "", "name": a.name, "database": d,
                                   "score": 0.0, "words": set()})
        c["score"] += min(3.0, 1.0 + 0.5 * (a.uses or 1))
        c["used_for"] = sorted(set(c.get("used_for", [])) | {a.word})


def _metric_details(c: dict[str, Any]) -> dict[str, Any]:
    """Type, unit and labels of a metric the dictionary does not know yet (one label lookup)."""
    from supagent.tools import _promagg_connection

    conn = _promagg_connection(c["database"])
    try:
        meta = conn.table_meta(c["name"])
    finally:
        conn.close()
    if meta is None:
        return {}
    return {"metric_type": meta.kind, "unit": meta.unit, "labels": list(meta.labels), "about": (meta.help or "")[:120]}


def _sql(c: dict[str, Any]) -> str:
    name, kind = c["name"], (c.get("metric_type") or "").lower()
    where = "WHERE ts >= TIMESTAMP '<start>' AND ts < TIMESTAMP '<end>'"
    if name.endswith("_bucket"):
        value = "HISTOGRAM_QUANTILE(0.95, SUM(RATE(value))) AS p95"
    elif kind in ("counter", "histogram", "summary") or name.endswith(("_total", "_count", "_sum")):
        value = "SUM(rate) AS per_second"
    else:
        value = "AVG(value) AS avg_value, MAX(value) AS max_value"
    return f'SELECT DATE_TRUNC(\'hour\', ts) AS t, {value} FROM "{name}" {where} GROUP BY 1 ORDER BY 1'


def where_block(question: str) -> str:
    """The candidates as a block of the system prompt ("" when nothing matches)."""
    try:
        ranked = resolve(question)
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent resolve: %s", ex)
        db.session.rollback()
        return ""
    if not ranked:
        return ""
    lines = ["\n\nWhere the data is (found for this question on names, descriptions and earlier answers, in the "
             "databases you may use; use these exact names and database ids, and do not search for them again):"]
    lookups = 0
    for c in ranked[:DETAILED]:
        d = c["database"]
        where = f'database {d.id} "{d.database_name}" ({d.backend})'
        if c["kind"] == "metric":
            if not c.get("metric_type") and lookups < LIVE_LOOKUPS:
                lookups += 1
                try:
                    c.update({k: v for k, v in _metric_details(c).items() if v})
                except Exception:  # pylint: disable=broad-except
                    pass
            bits = [b for b in (c.get("metric_type"), c.get("unit")) if b]
            if c.get("series"):
                bits.append(f"{c['series']} series")
            if c.get("labels"):
                bits.append("labels: " + ", ".join(map(str, c["labels"][:10])))
            about = f' - {c["about"]}' if c.get("about") else ""
            lines.append(f'- metric "{c["name"]}" ({"; ".join(bits) or "metric"}) in {where}{about}. SQL: {_sql(c)}')
        elif c["kind"] == "index":
            tf = f', time field "{c["time_field"]}"' if c.get("time_field") else ""
            about = f' - {c["about"]}' if c.get("about") else ""
            lines.append(f'- index "{c["name"]}"{tf} in {where}{about}')
        else:
            vals = c.get("values") or []
            v = f"; values: {', '.join(map(str, vals[:8]))}" if vals and len(vals) <= 20 else ""
            about = f' - {c["about"]}' if c.get("about") else ""
            lines.append(f'- field "{c["name"]}" ({c.get("data_type") or "field"}{v}) of index "{c["parent"]}" in '
                         f"{where}{about}")
        if c.get("elsewhere"):
            lines[-1] += f' (also in database {", ".join(map(str, sorted(set(c["elsewhere"]))[:4]))})'
        if c.get("used_for"):
            lines[-1] += f' (used before for: {", ".join(c["used_for"][:5])})'
    more = [f'{c["name"]}' + (f' ({c["parent"]})' if c["kind"] == "field" else "") for c in ranked[DETAILED:]]
    if more:
        lines.append("- also matching: " + ", ".join(more))
    return "\n".join(lines)
