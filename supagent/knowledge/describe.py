"""The data dictionary as the agent reads it (tool describe_data) and as the knowledge API
serves it: what was learned and curated, limited to the databases the user may query."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from superset import db

from supagent.knowledge.catalog import load_catalog
from supagent.knowledge.store import words
from supagent.models import Change, KObject, Relation, Run, Source

MAX_CHARS = 16000
SECTION_CHARS = 7000
MAX_METRICS = 12
MAX_FIELDS = 40
METRICS_SQL = (
    "  SQL: one table per metric; columns ts (sample time; in GROUP BY use DATE_TRUNC('hour', ts)), one column "
    "per label, value (sample), and for counters rate (per second) / increase (count) per series and time "
    "bucket: SUM(rate) GROUP BY node = sum by (node) (rate(...)). Functions: RATE(value), INCREASE(value), "
    "AVG_OVER_TIME(value), MAX_OVER_TIME(value), QUANTILE_OVER_TIME(0.95, value), on *_bucket tables "
    "HISTOGRAM_QUANTILE(0.95, SUM(RATE(value))); FILTER (WHERE label = 'x'). Always filter ts on a time range. "
    "Series with samples in a window: GROUP BY label with COUNT(*) (SELECT DISTINCT label reads the label "
    "index, like a Grafana variable, and may list series that stopped earlier).")


def marker(obj: KObject) -> str:
    from supagent.knowledge.excluded import says_not_used

    if says_not_used(obj):
        return " (DO NOT USE: the team's description)"
    if obj.description_source == "llm" and not obj.verified:
        return " (AI-written, unverified)"
    return ""


def _num(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.4g}" if isinstance(v, float) else f"{v:,}"
    return str(v)


def _dataset(table: str, database_id: int) -> Any:
    from superset.connectors.sqla.models import SqlaTable

    return db.session.query(SqlaTable).filter_by(table_name=table, database_id=database_id).first()


STOP = set("""a an and are as at be by did do does for from had has have how in is it its many
much of on or per that the their them there these this those to was were what when where which
who why will with yesterday today last week day days hour hours show give list me my our all
au aux avec ce ces dans de des du en est et la le les leur mes nos ou par pas pour quel quelle
quels quelles qui quoi sur un une hier aujourd""".split())


DERIVATIONAL = ("ation", "ment", "ing", "ure", "ed")   # failed / failure / failing -> fail
MIN_STEM = 4                    # never shorter: "rated" stays (a prefix of "rate"), "duration" too


def _plural(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "is", "ous", "tus", "bus", "sus", "rus")):
        return w[:-1]
    return w


def stem(w: str) -> str:
    """servers -> server, queries -> query, processes -> process; failed, failure, failures,
    failing -> fail; alerting -> alert; throttled, throttling -> throttl (the word someone types
    and the word in a name meet; not linguistic roots: a stem is never shorter than MIN_STEM)."""
    w = _plural(w)
    for suffix in DERIVATIONAL:
        if w.endswith(suffix) and len(w) - len(suffix) >= MIN_STEM:
            return w[:-len(suffix)]
    return w


def topic_words(topic: str | None) -> set[str]:
    return {stem(w) for w in words((topic or "").replace("_", " ")) if w not in STOP}


def _bag(*texts: Any) -> set[str]:
    bag: set[str] = set()
    for t in texts:
        if isinstance(t, (list, tuple, set)):
            t = " ".join(map(str, t))
        bag |= words(str(t or "").replace("_", " ")) | words(str(t or ""))
    return {stem(w) for w in bag}


def _score(ws: set[str], *texts: Any, weights: dict[str, float] | None = None) -> float:
    """How well these texts match the topic words (rare words count more)."""
    if not ws:
        return 1
    hit = ws & _bag(*texts)
    return sum((weights or {}).get(w, 1.0) for w in hit)


def _weights(ws: set[str], bags: list[set[str]]) -> dict[str, float]:
    """A topic word found in many objects says little about any of them."""
    out = {}
    for w in ws:
        n = sum(1 for b in bags if w in b)
        out[w] = 1.0 if n <= 3 else 0.5 if n <= 10 else 0.2
    return out


def _relation_text(rel: Relation, a: KObject, b: KObject) -> str:
    def end(o: KObject) -> str:
        if o.kind == "field":
            return f'{o.parent}."{o.name}"'
        if o.kind == "label":
            return f"metric label {o.name}"
        return f"{o.kind} {o.name}"

    ev = rel.evidence or {}
    if rel.origin == "curated":
        why = ev.get("description") or "written in the catalog"
    else:
        why = (f"measured: {ev.get('common')} values in common, {round(100 * (ev.get('coverage') or 0))}% of the "
               f"smaller side; e.g. {', '.join(map(str, (ev.get('examples') or [])[:3]))}")
    return f"{end(a)} = {end(b)} ({why})"


def _field_line(f: KObject) -> str:
    st = f.stats or {}
    line = f'  - "{f.name}" ({f.data_type or "?"}{", " + f.unit if f.unit else ""})'
    if f.description:
        line += f": {f.description.strip()}{marker(f)}"
    if f.synonyms:
        line += f" [also: {', '.join(map(str, f.synonyms))}]"
    if st.get("values"):
        vals = st["values"]
        line += f" values: {', '.join(map(str, vals[:30]))}" + (f" ... ({len(vals)})" if len(vals) > 30 else "")
    elif st.get("cardinality") is not None:
        line += f" {_num(st['cardinality'])} distinct values"
    if st.get("min") is not None and "values" not in st:
        line += f" range {_num(st.get('min'))} .. {_num(st.get('max'))}"
        if st.get("avg") is not None:
            line += f", avg {_num(st.get('avg'))}"
    if st.get("filled_pct") is not None and st["filled_pct"] < 99.5:
        line += f" (filled in {st['filled_pct']}% of documents)"
    if st.get("computed"):
        line += f" ({st['computed']})"
    return line


def _indices(src: Source, database: Any, ws: set[str], only: str | None,
             allowed: set[int]) -> list[tuple[float, list[str]]]:
    """One section per index, with its relevance to the topic."""
    q = db.session.query(KObject).filter(KObject.source_id == src.id, KObject.gone_at.is_(None))
    indices = [o for o in q.filter(KObject.kind == "index") if not only or o.name == only]
    if not indices:
        return []
    fields: dict[str, list[KObject]] = defaultdict(list)
    for f in q.filter(KObject.kind == "field", KObject.parent.in_([i.name for i in indices])):
        fields[f.parent].append(f)
    bags = {f.id: _bag(f.name, f.description, f.synonyms, (f.stats or {}).get("values"))
            for fs in fields.values() for f in fs}
    weights = _weights(ws, list(bags.values()))
    scored = []
    for ix in indices:
        fs = fields.get(ix.name, [])
        fscores = {f.id: (sum(weights.get(w, 1.0) for w in ws & bags[f.id]) if ws else 1) for f in fs}
        head = _score(ws, ix.name, ix.description, ix.category, weights=weights)
        scored.append((head + max(fscores.values(), default=0), ix, fscores))
    scored.sort(key=lambda x: (-x[0], x[1].name))
    chosen = [(sc, ix, fsc) for sc, ix, fsc in scored if sc > 0] if ws else scored
    sections: list[tuple[float, list[str]]] = []
    shown: set[tuple[str, str]] = set()          # index pairs whose relations are already listed
    for sc, ix, fscores in chosen[:6]:
        st = ix.stats or {}
        ds = _dataset(ix.name, database.id)
        out = [f"\nIndex {ix.name}: {(ix.description or '').strip()}{marker(ix)}",
               f'  SQL: FROM "{ix.name}" on database id {database.id} "{database.database_name}"'
               + (f"; Superset dataset id {ds.id} (charts: dataset_id={ds.id})" if ds else "")]
        if st.get("docs") is not None:
            rng = st.get("time_range") or [None, None]
            span = f" from {rng[0]} to {rng[1]}" if rng[0] and rng[1] else ""
            out.append(f"  {_num(st['docs'])} documents" + (f"; time field {st.get('time_field')}{span}"
                                                            if st.get("time_field") else "")
                       + (f" ({st['timezone']} time, as SQL shows it)" if st.get("timezone") and span else ""))
        fs = fields.get(ix.name, [])
        try:
            from supagent.knowledge.experience import timing_hints

            for hint in timing_hints([ix.name.rstrip("*")], database.id, limit=1):
                out.append(f"  queries: {hint}")
        except Exception:  # pylint: disable=broad-except
            pass
        out += _join_lines(ix.name, _relations([f.id for f in fs], allowed), ws, shown)
        rows = sorted(fs, key=lambda f: (-fscores[f.id], f.name))
        if ws and not only:
            keep = [f for f in rows if fscores[f.id] > 0][:15]
        else:
            keep = rows[:2 * MAX_FIELDS]
        tf = st.get("time_field")
        if tf and not any(f.name == tf for f in keep):
            keep += [f for f in fs if f.name == tf]
        out.append("  fields:" if keep else "  fields: (none matches the topic; call describe_data without topic)")
        out += [_field_line(f) for f in keep]
        rest = sorted(f.name for f in fs if f not in keep)
        if rest:
            out.append("  other fields: " + ", ".join(f'"{n}"' for n in rest)[:1500])
        if ds is not None and ds.metrics:
            out.append("  saved metrics (use {\"name\": <metric>, \"saved_metric\": true} in charts):")
            out += [f"  - {m.metric_name}: {m.description or m.expression}" for m in ds.metrics]
        sections.append((sc, out))
    others = [ix.name for _s, ix, _f in scored if ix not in [c[1] for c in chosen[:6]]]
    if others:
        sections.append((0, [f"  other indices of database {database.id}: {', '.join(others)[:1200]}"]))
    return sections


def _join_lines(index: str, rels: list[tuple[Relation, KObject, KObject]], ws: set[str],
                shown: set[tuple[str, str]], per_pair: int = 8) -> list[str]:
    """Relations of an index's fields, one line per other index: curated ones with their text,
    measured ones as the fields that hold the same values (the join keys)."""
    out: list[str] = []
    measured: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for rel, a, b in rels:
        mine, other = (a, b) if a.parent == index and a.kind == "field" else (b, a)
        if other.kind == "field" and other.parent == index:
            continue
        where = other.parent if other.kind == "field" else f"metric label {other.name}"
        pair = tuple(sorted((index, where)))
        if pair in shown:
            continue
        if rel.origin == "curated":
            out.append(f"  relationship: {_relation_text(rel, a, b)}")
            continue
        ev = rel.evidence or {}
        if other.kind == "field":
            on = f'"{mine.name}"' if mine.name == other.name else f'"{mine.name}" = {other.parent}."{other.name}"'
        else:
            where = "metric labels"
            on = f'"{mine.name}" = label {other.name}'
        rank = (2 if ws & words(mine.name.replace("_", " ")) else 0) + (rel.confidence or 0)
        measured[where].append((rank, f"{on} ({ev.get('common')} values in common)"))
    for where, items in sorted(measured.items(), key=lambda kv: -max(r for r, _t in kv[1])):
        items.sort(key=lambda x: -x[0])
        more = f" and {len(items) - per_pair} more" if len(items) > per_pair else ""
        if where == "metric labels":
            out.append("  same values as metric labels (join a metric on them): "
                       + ", ".join(t for _r, t in items[:per_pair]) + more + " (measured)")
        else:
            out.append(f"  joins {where} on " + ", ".join(t for _r, t in items[:per_pair]) + more + " (measured)")
    for rel, a, b in rels:
        for o in (a, b):
            if o.kind == "field" and o.parent != index:
                shown.add(tuple(sorted((index, o.parent))))
    return out


def _relations(ids: list[int], allowed: set[int],
               kinds: tuple[str, ...] = ("same_values", "curated")) -> list[tuple[Relation, KObject, KObject]]:
    """Relations of these objects whose two ends are in databases the user may query."""
    if not ids:
        return []
    rels = (db.session.query(Relation)
            .filter((Relation.a_id.in_(ids)) | (Relation.b_id.in_(ids)))
            .filter(Relation.relation.in_(kinds), Relation.rejected_at.is_(None)).all())
    ends = {r.a_id for r in rels} | {r.b_id for r in rels}
    objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ends))} if ends else {}
    out, seen = [], set()
    for r in sorted(rels, key=lambda r: (r.origin != "curated", -(r.confidence or 0))):
        a, b = objs.get(r.a_id), objs.get(r.b_id)
        if a is None or b is None or a.gone_at or b.gone_at:
            continue
        if a.source_id not in allowed or b.source_id not in allowed:
            continue                      # never show what lies in a database the user may not query
        key = tuple(sorted([(a.kind, a.parent if a.kind == "field" else "", a.name),
                            (b.kind, b.parent if b.kind == "field" else "", b.name)]))
        if key in seen:
            continue
        seen.add(key)
        out.append((r, a, b))
    return out


def _metric_lines(m: KObject, labels: list[KObject], tables: dict[str, Any], ds: Any, family: str | None,
                  related: bool, src: Source) -> list[str]:
    st = m.stats or {}
    t = tables.get(m.name) or {}
    head = f"  - {m.name} ({m.metric_type or 'unknown'}{', ' + m.unit if m.unit else ''})"
    head += f": {(m.description or '').strip()}{marker(m)}" if m.description else ""
    if ds is not None:
        head += f" [dataset id {ds.id}]"
    out = [head]
    facts = []
    if st.get("series") is not None:
        facts.append(f"{_num(st['series'])} series")
    if st.get("data_from"):
        facts.append(f"data from about {st['data_from']} to {st.get('data_to')}")
    if st.get("rate_total") is not None:
        facts.append(f"rate over {st.get('window')}: {_num(st['rate_total'])}/s in total, "
                     f"{_num(st.get('rate_max_series'))}/s for the busiest series")
    if st.get("min") is not None:
        facts.append(f"values over {st.get('window')}: {_num(st['min'])} .. {_num(st.get('max'))}, "
                     f"avg {_num(st.get('avg'))}")
    if st.get("p50") is not None or st.get("p95") is not None:
        facts.append(f"p50 {_num(st.get('p50'))}, p95 {_num(st.get('p95'))} over {st.get('window')}")
    if st.get("note"):
        facts.append(st["note"])
    if facts:
        out.append("      " + "; ".join(facts))
    parts = []
    for lb in sorted(labels, key=lambda o: o.name):
        ls = lb.stats or {}
        p = lb.name
        if lb.description:
            p += f" ({lb.description.strip()}{marker(lb)})"
        vals = ls.get("values") or ls.get("sample") or []
        if vals:
            p += ": " + ", ".join(map(str, vals[:12])) + (" ..." if len(vals) > 12 or ls.get("sample") else "")
        parts.append(p)
    if parts:
        out.append("      labels: " + "; ".join(parts))
    if family:
        out.append(f"      part of {family}")
    try:
        from supagent.knowledge.experience import timing_hints

        for hint in timing_hints([m.name], src.database_id, limit=1):
            out.append(f"      queries: {hint}")
    except Exception:  # pylint: disable=broad-except
        pass
    for q in t.get("sql") or []:
        out.append(f"      e.g. {q}")
    if ds is not None and ds.metrics:
        out.append("      saved metrics: " + "; ".join(f"{x.metric_name} = {x.expression}" for x in ds.metrics))
    if related:
        for lb in labels:
            if lb.name in ("le", "quantile", "__tenant_id__"):
                continue
            others = [n for (n,) in db.session.query(KObject.parent).filter(
                KObject.source_id == src.id, KObject.kind == "label", KObject.name == lb.name,
                KObject.parent != m.name, KObject.gone_at.is_(None)).limit(400)]
            if others:
                out.append(f"      label {lb.name} is also on {len(others)} other metric(s): "
                           + ", ".join(sorted(others)[:8]) + (" ..." if len(others) > 8 else ""))
    return out


def _metrics(src: Source, database: Any, ws: set[str], only: str | None, cat: dict[str, Any],
             allowed: set[int]) -> tuple[float, list[str]]:
    """The section of one metrics database, with its relevance to the topic."""
    spec = cat.get("metrics") or {}
    tables = spec.get("tables") or {}
    q = db.session.query(KObject).filter(KObject.source_id == src.id, KObject.gone_at.is_(None))
    metrics = q.filter(KObject.kind == "metric").all()
    if not metrics:
        return 0, []
    by_name = {m.name: m for m in metrics}
    best = 1.0
    if only:
        if only not in by_name:
            return 0, []
        chosen = [by_name[only]]
    else:
        label_names: dict[str, list[str]] = defaultdict(list)
        for parent, lname in db.session.query(KObject.parent, KObject.name).filter(
                KObject.source_id == src.id, KObject.kind == "label", KObject.gone_at.is_(None)):
            label_names[parent].append(lname)
        bags = {m.id: _bag(m.name, m.description, m.synonyms, m.category,
                           (tables.get(m.name) or {}).get("description"), label_names.get(m.name)) for m in metrics}
        weights = _weights(ws, list(bags.values()))
        scored = [((sum(weights.get(w, 1.0) for w in ws & bags[m.id]) if ws else 1), m) for m in metrics]
        if ws:
            ranked = [(sc, m) for sc, m in sorted(scored, key=lambda x: (-x[0], x[1].name)) if sc > 0]
            chosen = [m for _sc, m in ranked][:MAX_METRICS]
            best = ranked[0][0] if ranked else 0
        else:
            chosen = sorted(metrics, key=lambda m: (m.description_source != "curated", m.name))[:MAX_METRICS]
    desc = (spec.get("description") or "").strip() if str(spec.get("database") or "") in (
        "", database.database_name, str(database.id)) else ""
    out = [f"\nMetrics (Prometheus / Mimir) on database id {database.id} \"{database.database_name}\": {desc}",
           METRICS_SQL]
    checks = cat.get("checks") or {}
    if checks:
        out.append(f"  health checks (check_health): {', '.join(checks)}")
    tenants = set()
    for lb in q.filter(KObject.kind == "label", KObject.name == "__tenant_id__").limit(50):
        tenants |= set((lb.stats or {}).get("values") or [])
    if len(tenants) > 1:
        out.append(f"  tenants: {', '.join(sorted(tenants)[:40])} (Mimir tenant federation; each tenant is usually a "
                   "different application or subject): every series has the label __tenant_id__; keep it in GROUP "
                   "BY (__tenant_id__, node) so that the same name in two tenants stays apart, filter with WHERE "
                   "__tenant_id__ = '...' for one of them, never add tenants up unless asked.")
    label_ids = [i for (i,) in db.session.query(KObject.id).filter(
        KObject.source_id == src.id, KObject.kind == "label", KObject.gone_at.is_(None)).limit(20000)]
    measured = []
    for rel, a, b in _relations(label_ids, allowed):
        if a.kind != "field" and b.kind != "field":
            continue
        if rel.origin == "curated":
            out.append(f"  relationship: {_relation_text(rel, a, b)}")
        else:
            lb, f = (a, b) if a.kind == "label" else (b, a)
            measured.append(f'{lb.name} = {f.parent}."{f.name}" ({(rel.evidence or {}).get("common")} values in common)')
    if measured:
        out.append("  labels with the same values as index fields (join jobs and metrics on them): "
                   + ", ".join(measured[:12]) + (f" and {len(measured) - 12} more" if len(measured) > 12 else "")
                   + " (measured)")
    labels: dict[str, list[KObject]] = defaultdict(list)
    for lb in q.filter(KObject.kind == "label", KObject.parent.in_([m.name for m in chosen])):
        labels[lb.parent].append(lb)
    families: dict[int, str] = {}
    ids = [m.id for m in chosen]
    fam_rels = (db.session.query(Relation).filter(Relation.relation == "family_part",
                                                  (Relation.a_id.in_(ids)) | (Relation.b_id.in_(ids))).all()
                if chosen else [])
    ends = {r.a_id for r in fam_rels} | {r.b_id for r in fam_rels}
    fam_objs = {o.id: o for o in db.session.query(KObject).filter(KObject.id.in_(ends))} if ends else {}
    for r in fam_rels:                      # relate() keeps the smaller id first: either end may be the family
        a, b = fam_objs.get(r.a_id), fam_objs.get(r.b_id)
        if a is None or b is None:
            continue
        fam, part = (a, b) if a.kind == "family" else (b, a)
        families[part.id] = f"{fam.metric_type or 'metric'} family {fam.name}: " + ", ".join(
            (fam.stats or {}).get("parts") or [])
    for m in chosen:
        out += _metric_lines(m, labels.get(m.name, []), tables, _dataset(m.name, database.id), families.get(m.id),
                             related=bool(only), src=src)
    if not only and len(chosen) < len(metrics):
        rest = sorted(n for n in by_name if n not in {m.name for m in chosen})
        out.append(f"  other metrics ({len(rest)}): " + ", ".join(rest)[:1500])
    return best, out


def describe(topic: str | None = None, name: str | None = None) -> str | None:
    """The dictionary text for the current user; None when nothing was learned for the
    databases the user may query (the caller then falls back on live reads)."""
    from superset.models.core import Database

    from supagent.knowledge.curated import sources_of_user

    sources = []
    visible = sources_of_user()
    allowed = {s.id for s in visible}
    for src in visible:
        if db.session.query(KObject.id).filter_by(source_id=src.id).first() is not None:
            sources.append((src, db.session.get(Database, src.database_id)))
    if not sources:
        return None
    out: list[str] = []
    if name:
        known = db.session.query(KObject.id).filter(
            KObject.source_id.in_([s.id for s, _d in sources]), KObject.kind.in_(("index", "metric")),
            KObject.name == name, KObject.gone_at.is_(None)).first()
        if known is None:
            out.append(f"(there is no index or metric named {name!r}; below: what matches the topic)")
            topic = f"{topic or ''} {name.replace('_', ' ')}"
            name = None
    ws = topic_words(topic)
    cat = load_catalog()
    glossary = cat.get("glossary") or {}
    if glossary and not name:
        out.append("Business terms:")
        out += [f"- {k}: {v}" for k, v in glossary.items()]
    sections: list[tuple[float, list[str]]] = []
    for src, database in sources:
        if src.backend == "osagg":
            sections += _indices(src, database, ws, name, allowed)
        elif src.backend == "promagg":
            sc, lines = _metrics(src, database, ws, name, cat, allowed)
            if lines:
                sections.append((sc, lines))
    try:
        from supagent.knowledge.catalog import guides

        scored_guides = sorted(((_score(ws, n["title"], n["category"], n["text"]), n) for n in guides()),
                               key=lambda x: -x[0])
    except Exception:  # pylint: disable=broad-except
        scored_guides = []
    for sc, n in scored_guides[:2]:
        if ws and sc > 0:
            sections.append((sc + 0.5, [f"\nGuide \u201c{n['title']}\u201d ({n['category'] or 'catalog'}):",
                                        n["text"][:1500]]))
    try:
        from supagent.knowledge.catalog import formulas
        from supagent.security import visible_databases

        dbs = visible_databases()
        found = [f for f in formulas() if f["database_id"] is None or f["database_id"] in dbs]
    except Exception:  # pylint: disable=broad-except
        found = []
    scored_f = sorted(((_score(ws, f["title"], f["text"]), f) for f in found), key=lambda x: -x[0])
    lines = [f"- {' '.join(f['text'].split())[:400]}" for sc, f in scored_f[:8]
             if (ws and sc > 0) or (name and name in f["text"])]
    if lines:
        sections.append((scored_f[0][0] + 0.4, ["\nFormulas of the team (catalog; use them as written):"] + lines))
    budget = MAX_CHARS - sum(len(x) + 1 for x in out) - 300
    for _sc, lines in sorted(sections, key=lambda x: -x[0]):   # the most relevant first
        text = "\n".join(lines)
        if len(text) > SECTION_CHARS:
            text = text[:SECTION_CHARS].rsplit("\n", 1)[0] + "\n  ... (more: describe_data(index=<name>))"
        if budget - len(text) < 0:
            if budget > 400:
                out.append(text[:budget].rsplit("\n", 1)[0] + "\n  ... (cut: ask with a narrower topic)")
            break
        out.append(text)
        budget -= len(text) + 1
    if topic and not name:                     # metrics the dictionary does not have yet: the live list
        try:
            from supagent.knowledge.resolve import where_block

            live = [ln for ln in where_block(topic).splitlines()[2:] if ln.startswith("- metric ")]
            shown = "\n".join(x for x in out if x)
            live = [ln for ln in live if ln.split('"')[1] not in shown]
            if live:
                out.append("\nMatching metrics by name (live list; the dictionary does not describe them yet):")
                out += live[:4]
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
    learned = [s.last_learned_at for s, _d in sources if s.last_learned_at]
    if learned:
        out.append(f"\n(learned {max(learned):%Y-%m-%d %H:%M} UTC; describe_data(index=<name>) gives one index or "
                   "metric with its relations; data_changes lists what changed)")
    return "\n".join(x for x in out if x is not None)[:MAX_CHARS]


def changes(days: int = 7, limit: int = 200, offset: int = 0, total: dict[str, int] | None = None
            ) -> list[dict[str, Any]]:
    """What the learning runs found different in the last days, for the user's databases, newest
    first (`offset` for the next pages; `total` receives how many there are in all)."""
    from supagent.knowledge.curated import sources_of_user

    ids = {s.id: s for s in sources_of_user()}
    if not ids:
        return []
    since = dt.datetime.utcnow() - dt.timedelta(days=max(1, int(days)))
    q = (db.session.query(Change, KObject, Run)
         .join(KObject, KObject.id == Change.object_id)
         .join(Run, Run.id == Change.run_id)
         .filter(Change.at >= since, KObject.source_id.in_(list(ids))))
    if total is not None:
        total["total"] = q.count()
    rows = q.order_by(Change.id.desc()).offset(max(0, offset)).limit(limit).all()
    return [{"at": c.at.strftime("%Y-%m-%d %H:%M"), "change": c.change, "kind": o.kind,
             "name": o.name, "parent": o.parent or None, "database": ids[o.source_id].database_name,
             "detail": c.detail or {}, "run": r.id} for c, o, r in rows]
