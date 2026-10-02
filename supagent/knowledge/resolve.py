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
import math
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

_SYNONYMS: dict[str, set[str]] = {
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

# the same words as the questions and the names become (stems): failed and failure are "fail"
SYNONYMS: dict[str, set[str]] = {}
for _k, _v in _SYNONYMS.items():
    SYNONYMS.setdefault(stem(_k), set()).update(stem(x) for x in _v)

# words that say what to do with the data, not which data: never matched on names
GENERIC = set("""over during since until per rate rates count number total totals average avg sum max min maximum
minimum value values chart graph plot table need see want show give get last past next hour minute day week
month year today yesterday now please can could would top highest lowest most least trend evolution compare list
what which much many time times current currently moyenne nombre taux graphique courbe tableau dernier derniere
heure jour semaine mois annee aujourd hier maintenant plus moin evolution liste""".split())
GENERIC |= {stem(w) for w in GENERIC}

_LOCK = threading.Lock()
_CACHE: dict[Any, tuple[float, Any, str]] = {}


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


def _cached(key: Any, make: Any, fresh: bool = True) -> Any:
    """Kept TTL seconds; `fresh`: made again as soon as the knowledge changed (a description
    written, a learning run: on any server, see freshness), not only after TTL."""
    from supagent.knowledge.freshness import stamp

    now, changed = time.time(), stamp() if fresh else ""
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL and hit[2] == changed:
            return hit[1]
    value = make()
    with _LOCK:
        _CACHE[key] = (now, value, changed)
    return value


FAILED_TTL = 60.0                 # a live list that failed is not asked again for this long (Mimir down)


def _metric_names(database: Any) -> list[tuple[str, list[str]]]:
    """(name, its tokens) of every metric of a metrics database, from the live list."""
    from supagent.tools import _promagg_connection

    with _LOCK:
        failed = _CACHE.get(("failed", database.id))
    if failed and time.time() - failed[0] < FAILED_TTL:
        raise ConnectionError(f"the metric list of {database.database_name} failed less than a minute ago")

    def make() -> list[tuple[str, list[str]]]:
        conn = _promagg_connection(database)
        try:
            return [(n, name_tokens(n)) for n in conn.list_tables()]
        finally:
            conn.close()

    try:
        return _cached(("metrics", database.id), make, fresh=False)      # the live list: not the dictionary
    except Exception:
        with _LOCK:
            _CACHE[("failed", database.id)] = (time.time(), None, "")
        raise


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
                        "ai": bool(o.description) and o.description_source == "llm" and not o.verified,
                        "series": st.get("series"), "labels": st.get("labels"), "values": st.get("values"),
                        "time_field": st.get("time_field")})
        return out

    return _cached(("dictionary", source_ids), make)


class Rarity:
    """How rare each word of the names is: a match on a word few names have ("elasticsearch")
    counts more than one on a word hundreds of names have ("node" of node_*); from 0.7 (in every
    name) to 1 (in one, or none)."""

    def __init__(self, names: list[list[str]]) -> None:
        from collections import Counter

        self.n = max(1, len(names))
        self.df = Counter(t for tokens in names for t in set(tokens))

    def __call__(self, t: str) -> float:
        df = self.df.get(t, 0)
        if df <= 1 or self.n <= 1:
            return 1.0
        return 0.7 + 0.3 * max(0.0, 1.0 - math.log(1 + df) / math.log(1 + self.n))


def _score(q: list[str], tokens: list[str], words: set[str], labels: set[str] | None = None,
           rarity: Rarity | None = None) -> float:
    """The word itself in the name 3, a synonym 2, the start of a name word 1.5 (each weighted by
    the rarity of the word), the description 1; a word naming one of the metric's labels ("per
    application") 1; two name matches 1 more."""
    score, matched = 0.0, 0
    tset = set(tokens)
    for t in q:
        alts = SYNONYMS.get(t, set()) - {t}
        w = rarity(t) if rarity is not None else 1.0
        if t in tset:
            score += 3 * w
            matched += 1
        elif alts & tset:
            score += 2 * (min(rarity(a) for a in alts & tset) if rarity is not None else 1.0)
            matched += 1
        elif any(len(a) >= 4 and len(n) >= 4 and (n.startswith(a) or a.startswith(n)) for a in alts | {t} for n in tset):
            score += 1.5 * w
            matched += 1
        elif ({t} | alts) & words:
            score += 1
        if labels and t in labels:
            score += 1
    if matched >= 2:
        score += 1
    return score - 0.1 * max(0, len(tset) - matched)


