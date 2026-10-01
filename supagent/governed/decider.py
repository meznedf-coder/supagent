"""The decider: the knowledge one question needs, chosen before any plan or query.

Every source stays available: the data dictionary (indices, fields, metrics, labels and their values),
the catalog (formulas, rules, glossary, facts, notes), the team's and the user's memory, the learned
answers, the documents, the Context, Superset's charts and dashboards. For one question:

1. gather (no LLM): candidate tables (an index or a metric in one database) with their features (see
   gate), and candidate knowledge items. It reads the dictionary directly (resolver, values), so stale
   search pieces or missing vectors cost recall only through the search feature;
2. rank the tables with the gate (features x learned weights);
3. choose (one LLM call, structured): for each need of the question the tables that hold it, the
   knowledge items that change how to count, the alternatives that would give other numbers, what no
   source holds;
4. pack: the chosen tables in full (fields and their values, labels, units, verified or AI-written
   descriptions), the rules about their fields, the chosen knowledge, the learned answers on them.

More databases, metrics or documents only change step 1 (measured by lab/decider_eval.py); what the
planner reads stays the pack of the chosen tables.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from superset import db

from supagent.governed import gate

log = logging.getLogger(__name__)

RESOLVER_LIMIT = 40          # tables and fields from the resolver (the classic prompt shows 10)
PREVIOUS_FACTOR = 0.6        # a follow-up: the tables of the question before count this much
CHOOSE_SECONDS = 120    # the decider's one LLM call at most
SEARCH_K = 30                # knowledge pieces searched
SHOWN_TABLES = 14            # tables the LLM chooses among
SHOWN_KNOWLEDGE = 18         # knowledge items the LLM chooses among
WIDE_LABEL = 20              # a value in the label of more metrics than this: it boosts, it adds none
PACK_CHARS = 16000           # the chosen knowledge given to the planner, at most
TABLE_CHARS = 5000           # one table's details, at most
VALUES_SHOWN = 20            # values of a field or a label given in full up to this many
TEAM_KINDS = ("rule", "glossary", "memory")   # always shown: few, and they change how to count


@dataclass
class Candidate:
    subject: str                                # data:<database id>:<table> | entry:<id> | memory:<id> | ...
    kind: str                                   # index | metric | rule | glossary | memory | recipe | doc | ...
    title: str
    about: str = ""
    database_id: int | None = None
    database: str = ""
    backend: str = ""
    table: str = ""
    fields: list[str] = field(default_factory=list)     # fields or labels the question matched
    values: dict[str, str] = field(default_factory=dict)  # a value of the question -> its field or label
    features: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    ai: bool = False                            # only an AI-written, unverified description
    text: str = ""                              # a knowledge item's text
    ref: str = ""                               # T1, K2: its name for the LLM
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Gathered:
    question: str
    tables: dict[str, Candidate]
    knowledge: dict[str, Candidate]
    named: dict[int, str]
    terms: list[str]
    seconds: float = 0.0

    def ranked(self) -> list[Candidate]:
        """Best first, one entry per table: the same index or metric in several databases is one table, in
        the database the clues choose (named, the policy); the others are listed in its extra["twins"]."""
        return [c for c in sorted(self.tables.values(), key=lambda c: (-c.score, c.subject))
                if not c.extra.get("twin_of")]

    def items(self) -> list[Candidate]:
        return sorted(self.knowledge.values(), key=lambda c: (c.kind not in TEAM_KINDS, -c.score, c.subject))


@dataclass
class Selection:
    kind: str = "data"                          # data | action | explain | status | other
    needs: list[dict[str, Any]] = field(default_factory=list)       # [{"what", "tables": [subject]}]
    knowledge: list[str] = field(default_factory=list)              # subjects
    ambiguous: list[dict[str, Any]] = field(default_factory=list)   # [{"what", "options": [subject], "why"}]
    missing: list[str] = field(default_factory=list)
    confidence: str = "medium"
    by: str = "llm"                             # llm | gate (the LLM gave nothing usable)

    def tables(self) -> list[str]:
        return list(dict.fromkeys(s for n in self.needs for s in n.get("tables") or []))


@dataclass
class Decision:
    gathered: Gathered
    selection: Selection
    pack: "Pack"
    route_id: int | None = None


# --------------------------------------------------------------------------------------------- #
# 1. gather
# --------------------------------------------------------------------------------------------- #
def gather(question: str, previous: str = "", user_id: int | None = None) -> Gathered:
    """Candidate tables and knowledge items of a question (see the module's docstring)."""
    from supagent.knowledge import resolve as R

    t0 = time.time()
    databases = R._databases()
    by_id = {d.id: d for d in databases}
    tables: dict[str, Candidate] = {}
    knowledge: dict[str, Candidate] = {}

    def table(dbid: int, name: str, kind: str) -> Candidate | None:
        d = by_id.get(dbid)
        if d is None or not name:
            return None
        key = f"data:{dbid}:{name}"
        c = tables.get(key)
        if c is None:
            c = tables[key] = Candidate(subject=key, kind=kind, title=name, database_id=dbid,
                                        database=d.database_name, backend=d.backend, table=name)
        return c

    def up(c: Candidate | None, feature: str, value: float) -> None:
        if c is not None:
            c.features[feature] = max(c.features.get(feature, 0.0), round(float(value), 4))

    # the resolver: names, descriptions, synonyms, associations (from the dictionary and live metric lists)
    for text, factor in ((question, 1.0), (previous, PREVIOUS_FACTOR)):
        if not (text or "").strip():
            continue
        try:
            found = R.resolve(text, limit=RESOLVER_LIMIT)
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            log.warning("supagent decider: resolver", exc_info=True)
            found = []
        top = max((c["score"] for c in found), default=1.0) or 1.0
        for i, c in enumerate(found):
            name = c["parent"] if c["kind"] == "field" else c["name"]
            kind = "index" if c["kind"] in ("index", "field") else "metric"
            for d in [c["database"]] + list(c.get("elsewhere") or []):
                t = table(d.id, name, kind)
                up(t, "resolver", factor * c["score"] / top)
                up(t, "rank", factor / (1 + i))
                if d.id == c["database"].id:
                    up(t, "first", factor)        # the resolver's policy put this database first
                if t is None:
                    continue
                if c["kind"] == "field" and c["name"] not in t.fields:
                    t.fields.append(c["name"])
                if c.get("used_for"):
                    t.extra.setdefault("used_for", sorted(set(c["used_for"])))
                if c.get("why_here") and d.id == c["database"].id:
                    t.extra["why_here"] = c["why_here"]

    # the values the question names (BILLING_API is a value of the field APPLICATION, of the label application)
    try:
        rows = R.value_rows(R.value_tokens(question), databases)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        rows = {}
    for token, places in rows.items():
        for (dbid, kind, name), parents in places.items():
            wide = kind == "label" and len(set(parents)) > WIDE_LABEL
            for p in sorted(set(parents)):
                if not p:
                    continue
                key = f"data:{dbid}:{p}"
                t = tables.get(key) if wide else table(dbid, p, "index" if kind == "field" else "metric")
                if t is None:
                    continue
                up(t, "value", 1.0 if kind == "field" else 1.0 / (1.0 + math.log1p(len(set(parents)))))
                t.values[token] = name
                if name not in t.fields:
                    t.fields.append(name)

    terms = R.terms(question)

    # the knowledge search: pieces of the dictionary count for their table; the others are knowledge items
    _search(question, table, up, knowledge)
    _neighbors(question, user_id, table, up, by_id)

    # learned answers to similar questions: the tables they read, and the answers themselves
    try:
        from supagent.knowledge.experience import recipes_for

        for i, r in enumerate(recipes_for(question, limit=5)):
            for name in _recipe_tables(r):
                d = by_id.get(r["database_id"])
                t = table(r["database_id"], name, "metric" if d is not None and d.backend == "promagg" else "index")
                up(t, "recipe", (1.0 / (1 + i)) * (1.0 if r.get("status") == "confirmed" else 0.8))
            k = knowledge.setdefault(f"recipe:{r['id']}", Candidate(
                subject=f"recipe:{r['id']}", kind="recipe", title=(r.get("question") or "")[:160],
                text=f"{r.get('tool')}: {(r.get('query') or '')[:600]}", database_id=r["database_id"]))
            k.features["recipe"] = max(k.features.get("recipe", 0.0), 1.0 / (1 + i))
            k.extra["status"] = r.get("status")
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        log.warning("supagent decider: learned answers", exc_info=True)

    # what the question names: databases (by name, by a tenant only one has), charts and dashboards
    named: dict[int, str] = {}
    try:
        from supagent.knowledge.scope import named_databases, references, tenant_databases

        named = _most_specific({**tenant_databases(question, databases), **named_databases(question, databases)},
                               question, by_id)
        chart_ids, board_ids = references(question)
        for dbid, name in _chart_tables(chart_ids, board_ids):
            t = table(dbid, name, "metric" if by_id.get(dbid) is not None and by_id[dbid].backend == "promagg"
                      else "index")
            up(t, "named", 1.0)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        log.warning("supagent decider: named databases or charts", exc_info=True)
    for t in tables.values():
        if t.database_id in named:
            up(t, "named", 1.0)
    _named_elsewhere(named, tables, table, by_id)

    # the same table in several databases: the policy; the team's charts; confirmed answers with these words
    try:
        order = R.preferred_databases(databases)
        charts = R._charts_per_table()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        order, charts = {}, {}
    count = Counter(t.table for t in tables.values())
    most = max(charts.values(), default=0)
    prior = gate.priors(terms)
    for t in tables.values():
        if count[t.table] > 1 and t.database_id in order:
            up(t, "preferred", 1.0 / (1 + order[t.database_id][0]))
        n = charts.get((t.database_id, t.table), 0)
        if n and most:
            up(t, "charts", math.log1p(n) / math.log1p(most))
        if prior.get(t.subject):
            up(t, "prior", prior[t.subject])

    _share_twins(tables)
    _family(question, tables)
    _describe_tables(tables)
    _team_knowledge(question, user_id, knowledge)
    _drop_excluded(tables)
    w = gate.weights()
    for c in list(tables.values()) + list(knowledge.values()):
        c.score = round(gate.score(c.features, w), 4)
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for t in sorted(tables.values(), key=lambda c: (-c.score, c.subject)):
        groups.setdefault((t.backend, t.table), []).append(t)
    for first, *others in groups.values():
        first.extra["twins"] = [o.database_id for o in others]
        for o in others:
            o.extra["twin_of"] = first.subject
    return Gathered(question=question, tables=tables, knowledge=knowledge, named=named, terms=terms,
                    seconds=round(time.time() - t0, 3))


def _search(question: str, table: Callable, up: Callable, knowledge: dict[str, Candidate]) -> None:
    from supagent.knowledge.search import CONTEXT_PLACES, search
    from supagent.models import KObject, Source

    try:
        pieces = search(question, k=SEARCH_K, lower={"context": CONTEXT_PLACES})
    except Exception:  # pylint: disable=broad-except   (words and vectors may both fail: the resolver stays)
        db.session.rollback()
        log.warning("supagent decider: search", exc_info=True)
        return
    if not pieces:
        return
    best = max(p["score"] for p in pieces) or 1.0
    ids = [int(p["ref"].split(":")[1]) for p in pieces if re.match(r"object:\d+$", p["ref"])]
    objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ids or [-1]))}
    src_db = {s.id: s.database_id for s in db.session.query(Source)}
    for p in pieces:
        m = re.match(r"object:(\d+)$", p["ref"])
        if m:
            o = objs.get(int(m.group(1)))
            if o is None:
                continue
            name = o.parent if o.kind in ("field", "label") else o.name
            t = table(src_db.get(o.source_id), name, "index" if o.kind in ("index", "field") else "metric")
            up(t, "search", p["score"] / best)
            if "meaning" in (p.get("via") or ""):
                up(t, "meaning", 1.0)
            if p.get("spelled"):                  # a name or a value spelled like a word of the question (store)
                up(t, "spelling", max(x["similarity"] for x in p["spelled"]))
            continue
        k = knowledge.get(p["ref"])
        if k is None:
            k = knowledge[p["ref"]] = Candidate(subject=p["ref"], kind=p["kind"], title=(p.get("title") or "")[:160],
                                                text=(p.get("text") or "")[:1500])
        k.features["search"] = max(k.features.get("search", 0.0), p["score"] / best)


