"""What the agent knows of the system, and what is missing for an investigation (0.9).

An investigation follows what the team gave: the values of the categories (the parts of the system), what each
is part of, the interactions drawn between them, where each category is in the data, how the tables join, which
field holds the usual value of a measure, the health checks, the investigation paths the team validated. This
report counts each of these and lists, in words, what is not there yet: the admin's to-do list while preparing
the categories, the System map and the catalog. Nothing is called and nothing is changed; no LLM.

    superset supagent check-system [--question "..."] [--json]      and the System map page (admins)
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from superset import db

log = logging.getLogger(__name__)
NAMED = 6                 # names given in a line of the to-do list
TABLES = 40               # tables of the catalog looked at
_LEARNED: dict[str, Any] = {}       # {knowledge stamp: (the learned tables, their fields named like a usual value)}
_LOCK = threading.Lock()
NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+\b|\b[A-Z]{3,}[0-9]*\b")       # written as a name: ABC, AB_CD


def _some(items: list[str], n: int = NAMED) -> str:
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _categories(g: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    from supagent.knowledge.brief import _fields_of

    V, kids = g["values"], g["children"]
    by_cat: dict[str, list[int]] = {}
    for i, v in V.items():
        by_cat.setdefault(v["cat"], []).append(i)
    names = {c: [V[i]["name"] for i in ids] for c, ids in by_cat.items()}
    try:
        places = _fields_of(list(by_cat), names)
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent readiness: the data places: not read", exc_info=True)
        db.session.rollback()
        places = {}
    rows, todo = [], []
    for cat in g["rank"]:
        ids = by_cat.get(cat, [])
        linked = [i for i in ids if g["out"].get(i) or g["in"].get(i)]
        alone = [i for i in ids if i not in linked and not V[i]["parents"] and not kids.get(i)]
        rows.append({"category": cat, "values": len(ids), "described": sum(1 for i in ids if V[i]["about"]),
                     "part_of_or_parts": sum(1 for i in ids if V[i]["parents"] or kids.get(i)),
                     "with_interactions": len(linked), "in_the_data": places.get(cat, [])})
        if not linked:                            # a category of topics, or one the map does not use yet
            continue
        if not places.get(cat):
            todo.append(f'category "{cat}": its values interact on the System map, and none is found in a field or '
                        f'a metric label the agent may read (Settings, categories.fields, e.g. "{cat}": '
                        '"^(FIELD_NAME|label_name)$"): the agent knows these parts by name but not where to look for '
                        "them in the data")
        if alone:                                 # some are on the map: the others were probably forgotten
            todo.append(f'category "{cat}": {len(alone)} of {len(ids)} value(s) have no interaction and are part '
                        f'of nothing ({_some(sorted(V[i]["name"] for i in alone))}): an investigation cannot '
                        "follow them (System map: what they depend on, run on, read, call)")
    return rows, todo


def _interactions() -> tuple[dict[str, Any], list[str]]:
    from sqlalchemy import func

    from supagent.models import Link

    counts: dict[str, dict[str, int]] = {}
    q = (db.session.query(Link.status, Link.kind, func.count(Link.id))
         .filter(Link.a_ref.like("facet:%"), Link.b_ref.like("facet:%")).group_by(Link.status, Link.kind))
    for status, kind, n in q:
        counts.setdefault(status or "", {})[kind or ""] = int(n)
    out = {"approved": counts.get("approved", {}), "proposed": sum(counts.get("proposed", {}).values())}
    todo = []
    if not out["approved"]:
        todo.append("no interaction is drawn between the parts of the system (System map): an investigation "
                    "cannot go from a late or slow part to what it depends on, runs on, reads or calls")
    if out["proposed"]:
        todo.append(f'{out["proposed"]} interaction(s) proposed from the documents wait in Data dictionary, To '
                    "review: the map draws, and investigations follow, the approved ones only")
    return out, todo


def _tables() -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    from supagent.knowledge.brief import USUAL_NAME
    from supagent.models import KObject
    from supagent.tools import _catalog

    from supagent.knowledge.freshness import stamp

    cat = _catalog()
    indices = cat.get("indices") or {}
    key = stamp()
    with _LOCK:
        known = _LEARNED.get(key)
    if known is None:                             # every field of the dictionary is looked at: once per state of it
        learned: dict[str, dict[str, Any]] = {}
        fields: dict[str, list[str]] = {}
        try:
            for o in db.session.query(KObject).filter(KObject.kind == "index", KObject.gone_at.is_(None)):
                learned[o.name] = {"time_field": (o.stats or {}).get("time_field")}
            for parent, name in db.session.query(KObject.parent, KObject.name).filter(
                    KObject.kind == "field", KObject.gone_at.is_(None)):
                if USUAL_NAME.match(name or ""):
                    fields.setdefault(parent or "", []).append(name)
            with _LOCK:
                _LEARNED.clear()
                _LEARNED[key] = (learned, fields)
        except Exception:  # pylint: disable=broad-except
            log.warning("supagent readiness: the learned tables: not read", exc_info=True)
            db.session.rollback()
    else:
        learned, fields = known
    rows, todo = [], []
    for t in sorted(set(indices) | set(fields))[:TABLES]:
        spec = indices.get(t) or {}
        fs = {n: f for n, f in (spec.get("fields") or {}).items() if isinstance(f, dict)}
        usual = {n: f["usual_of"] for n, f in fs.items() if f.get("usual_of")}
        guessed = sorted({n for n in list(fs) + fields.get(t, []) if USUAL_NAME.match(n) and n not in usual})
        time_field = spec.get("time_field") or (learned.get(t) or {}).get("time_field") or \
            next(((s or {}).get("time_field") for n, s in learned.items()
                  if t.endswith("*") and n.startswith(t.rstrip("*")) and (s or {}).get("time_field")), None)
        joins = len(spec.get("relationships") or []) + sum(
            1 for other in indices.values() for rel in (other or {}).get("relationships") or [] if rel.get("to") == t)
        rows.append({"table": t, "in_the_catalog": t in indices, "time_field": time_field or None,
                     "joins": joins, "usual_values": usual, "usual_values_not_said": guessed})
        if not time_field:
            todo.append(f'table {t}: its time field is not known (catalog, index entry: time_field): a comparison '
                        "with the usual days needs it")
        if guessed:
            todo.append(f'table {t}: {_some([chr(34) + n + chr(34) for n in guessed])} look(s) like the usual value '
                        "of a measure: say of which field in the catalog (index entry, fields: <this field>: "
                        "usual_of: <the measured field>), and the agent compares the two")
    joined = {r["table"] for r in rows if r["joins"]}
    if len(indices) >= 2 and not joined:
        todo.append("the catalog says of no table how it joins another (index entry: relationships): a finding in "
                    "one table (a slow job) cannot be followed into another (its steps, the feeds, the changes)")
    rels = (cat.get("metrics") or {}).get("label_relationships") or []
    checks = list(cat.get("checks") or {})
    if not checks:
        todo.append("no health check in the catalog (checks): check_health has nothing to run at the start of an "
                    "investigation")
    return rows, {"label_relationships": len(rels), "checks": len(checks),
                  "tables_learned": len(learned), "tables_in_the_catalog": len(indices)}, todo


def _paths() -> tuple[dict[str, int], list[str]]:
    from sqlalchemy import func

    from supagent.knowledge.paths import TOOL
    from supagent.models import Recipe

    counts = dict(db.session.query(Recipe.status, func.count(Recipe.id)).filter(Recipe.tool == TOOL)
                  .group_by(Recipe.status))
    out = {"confirmed": int(counts.get("confirmed", 0)), "waiting": int(counts.get("helpful", 0))}
    todo = []
    if out["waiting"]:
        todo.append(f'{out["waiting"]} investigation path(s) wait in Data dictionary, To review: once confirmed, '
                    "the next investigations of the same kind start from them")
    return out, todo


def of_question(question: str, g: dict[str, Any]) -> dict[str, Any]:
    """What the agent is given for this question, and the names it writes that the knowledge does not have."""
    from supagent.knowledge.brief import brief_block, named, says_something
    from supagent.models import KObject

    V = g["values"]
    every = named(question, g)
    ids = [i for i in every if says_something(i, g)]
    silent = [i for i in every if i not in ids]
    known = {V[i]["name"].lower() for i in every} | {n for i in every for n, xs in g["names"].items() if i in xs}
    words = list(dict.fromkeys(m.group(0) for m in NAME.finditer(question or "")))
    words = [w for w in words if w.lower() not in known and not any(w.lower() in k.split() for k in known)]
    unknown = []
    if words:
        from sqlalchemy import func

        have = {n.lower() for (n,) in db.session.query(KObject.name).filter(
            KObject.gone_at.is_(None), func.lower(KObject.name).in_([w.lower() for w in words]))}
        try:
            from supagent.knowledge.resolve import _databases, value_rows

            have |= {w.lower() for w, places in value_rows(words, list(_databases())).items() if places}
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
        unknown = [w for w in words if w.lower() not in have]
    return {"names": [f'{V[i]["name"]} ({V[i]["cat"]})' for i in ids],
            "says_nothing": [f'{V[i]["name"]} ({V[i]["cat"]})' for i in silent], "not_known": unknown,
            "given": brief_block(question, full=True).strip()}


def report(question: str | None = None) -> dict[str, Any]:
    from supagent.knowledge.brief import _graph

    g = _graph()
    cats, todo = _categories(g)
    inter, t = _interactions()
    todo += t
    tables, catalog, t = _tables()
    todo += t
    paths, t = _paths()
    todo += t
    out: dict[str, Any] = {"categories": cats, "interactions": inter, "tables": tables, "catalog": catalog,
                           "paths": paths, "to_do": todo}
    if question:
        out["question"] = of_question(question, g)
        q = out["question"]
        if not q["names"]:
            out["to_do"].insert(0, "the question names no part the categories say something of: the agent is given "
                                   "no picture of the system for it (add the application, family or component it is "
                                   "about as a value, with its other names, what it is part of and its interactions)")
        if q["says_nothing"]:
            out["to_do"].insert(0, "the question names " + _some(q["says_nothing"]) + ", of which nothing is said (no "
                                   "description, part of nothing, no part, no interaction): not in the picture")
        if q["not_known"]:
            out["to_do"].insert(0, "the question writes " + _some(q["not_known"]) + " as names: neither a value of "
                                   "the categories nor a table, field, metric or value the agent learned")
    return out


def text(out: dict[str, Any]) -> str:
    """The report in lines (the command line)."""
    lines = ["Parts of the system (the categories):"]
    for c in out["categories"]:
        lines.append(f'  {c["category"]}: {c["values"]} value(s), {c["described"]} described, '
                     f'{c["part_of_or_parts"]} part of another or with parts, {c["with_interactions"]} with '
                     "interactions; in the data: " + ("; ".join(c["in_the_data"][:3]) or "nowhere"))
    i = out["interactions"]
    lines.append("Interactions (System map): " + (", ".join(f"{k} {n}" for k, n in sorted(i["approved"].items())) or "none")
                 + (f'; {i["proposed"]} proposed, waiting' if i["proposed"] else ""))
    lines.append("Tables:")
    for t in out["tables"]:
        usual = ", ".join(f"{n} = usual {m}" for n, m in t["usual_values"].items())
        lines.append(f'  {t["table"]}: time field {t["time_field"] or "?"}, {t["joins"]} join(s)'
                     + (f"; {usual}" if usual else "") + ("" if t["in_the_catalog"] else " (not in the catalog)"))
    c, p = out["catalog"], out["paths"]
    lines.append(f'Health checks: {c["checks"]}; metric labels tied to fields: {c["label_relationships"]}; '
                 f'investigation paths: {p["confirmed"]} confirmed, {p["waiting"]} waiting')
    q = out.get("question")
    if q:
        lines.append("The question names: " + (", ".join(q["names"]) or "no part the categories say something of"))
        lines.append(q["given"] or "(no picture of the system is given for it)")
    lines.append("To do:" if out["to_do"] else "Nothing missing was found.")
    lines += [f"- {x}" for x in out["to_do"]]
    return "\n".join(lines)