def _rarity(known: list[dict[str, Any]], lists: dict[int, list[tuple[str, list[str]]]]) -> Rarity:
    """The rarity of the words of every name the resolver scores (cached with the lists)."""
    key = ("rarity", len(known), tuple((i, len(v)) for i, v in sorted(lists.items())))

    def make() -> Rarity:
        seen: dict[str, list[str]] = {}
        for o in known:
            seen.setdefault(o["name"], name_tokens(o["name"]))
        for names in lists.values():
            for n, tokens in names:
                seen.setdefault(n, tokens)
        return Rarity(list(seen.values()))

    return _cached(key, make)


def _databases() -> list[Any]:
    from superset.models.core import Database

    from supagent.security import can_use_database
    from supagent.tools import agent_databases

    return agent_databases([d for d in db.session.query(Database).all() if can_use_database(d)])


def tenants_of(database_id: int) -> list[str]:
    """The Mimir tenants of a metrics database (values of its __tenant_id__ labels, learned)."""
    from supagent.models import KObject, Source

    def make() -> list[str]:
        src = db.session.query(Source.id).filter(Source.database_id == database_id).first()
        if src is None:
            return []
        values: set[str] = set()
        for (st,) in (db.session.query(KObject.stats).filter(KObject.source_id == src[0], KObject.kind == "label",
                                                              KObject.name == "__tenant_id__").limit(200)):
            values |= set(map(str, (st or {}).get("values") or []))
        return sorted(values)

    try:
        return _cached(("tenants", database_id), make)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return []


def resolve(question: str, limit: int = DETAILED + NAMED) -> list[dict[str, Any]]:
    """The metrics, indices and fields a question most likely needs, best first (`limit` of them)."""
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
    lists: dict[int, list[tuple[str, list[str]]]] = {}     # the live metric names of each metrics database
    for d in databases:
        if d.backend != "promagg" or time.time() - t0 > BUDGET_S:
            continue
        try:
            lists[d.id] = _metric_names(d)
        except Exception as ex:  # pylint: disable=broad-except
            log.info("supagent resolve: metrics of %s: %s", d.database_name, ex)
    rarity = _rarity(known, lists)
    for o in known:
        d = by_source.get(o["source_id"])
        labels = {stem(norm(str(x))) for x in o.get("labels") or []}
        score = _score(q, name_tokens(o["name"]), o["words"], labels, rarity)
        if d is None or score < 2:
            continue
        found[(d.id, o["kind"], o["parent"], o["name"])] = {**o, "database": d, "score": score}
    live: dict[int, set[str]] = {}                  # metrics that exist now, per metrics database
    for d in databases:
        names = lists.get(d.id)
        if names is None:
            continue
        live[d.id] = {n for n, _t in names}
        for name, tokens in names:
            key = (d.id, "metric", "", name)
            if key in found:
                continue
            score = _score(q, tokens, set(), None, rarity)
            if score >= 2:
                found[key] = {"kind": "metric", "parent": "", "name": name, "database": d, "score": score,
                              "words": set()}
    _associations(q, found, databases, live, _gone(tuple(sorted(by_source))), by_source)
    known_sources = {o["source_id"] for o in known}
    learned = {d.id for sid, d in by_source.items() if sid in known_sources}
    # one entry per metric, index or field name, in the database the question names, else the one of
    # the policy (preferred_databases), stated in "Where the data is" with the others that have it
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for c in found.values():
        groups.setdefault((c["kind"], c["parent"], c["name"]), []).append(c)
    named = _named(question, databases) if any(len(g) > 1 for g in groups.values()) else {}
    order = preferred_databases(databases)
    charts = _charts_per_table() if any(len(g) > 1 for g in groups.values()) else {}
    best: dict[tuple, dict[str, Any]] = {}
    for key, cands in groups.items():
        def rank(c: dict[str, Any]) -> tuple:
            d = c["database"]
            table = c["parent"] or c["name"]
            return (d.id not in named, order.get(d.id, (len(order), ""))[0], -c["score"], -charts.get((d.id, table), 0),
                    d.id not in learned, d.id, len(c["name"]))

        cands.sort(key=rank)
        top = dict(cands[0])
        top["score"] = max(c["score"] for c in cands)
        if len(cands) > 1:
            d = top["database"]
            table = top["parent"] or top["name"]
            top["elsewhere"] = [c["database"] for c in cands[1:]]
            top["why_here"] = (named.get(d.id) or order.get(d.id, (0, ""))[1]
                               or (f"the team's charts use it ({charts[(d.id, table)]})" if charts.get((d.id, table))
                                   else "the one learned" if d.id in learned else "the first one"))
        best[key] = top
    ranked = sorted(best.values(), key=lambda c: -c["score"])
    return ranked[:limit]