def _neighbors(question: str, user_id: int | None, table: Callable, up: Callable, by_id: dict[int, Any]) -> None:
    """The tables that answered the questions like this one (the knowledge store: routes people confirmed, the
    user's own earlier answers), each voting as close as its question is."""
    try:
        from supagent.knowledge import pgstore

        if not pgstore.active():
            return
        votes = pgstore.neighbors(question, user_id)
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent decider: neighbours", exc_info=True)
        return
    for subject, w in votes.items():
        m = re.match(r"data:(\d+):(.+)$", subject)
        if not m:
            continue
        d = by_id.get(int(m.group(1)))
        up(table(int(m.group(1)), m.group(2), "metric" if d is not None and d.backend == "promagg" else "index"),
           "neighbors", w)


def _named_elsewhere(named: dict[int, str], tables: dict[str, Candidate], table: Callable,
                     by_id: dict[int, Any]) -> None:
    """A database the question names that has no candidate (not learned: a replica, a gateway): the best
    tables of the same kind of database, when it has them too (its live list)."""
    from supagent.knowledge.scope import _has_table

    for dbid in named:
        d = by_id.get(dbid)
        if d is None or any(t.database_id == dbid for t in tables.values()):
            continue
        same = sorted((t for t in tables.values() if t.backend == d.backend), key=lambda t: -gate.score(t.features))
        for t in same[:5]:
            if not _has_table(dbid, t.table):
                continue
            c = table(dbid, t.table, t.kind)
            if c is None:
                continue
            c.features.update({k: v for k, v in t.features.items() if k in ("resolver", "rank", "search", "value",
                                                                             "recipe", "meaning")})
            c.features["named"] = 1.0
            c.fields, c.values = list(t.fields), dict(t.values)
            c.extra["twin"] = t.database_id      # its fields and labels: the ones learned in that database