def _named(question: str, databases: list[Any]) -> dict[int, str]:
    """The databases the question names (by name or by a tenant only one has): see scope."""
    try:
        from supagent.knowledge.scope import named_databases, tenant_databases

        return {**tenant_databases(question, databases), **named_databases(question, databases)}
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return {}


def preferred_databases(databases: list[Any]) -> dict[int, tuple[int, str]]:
    """The databases used first when the same name is in several (id -> (rank, why)): the admins' list
    (agent.preferred_databases, names or ids), then the catalog's metrics database."""
    from supagent import settings

    out: dict[int, tuple[int, str]] = {}
    try:
        wanted = [str(x).strip() for x in settings.get("agent.preferred_databases") or [] if str(x).strip()]
    except Exception:  # pylint: disable=broad-except
        wanted = []
    for w in wanted:
        for d in databases:
            if (str(d.id) == w or (d.database_name or "").lower() == w.lower()) and d.id not in out:
                out[d.id] = (len(out), "the admins' preferred database (agent.preferred_databases)")
    try:
        from supagent.tools import _catalog

        metrics_db = (_catalog().get("metrics") or {}).get("database")
    except Exception:  # pylint: disable=broad-except
        metrics_db = None
    for d in databases:
        if metrics_db and d.database_name == metrics_db and d.id not in out:
            out[d.id] = (len(out), "the catalog's metrics database")
    return out


def _charts_per_table() -> dict[tuple[int, str], int]:
    """Saved charts per (database id, table) of a physical dataset: the database the team's charts use."""
    def make() -> dict[tuple[int, str], int]:
        from sqlalchemy import func
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.slice import Slice

        rows = (db.session.query(SqlaTable.database_id, SqlaTable.table_name, func.count(Slice.id))
                .join(Slice, (Slice.datasource_id == SqlaTable.id) & (Slice.datasource_type == "table"))
                .filter((SqlaTable.sql.is_(None)) | (SqlaTable.sql == ""))
                .group_by(SqlaTable.database_id, SqlaTable.table_name).all())
        return {(i, t): n for i, t, n in rows}

    try:
        return _cached(("charts per table",), make, fresh=False)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return {}


ASSOCIATION_DAYS = 60             # an association no answer used for this long is not used


def _gone(source_ids: tuple[int, ...]) -> set[tuple[int, str, str]]:
    """(source id, kind, name) of the metrics and indices the learning saw disappear."""
    from supagent.models import KObject

    def make() -> set[tuple[int, str, str]]:
        rows = (db.session.query(KObject.source_id, KObject.kind, KObject.name)
                .filter(KObject.source_id.in_(source_ids), KObject.kind.in_(("metric", "index")),
                        KObject.gone_at.isnot(None)).all())
        return {(sid, kind, name) for sid, kind, name in rows}

    return _cached(("gone", source_ids), make) if source_ids else set()


def gone_names() -> set[str]:
    """Names of the metrics and indices the learning saw disappear and that no database still has."""
    from supagent.models import KObject

    def make() -> set[str]:
        kinds = ("metric", "index")
        gone = {n for (n,) in db.session.query(KObject.name).filter(KObject.kind.in_(kinds),
                                                                     KObject.gone_at.isnot(None))}
        if not gone:
            return set()
        alive = {n for (n,) in db.session.query(KObject.name).filter(KObject.kind.in_(kinds), KObject.gone_at.is_(None),
                                                                      KObject.name.in_(list(gone)[:5000]))}
        return gone - alive

    try:
        return _cached(("gone_names",), make)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return set()


def mentions_gone(text: str | None, names: set[str] | None = None) -> bool:
    """A learned answer or a memory that names a metric or index that no longer exists: not given
    to the agent (it would send it to data that is not there)."""
    names = gone_names() if names is None else names
    if not names or not text:
        return False
    return any(w in names for w in re.findall(r"[A-Za-z0-9_:.@\-]+", text))


def _associations(q: list[str], found: dict[tuple, dict[str, Any]], databases: list[Any],
                  live: dict[int, set[str]] | None = None, gone: set[tuple[int, str, str]] | None = None,
                  by_source: dict[int, Any] | None = None) -> None:
    """What earlier successful answers used for these words: a boost (or a new candidate). Not
    used: associations no answer used for ASSOCIATION_DAYS, and those to a metric that no longer
    exists (live list) or to a metric or index the learning saw disappear."""
    import datetime as dt

    from supagent import settings
    from supagent.models import Association

    if not settings.get("learn.associations"):
        return
    ids = {d.id: d for d in databases}
    source_of = {d.id: sid for sid, d in (by_source or {}).items()}
    since = dt.datetime.utcnow() - dt.timedelta(days=ASSOCIATION_DAYS)
    try:
        rows = db.session.query(Association).filter(Association.word.in_(q)).all()
    except Exception:  # pylint: disable=broad-except   (table not created yet: superset supagent init)
        db.session.rollback()
        return
    for a in rows:
        d = ids.get(a.database_id)
        if d is None:
            continue
        if a.updated_at is not None and a.updated_at < since and a.source != "admin":   # an admin's never fades
            continue
        if a.kind == "metric" and live and a.database_id in live and a.name not in live[a.database_id]:
            continue
        if gone and (source_of.get(a.database_id), a.kind, a.name) in gone:
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
    elif "cpu" in name.lower() and "mode" in {str(x).lower() for x in c.get("labels") or []}:
        # CPU seconds per mode: the busy share (SUM(rate) of every mode is the number of cores)
        value = "100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate) AS busy_pct"
    elif kind in ("counter", "histogram", "summary") or name.endswith(("_total", "_count", "_sum")):
        value = "SUM(rate) AS per_second"
    else:
        value = "AVG(value) AS avg_value, MAX(value) AS max_value"
    return f'SELECT DATE_TRUNC(\'hour\', ts) AS t, {value} FROM "{name}" {where} GROUP BY 1 ORDER BY 1'


VALUE_TOKEN = re.compile(r"(?<![\w-])([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)+|[A-Z][A-Z0-9]{2,})(?![\w-])")
MAX_VALUES = 4                    # values of the question looked up
VALUE_ROWS = 200                  # fields and labels read per value