CONTENT = ("resolver", "rank", "search", "value", "recipe", "meaning")   # what a table holds (same in its twins)
FAMILIES = {"promagg": re.compile(r"\b(prometheus|mimir|metrics?|histogram|counter|gauge|exporter|promql|series)\b",
                                  re.I),
            "osagg": re.compile(r"\b(opensearch|elasticsearch|index|indices|documents?|logs?)\b", re.I)}


def _share_twins(tables: dict[str, Candidate]) -> None:
    """The same table in several databases holds the same data: the clues about what it holds are shared
    (the best of them), so that only the database clues (named, policy, the team's charts, confirmed
    answers) choose among them."""
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for t in tables.values():
        groups.setdefault((t.backend, t.table), []).append(t)
    for twins in groups.values():
        if len(twins) < 2:
            continue
        for k in CONTENT:
            best = max(t.features.get(k, 0.0) for t in twins)
            if best:
                for t in twins:
                    t.features[k] = best
        fields = list(dict.fromkeys(f for t in twins for f in t.fields))
        values = {v: n for t in twins for v, n in t.values.items()}
        for t in twins:
            t.fields, t.values = list(fields), dict(values)


def _family(question: str, tables: dict[str, Candidate]) -> None:
    """ "From the Prometheus metrics", "in the jobs index": the question names the kind of source."""
    said = {backend for backend, rx in FAMILIES.items() if rx.search(question or "")}
    if len(said) != 1:
        return
    for t in tables.values():
        if t.backend in said:
            t.features["family"] = 1.0