def value_places(question: str, databases: list[Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Where the values the question names are ("BILLING_API": the field APPLICATION of an index and the
    label application of metrics), when they are in more than one kind of data or database and the
    question does not say which data it means: {value: [{database_id, database, kind, name, tables}]}."""
    tokens = value_tokens(question)
    if not tokens:
        return {}
    databases = databases if databases is not None else _databases()
    names = {d.id: d.database_name for d in databases}
    out: dict[str, list[dict[str, Any]]] = {}
    for token, places in value_rows(tokens, databases).items():
        kinds = {k for (_i, k, _n) in places}
        dbs = {i for (i, _k, _n) in places}
        if len(kinds) < 2 and len(dbs) < 2:
            continue                                    # one kind of data in one database: nothing to choose
        subjects = {t for parents in places.values() for p in parents for t in name_tokens(p)}
        if set(terms(question)) & subjects:
            continue                                    # "jobs of BILLING": the question says which data
        out[token] = [{"database_id": i, "database": names.get(i, ""), "kind": kind, "name": name,
                       "tables": sorted(set(parents))} for (i, kind, name), parents in sorted(places.items())]
    return out


def value_tokens(question: str) -> list[str]:
    """The words of a question that look like values (BILLING_API, ORDERS, srv-a-1), at most MAX_VALUES."""
    return [t for t in dict.fromkeys(VALUE_TOKEN.findall(question or "")) if not t.isdigit()][:MAX_VALUES]


def value_rows(tokens: list[str], databases: list[Any]) -> dict[str, dict[tuple[int, str, str], list[str]]]:
    """Where each value is: {value: {(database id, "field" or "label", its name): [indices or metrics]}}."""
    from sqlalchemy import Text, cast, func

    from supagent.models import KObject, Source

    by_source = {sid: d for sid, d in ((s.id, next((d for d in databases if d.id == s.database_id), None))
                                       for s in db.session.query(Source)) if d is not None}
    if not by_source or not tokens:
        return {}
    # a label of the same name has much the same values in every metric: one of them is read (tens of
    # thousands of labels stay unread), and the metrics that have it are counted
    reps = (db.session.query(func.min(KObject.id)).filter(KObject.kind == "label", KObject.gone_at.is_(None),
                                                          KObject.source_id.in_(list(by_source)))
            .group_by(KObject.source_id, KObject.name))
    out: dict[str, dict[tuple[int, str, str], list[str]]] = {}
    for token in tokens:
        like = cast(KObject.stats, Text).like(f'%"{token}"%')
        rows = (db.session.query(KObject.source_id, KObject.kind, KObject.parent, KObject.name, KObject.stats)
                .filter(KObject.gone_at.is_(None), KObject.source_id.in_(list(by_source)), like,
                        ((KObject.kind == "field") | KObject.id.in_(reps))).limit(VALUE_ROWS).all())
        places: dict[tuple[int, str, str], list[str]] = {}
        for sid, kind, parent, name, stats in rows:
            if token not in map(str, (stats or {}).get("values") or []):
                continue
            if kind == "label":                         # every metric that has the label
                parent_rows = (db.session.query(KObject.parent).filter(
                    KObject.kind == "label", KObject.source_id == sid, KObject.name == name,
                    KObject.gone_at.is_(None)).limit(200).all())
                places.setdefault((by_source[sid].id, kind, name), []).extend(p for (p,) in parent_rows)
                continue
            places.setdefault((by_source[sid].id, kind, name), []).append(parent or "")
        if places:
            out[token] = places
    return out


def place_text(p: dict[str, Any]) -> str:
    tables = p["tables"]
    what = (f"field {p['name']} of index {', '.join(tables[:3])}" if p["kind"] == "field" else
            f"label {p['name']} of {len(tables)} metric(s): {', '.join(tables[:4])}" + (" ..." if len(tables) > 4 else ""))
    return f'{what} (database {p["database_id"]} "{p["database"]}")'


def values_lines(question: str, databases: list[Any] | None = None) -> list[str]:
    """The lines of "Where the data is" about value_places."""
    return [f'- "{token}" is a value of: ' + "; ".join(place_text(p) for p in places[:6]) + ". If the question can "
            "mean either, say in the first line which one you answered and name the other one."
            for token, places in value_places(question, databases).items()]


def other_reading(answer: str, tables_read: set[str], places: dict[str, list[dict[str, Any]]]) -> str:
    """A value the question named is in data this answer did not read and does not mention: said in one
    line (the other reading), e.g. an application that also has HTTP metrics."""
    low = (answer or "").lower()
    read = {t.lower() for t in tables_read}
    notes = []
    for token, ps in places.items():
        used = [p for p in ps if read & {t.lower() for t in p["tables"]}]
        if not used:
            continue                                   # none of them read: nothing to say about the others
        kinds = {p["kind"] for p in used}
        other = [p for p in ps if p["kind"] not in kinds and not any(t.lower() in low for t in p["tables"])]
        if other:
            notes.append(f'"{token}" is also a value of the {place_text(other[0])}')
    if not notes:
        return ""
    return "\n\n(Not read for this answer: " + "; ".join(notes[:2]) + ". Ask if you meant that data.)"


def where_block(question: str, shown: set[str] | None = None) -> str:
    """The candidates as a block of the system prompt ("" when nothing matches). `shown` gets the
    titles of the knowledge pieces of the metrics and indices given with their details."""
    try:
        ranked = resolve(question)
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent resolve: %s", ex)
        db.session.rollback()
        return ""
    from supagent.knowledge.excluded import is_excluded, of_table

    ranked = [c for c in ranked                           # what the team said not to use is never proposed
              if not is_excluded(c["kind"], c["database"].id, c["name"], c.get("parent") or "")]
    try:
        values = values_lines(question)
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent resolve: values: %s", ex)
        db.session.rollback()
        values = []
    if not ranked and not values:
        return ""
    lines = ["\n\nWhere the data is (found for this question on names, descriptions and earlier answers, in the "
             "databases you may use; use these exact names and database ids, and do not search for them again):"]
    lookups = 0
    for c in ranked[:DETAILED]:
        d = c["database"]
        if shown is not None and c["kind"] in ("metric", "index"):
            shown.add(f'{c["kind"]} {c["name"]}')
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
            tenants = tenants_of(d.id) if "__tenant_id__" in map(str, c.get("labels") or []) else []
            if len(tenants) > 1:
                bits.append("tenants (__tenant_id__, each usually a different application or subject): "
                            + ", ".join(tenants[:12]))
            about = f' - {c["about"]}' + (" (AI-written, unverified)" if c.get("ai") else "") if c.get("about") else ""
            lines.append(f'- metric "{c["name"]}" ({"; ".join(bits) or "metric"}) in {where}{about}. SQL: {_sql(c)}')
        elif c["kind"] == "index":
            tf = f', time field "{c["time_field"]}"' if c.get("time_field") else ""
            about = f' - {c["about"]}' + (" (AI-written, unverified)" if c.get("ai") else "") if c.get("about") else ""
            lines.append(f'- index "{c["name"]}"{tf} in {where}{about}')
        else:
            vals = c.get("values") or []
            v = f"; values: {', '.join(map(str, vals[:8]))}" if vals and len(vals) <= 20 else ""
            about = f' - {c["about"]}' + (" (AI-written, unverified)" if c.get("ai") else "") if c.get("about") else ""
            lines.append(f'- field "{c["name"]}" ({c.get("data_type") or "field"}{v}) of index "{c["parent"]}" in '
                         f"{where}{about}")
        if c["kind"] in ("metric", "index"):
            banned = of_table(d.id, c["name"])
            if banned:
                lines[-1] += " DO NOT USE (the team): " + "; ".join(f'"{x["name"]}" ({x["why"][:80]})' for x in banned[:8])
        if c.get("elsewhere"):
            others = ", ".join(f'{o.id} "{o.database_name}"' for o in sorted(c["elsewhere"], key=lambda o: o.id)[:4])
            lines[-1] += (f" (also in database {others}: use database {d.id} unless the question asks for another; "
                          f"{c.get('why_here') or 'the first one'})")
        if c.get("used_for"):
            lines[-1] += f' (used before for: {", ".join(c["used_for"][:5])})'
    more = [f'{c["name"]}' + (f' ({c["parent"]})' if c["kind"] == "field" else "") for c in ranked[DETAILED:]]
    if more:
        lines.append("- also matching: " + ", ".join(more))
    lines += values
    return "\n".join(lines)