def _most_specific(named: dict[int, str], question: str, by_id: dict[int, Any]) -> dict[int, str]:
    """ "the jobs replica" names the database "jobs replica", not also "jobs" (a name inside the other)."""
    if len(named) < 2:
        return named
    low = " ".join((question or "").lower().split())
    names = {i: " ".join((by_id[i].database_name if i in by_id else "").lower().split()) for i in named}
    out = {}
    for i, why in named.items():
        inside = any(j != i and names[i] and names[i] in names[j] and names[j] in low for j in named)
        if not inside:
            out[i] = why
    return out or named


def _recipe_tables(r: dict[str, Any]) -> list[str]:
    from supagent.knowledge.experience import promql_pattern, sql_pattern

    try:
        if r.get("tool") == "promql_query":
            return promql_pattern(r.get("query") or "")[1]
        return sql_pattern(r.get("query") or "")[1]
    except Exception:  # pylint: disable=broad-except
        return []


def _chart_tables(chart_ids: list[int], board_ids: list[int]) -> list[tuple[int, str]]:
    """(database id, table) read by these charts and by the charts of these dashboards."""
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

    from supagent.knowledge.scope import _chart_tables as tables_of

    charts = [db.session.get(Slice, i) for i in chart_ids]
    for b in (db.session.get(Dashboard, i) for i in board_ids):
        charts += list(b.slices or []) if b is not None else []
    out = []
    for c in charts:
        if c is None:
            continue
        try:
            ds, names = tables_of(c)
        except Exception:  # pylint: disable=broad-except
            continue
        if ds is not None:
            out += [(ds.database_id, n) for n in names]
    return out


def _describe_tables(tables: dict[str, Candidate]) -> None:
    """What the dictionary says of each candidate table (one query per database)."""
    from supagent.models import KObject, Source

    per_db: dict[int, list[Candidate]] = {}
    for t in tables.values():
        per_db.setdefault(t.database_id, []).append(t)
    srcs = {dbid: db.session.query(Source).filter(Source.database_id == dbid).first() for dbid in per_db}
    for dbid, cands in sorted(per_db.items(), key=lambda kv: srcs[kv[0]] is None):   # the learned ones first
        src = srcs[dbid]
        if src is None:                                   # not learned: what its twin says
            for c in cands:
                twin = tables.get(f"data:{c.extra.get('twin')}:{c.table}")
                if twin is not None:
                    c.kind, c.about, c.ai = twin.kind, twin.about, twin.ai
                    c.extra.update({k: v for k, v in twin.extra.items() if k not in c.extra})
            continue
        rows = (db.session.query(KObject).filter(KObject.source_id == src.id, KObject.kind.in_(("index", "metric")),
                                                 KObject.name.in_([c.table for c in cands]),
                                                 KObject.gone_at.is_(None)).all())
        objs = {o.name: o for o in rows}
        for c in cands:
            o = objs.get(c.table)
            if o is None:
                continue
            c.kind = o.kind
            c.about = (o.description or o.backend_help or "")[:300]
            c.ai = bool(o.description) and o.description_source == "llm" and not o.verified
            if c.ai:
                c.features["ai_only"] = 1.0
            st = o.stats or {}
            c.extra.update({k: v for k, v in (("metric_type", o.metric_type), ("unit", o.unit),
                                              ("time_field", st.get("time_field")),
                                              ("time_range", st.get("time_range")),
                                              ("labels", st.get("labels"))) if v})


def _team_knowledge(question: str, user_id: int | None, knowledge: dict[str, Candidate]) -> None:
    """The team's rules, the glossary terms of the question, the memory (few, and they change how to count)."""
    try:
        from supagent.knowledge.catalog import rules

        for r in rules():
            key = f"entry:{r['id']}"
            k = knowledge.setdefault(key, Candidate(subject=key, kind="rule", title=r["title"], text=r["text"][:1200]))
            k.kind = "rule"
            k.features["team"] = 1.0
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    try:
        from supagent.knowledge.glossary import found

        for i, t in enumerate(found(question)):
            key = t.get("ref") or f"glossary:{t.get('term')}"
            k = knowledge.setdefault(key, Candidate(subject=key, kind="glossary", title=str(t.get("term") or key),
                                                    text=str(t.get("definition") or "")[:1200]))
            k.kind = "glossary"
            k.features["team"] = 1.0 / (1 + i)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    try:
        from supagent.knowledge.memory import memories_for

        for m in memories_for(user_id):
            key = f"memory:{m.id}"
            k = knowledge.setdefault(key, Candidate(subject=key, kind="memory", title=(m.text or "")[:120],
                                                    text=(m.text or "")[:600]))
            k.kind = "memory"
            k.extra.update(scope=m.scope, memory_kind=m.kind)
            k.features["team"] = 0.5
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()


def _drop_excluded(tables: dict[str, Candidate]) -> None:
    try:
        from supagent.knowledge.excluded import is_excluded

        for key in [k for k, t in tables.items() if is_excluded(t.kind, t.database_id, t.table)]:
            del tables[key]
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()


# --------------------------------------------------------------------------------------------- #
# 2-3. choose
# --------------------------------------------------------------------------------------------- #
CHOOSE_TOOL = {"type": "function", "function": {
    "name": "choose_knowledge",
    "description": "The tables and knowledge items one question needs (chosen from the lists given).",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["data", "action", "explain", "status", "other"],
                 "description": "data: numbers or rows from the data; action: a chart, dashboard, e-mail, export; "
                                "explain: answered by the knowledge items alone; status: is everything normal, what "
                                "is happening; other: none of these"},
        "needs": {"type": "array", "description": "each thing the question asks and the tables that hold it",
                  "items": {"type": "object", "properties": {
                      "what": {"type": "string"},
                      "tables": {"type": "array", "items": {"type": "string"}, "description": "T refs"}},
                      "required": ["what", "tables"]}},
        "knowledge": {"type": "array", "items": {"type": "string"},
                      "description": "K refs that change how to count or answer (rules, definitions, thresholds, "
                                     "formulas, preferences, learned answers, notes)"},
        "ambiguous": {"type": "array", "description": "a need that two tables could hold with different numbers, "
                                                      "that nothing in the question or the knowledge settles",
                      "items": {"type": "object", "properties": {
                          "what": {"type": "string"},
                          "options": {"type": "array", "items": {"type": "string"}},
                          "why": {"type": "string"}}, "required": ["what", "options"]}},
        "missing": {"type": "array", "items": {"type": "string"},
                    "description": "needs that no table or knowledge item holds"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
        "required": ["kind", "needs", "knowledge", "confidence"]}}}

CHOOSE_SYSTEM = (
    "You choose the data and the knowledge one question needs, before anything is read. You do not answer the "
    "question. Choose from the two lists only, by their refs. Tables (T refs) are an index of documents or a metric, "
    "in one database; knowledge items (K refs) are the team's rules, the glossary, the memory, notes and documents, "
    "learned answers and charts. For each thing the question asks (a need), give the tables that hold it: the one "
    "whose fields or labels have the values and measures the question names. The same table in two databases is "
    "one need with the database the question names, else the first one listed. Choose every knowledge item that "
    "changes how to count or what to answer: a rule on a field of a chosen table, a definition or threshold of a "
    "word the question uses, a formula, a preference, a learned answer to the same question. If two different "
    "tables could hold a need and would give different numbers (job failures and HTTP errors, a duration field and a "
    "duration histogram), and nothing in the question or the knowledge settles which one, put the need in "
    "'ambiguous' with both. If nothing in the lists holds a need, put it in 'missing'. Call choose_knowledge once.")


def choose_messages(g: Gathered, previous: str = "") -> list[dict[str, str]]:
    """The question and the best candidates, with the T and K refs the LLM answers with."""
    shown = g.ranked()[:SHOWN_TABLES]
    items = g.items()[:SHOWN_KNOWLEDGE]
    for i, t in enumerate(shown, 1):
        t.ref = f"T{i}"
    for i, k in enumerate(items, 1):
        k.ref = f"K{i}"
    lines = [f"Question: {g.question}"]
    if previous:
        lines.append(f"(The question before, in the same chat: {previous[:500]})")
    lines.append("\nTables (best first):")
    lines += [table_line(t) for t in shown] or ["(none found)"]
    lines.append("\nKnowledge items:")
    lines += [f"{k.ref}: {k.kind} \"{k.title}\": {_one_line(k.text, 260)}" for k in items] or ["(none)"]
    return [{"role": "system", "content": CHOOSE_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def table_line(t: Candidate) -> str:
    what = f'{t.ref}: {t.kind} "{t.table}" in database {t.database_id} "{t.database}" ({t.backend})'
    bits = []
    if t.extra.get("metric_type"):
        bits.append(str(t.extra["metric_type"]))
    if t.extra.get("time_field"):
        bits.append(f'time field {t.extra["time_field"]}')
    if t.about:
        bits.append(_one_line(t.about, 180) + (" (AI-written, unverified)" if t.ai else ""))
    if t.fields:
        bits.append("matching fields/labels: " + ", ".join(t.fields[:8]))
    if t.values:
        bits.append("has the values " + ", ".join(f"{v} ({n})" for v, n in list(t.values.items())[:4]))
    if t.extra.get("twins"):
        bits.append("also in database " + ", ".join(map(str, t.extra["twins"][:4])) +
                    (f" (this one: {t.extra['why_here']})" if t.extra.get("why_here") else ""))
    return what + (": " + "; ".join(bits) if bits else "")


def _one_line(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def parse_choice(args: Any, g: Gathered) -> Selection | None:
    """The LLM's choice with its refs turned into subjects (unknown refs dropped); None if unusable."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            m = re.search(r"\{.*\}", args, re.S)
            try:
                args = json.loads(m.group(0)) if m else None
            except ValueError:
                args = None
    if not isinstance(args, dict):
        return None
    refs = {t.ref: t.subject for t in g.tables.values() if t.ref}
    refs.update({k.ref: k.subject for k in g.knowledge.values() if k.ref})

    def subjects(xs: Any, prefix: str) -> list[str]:
        return [refs[x.strip()] for x in (xs or []) if isinstance(x, str) and x.strip() in refs
                and x.strip().startswith(prefix)]

    needs = []
    for n in args.get("needs") or []:
        if isinstance(n, dict):
            ts = subjects(n.get("tables"), "T")
            needs.append({"what": str(n.get("what") or "")[:200], "tables": ts})
    ambiguous = []
    for a in args.get("ambiguous") or []:
        if isinstance(a, dict):
            opts = subjects(a.get("options"), "T")
            if len(set(opts)) >= 2:
                ambiguous.append({"what": str(a.get("what") or "")[:200], "options": opts,
                                  "why": str(a.get("why") or "")[:300]})
    kind = args.get("kind") if args.get("kind") in ("data", "action", "explain", "status", "other") else "data"
    conf = args.get("confidence") if args.get("confidence") in ("high", "medium", "low") else "medium"
    return Selection(kind=kind, needs=needs, knowledge=subjects(args.get("knowledge"), "K"), ambiguous=ambiguous,
                     missing=[str(x)[:200] for x in (args.get("missing") or []) if isinstance(x, str)][:6],
                     confidence=conf, by="llm")


def gate_selection(g: Gathered) -> Selection:
    """Without the LLM (or when it gave nothing usable): the best table, the tables close to it, the team's
    knowledge and the knowledge found for the question."""
    ranked = g.ranked()
    if not ranked:
        return Selection(kind="data", needs=[], knowledge=[k.subject for k in g.items()[:8]], confidence="low",
                         by="gate", missing=[g.question[:200]])
    best = ranked[0].score
    tables, names = [], set()
    for t in ranked[:6]:                        # one database per table: the best scored one
        if t.score >= 0.8 * best and t.table not in names:
            tables.append(t.subject)
            names.add(t.table)
    return Selection(kind="data", needs=[{"what": g.question[:200], "tables": tables}],
                     knowledge=[k.subject for k in g.items()[:8]], confidence="low", by="gate")


def choose(g: Gathered, llm: Any = None, previous: str = "") -> Selection:
    """One LLM call (choose_knowledge); the gate's choice when the LLM is missing or unusable."""
    if llm is None:
        return gate_selection(g)
    messages = choose_messages(g, previous)
    try:
        from supagent.llm import bounded

        with bounded(llm, CHOOSE_SECONDS):                     # a short choice: no answer in time, the gate's
            msg = llm.chat(messages, tools=[CHOOSE_TOOL])
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent decider: the choice failed", exc_info=True)
        return gate_selection(g)
    args: Any = None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "choose_knowledge":
            args = (tc.get("function") or {}).get("arguments")
            break
    if args is None:
        args = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
    sel = parse_choice(args, g)
    if sel is None or (sel.kind in ("data", "action") and not sel.tables() and not sel.missing and not sel.ambiguous):
        fallback = gate_selection(g)
        if sel is not None:
            fallback.kind = sel.kind
            fallback.knowledge = sel.knowledge or fallback.knowledge
        return fallback
    team = [k.subject for k in g.items() if k.kind in ("rule", "glossary") and k.features.get("team")]
    for s in team:                          # the team's rules and the question's terms always reach the planner
        if s not in sel.knowledge:
            sel.knowledge.append(s)
    return sel


# --------------------------------------------------------------------------------------------- #
# 4. pack
# --------------------------------------------------------------------------------------------- #
def exact_count(value: Any) -> int | None:
    """A learned count as a number; None when unknown or not exact (the learner writes ">=200": at least 200)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    return int(text) if text.isdigit() else None


@dataclass
class TableInfo:
    subject: str
    ref: str
    kind: str                                   # index | metric
    database_id: int
    database: str
    backend: str
    name: str
    about: str = ""
    ai: bool = False
    time_field: str = ""
    time_range: list[str] = field(default_factory=list)
    metric_type: str = ""
    unit: str = ""
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)   # field or label -> type, values, about
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pack:
    tables: list[TableInfo]
    knowledge: list[Candidate]
    ambiguous: list[dict[str, Any]]
    missing: list[str]
    kind: str
    confidence: str

    def table(self, ref_or_name: str) -> TableInfo | None:
        return next((t for t in self.tables if ref_or_name in (t.ref, t.name, t.subject)), None)

    def item(self, ref: str) -> Candidate | None:
        return next((k for k in self.knowledge if ref in (k.ref, k.subject)), None)

    def text(self, limit: int = PACK_CHARS) -> str:
        out = ["Data chosen for this question:"]
        for t in self.tables:
            out.append(_table_text(t))
        if not self.tables:
            out.append("(none)")
        out.append("\nKnowledge chosen (cite it by its ref when a condition or a number comes from it):")
        for k in self.knowledge:
            out.append(f'[{k.ref}] {_kind_name(k)} "{k.title}": {_one_line(k.text, 700)}')
        if not self.knowledge:
            out.append("(none)")
        text = "\n".join(out)
        return text if len(text) <= limit else text[:limit - 30] + "\n… (cut to fit)"


def _kind_name(k: Candidate) -> str:
    return {"rule": "team rule", "glossary": "glossary term", "memory": "memory", "recipe": "learned answer",
            "doc": "document", "context": "Context page", "chart": "Superset chart",
            "dashboard": "Superset dashboard"}.get(k.kind, k.kind)


def _table_text(t: TableInfo) -> str:
    head = f'\n[{t.ref}] {t.kind} "{t.name}" in database {t.database_id} "{t.database}" ({t.backend})'
    if t.time_field:
        head += f', time field "{t.time_field}"'
        if t.time_range:
            head += f" (data from {t.time_range[0]} to {t.time_range[-1]})"
    if t.metric_type:
        head += f", {t.metric_type}" + (f" in {t.unit}" if t.unit else "")
    lines = [head]
    if t.about:
        lines.append("About: " + _one_line(t.about, 400) + (" (AI-written, unverified)" if t.ai else ""))
    if t.metric_type == "counter":
        lines.append("A counter: how many over a period = SUM(increase); per second = SUM(rate); never COUNT.")
    elif t.metric_type == "histogram" or t.name.endswith("_bucket"):
        lines.append("A histogram: quantiles with HISTOGRAM_QUANTILE(q, SUM(RATE(value))); the bucket label le "
                     "counts the observations up to its bound.")
    if t.columns:
        lines.append("Fields:" if t.kind == "index" else "Labels:")
        for name, c in t.columns.items():
            vals = c.get("values") or []
            v = ""
            if vals:
                shown = ", ".join(map(str, vals[:VALUES_SHOWN]))
                count = exact_count(c.get("cardinality"))
                v = f": {shown}" + (f" … ({c.get('cardinality')} values)" if c.get("cardinality") and
                                    (count is None or count > len(vals[:VALUES_SHOWN])) else "")
            elif c.get("range"):
                v = f": {c['range']}"
            about = f" - {_one_line(c['about'], 160)}" + (" (AI-written, unverified)" if c.get("ai") else "") \
                if c.get("about") else ""
            kind = ", ".join(x for x in (c.get("type") or "?", c.get("unit") or "") if x)
            lines.append(f"- {name} ({kind}){v}{about}")
    text = "\n".join(lines)
    return text if len(text) <= TABLE_CHARS else text[:TABLE_CHARS - 30] + "\n… (more fields not shown)"


def pack(g: Gathered, sel: Selection) -> Pack:
    """The chosen tables in full and the chosen knowledge (with the rules on the chosen tables' fields)."""
    subjects = list(dict.fromkeys(sel.tables() + [o for a in sel.ambiguous for o in a["options"]]))
    tables = []
    for i, s in enumerate(subjects, 1):
        c = g.tables.get(s)
        if c is not None:
            tables.append(table_info(c, f"T{i}"))
    items = [g.knowledge[s] for s in sel.knowledge if s in g.knowledge]
    fields = {n.lower() for t in tables for n in t.columns}
    for k in g.knowledge.values():                    # a rule on a field of a chosen table always goes
        if k.kind == "rule" and k not in items and any(re.search(rf"\b{re.escape(f)}\b", k.text.lower())
                                                       for f in fields if len(f) >= 3):
            items.append(k)
    for i, k in enumerate(items, 1):
        k.ref = f"K{i}"
    return Pack(tables=tables, knowledge=items, ambiguous=sel.ambiguous, missing=sel.missing, kind=sel.kind,
                confidence=sel.confidence)


def table_info(c: Candidate, ref: str) -> TableInfo:
    """A table's fields (an index) or labels (a metric) with their types and values, from the dictionary."""
    from supagent.models import KObject, Source

    t = TableInfo(subject=c.subject, ref=ref, kind=c.kind, database_id=c.database_id or 0, database=c.database,
                  backend=c.backend, name=c.table, about=c.about, ai=c.ai,
                  time_field=str(c.extra.get("time_field") or ""), time_range=list(c.extra.get("time_range") or []),
                  metric_type=str(c.extra.get("metric_type") or ""), unit=str(c.extra.get("unit") or ""))
    src = db.session.query(Source).filter(Source.database_id == c.database_id).first()
    if src is None and c.extra.get("twin"):              # not learned: the twin's fields and labels
        src = db.session.query(Source).filter(Source.database_id == c.extra["twin"]).first()
    if src is None:
        return t
    child = "field" if c.kind == "index" else "label"
    rows = (db.session.query(KObject).filter(KObject.source_id == src.id, KObject.kind == child,
                                             KObject.parent == c.table, KObject.gone_at.is_(None))
            .order_by(KObject.name).all())
    matched = {f.lower() for f in c.fields}
    rows.sort(key=lambda o: (o.name.lower() not in matched, o.name.lower() != t.time_field.lower(), o.name))
    for o in rows:
        st = o.stats or {}
        col: dict[str, Any] = {"type": o.data_type or "", "unit": o.unit or "",
                               "values": list(st.get("values") or [])[:40], "cardinality": st.get("cardinality"),
                               "about": (o.description or o.backend_help or "")[:300],
                               "ai": bool(o.description) and o.description_source == "llm" and not o.verified}
        if st.get("min") is not None and st.get("max") is not None:
            col["range"] = f"{st.get('min')} .. {st.get('max')}"
        t.columns[o.name] = col
    return t


# --------------------------------------------------------------------------------------------- #
# the whole decision
# --------------------------------------------------------------------------------------------- #
def decide(question: str, previous: str = "", user_id: int | None = None, llm: Any = None,
           record: bool = True, route_id: int | None = None) -> Decision:
    g = gather(question, previous, user_id)
    sel = choose(g, llm, previous)
    p = pack(g, sel)
    if record:
        shown = [{"subject": t.subject, "features": t.features, "score": t.score} for t in g.ranked()[:SHOWN_TABLES]]
        route_id = gate.record(question, g.terms, shown, sel.tables(), user_id, route_id=route_id)
    return Decision(gathered=g, selection=sel, pack=p, route_id=route_id)
