"""The system around a question (0.9): what the team's categories, the interactions drawn between their values
and the catalog say about the parts a question names, put together without the LLM and given with the question.

A question about "the batch of the billing applications" names a value of the categories. From it: what the
value consists of (the applications of a family, the servers of a pool), what those depend on, run on, read,
call (one step further for what they wait for), where each kind of part is in the data (the fields and metric
labels its category is read from), how the tables join, which field holds the usual value of a measure, and the
health checks. An investigation starts from this picture instead of discovering the system query by query, and
a plain count starts with the right values ("billing applications" = the three that are part of it).

Nothing here is about a domain: everything is read from the deployment's own categories (knowledge.facets), the
interactions of its system map (knowledge.sysmap) and its catalog.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from superset import db

log = logging.getLogger(__name__)
SEEDS = 4                 # values of the question the picture is built from
MEMBERS = 12              # parts of a named value shown by name
ACTORS = 10               # values whose interactions are read
TARGETS = 8               # per kind of interaction
FURTHER = 8               # values followed one step further
BRIEF_CHARS = 4600
ABOUT_CHARS = 700         # what the categories are, in the picture
FOLLOW_CHARS = 900        # what to do on the interactions of the question's parts (their long explanations)
ROUTER_CHARS = 500        # what the question names, for the router
SCOPE_CHARS = 700
ATOM = re.compile(r"[A-Za-z0-9À-ÿ_][A-Za-z0-9À-ÿ_.:/\-]*")
OUT = {"depends_on": "depend on", "runs_on": "run on", "reads_from": "read from", "sends_to": "send data to",
       "calls": "call", "triggers": "trigger", "monitors": "monitor", "about": "relate to"}
ONE = {"depends_on": "depends on", "runs_on": "runs on", "reads_from": "reads from", "sends_to": "sends data to",
       "calls": "calls", "triggers": "triggers", "monitors": "monitors", "about": "relates to"}
IN = {"depends_on": "needed by", "runs_on": "what runs on them", "reads_from": "read by", "sends_to": "receive data from",
      "calls": "called by", "triggers": "triggered by", "monitors": "monitored by", "about": "related"}
FOLLOWED = ("depends_on", "reads_from", "runs_on", "calls", "sends_to")     # what a slowdown or a delay can come from
USUAL_NAME = re.compile(r"^(avg|average|mean|usual|baseline|median|typical)[_ ]", re.I)
_CACHE: dict[str, Any] = {"stamp": None, "graph": None, "fields": {}}
_LOCK = threading.Lock()


def _graph() -> dict[str, Any]:
    """The approved values with their names, what each is part of and consists of, and the interactions drawn
    between them; read again when the knowledge changed."""
    from supagent.knowledge.facets import editable
    from supagent.knowledge.freshness import stamp
    from supagent.models import Facet, Link

    s = stamp()
    with _LOCK:
        if _CACHE["graph"] is not None and _CACHE["stamp"] == s:
            return _CACHE["graph"]
    cats = list(editable())
    values: dict[int, dict[str, Any]] = {}
    names: dict[str, list[int]] = {}
    children: dict[int, list[int]] = {}
    for f in db.session.query(Facet).filter(Facet.status == "approved", Facet.facet.in_(cats)):
        values[f.id] = {"id": f.id, "cat": f.facet, "name": f.value, "about": (f.description or "").strip(),
                        "parents": [int(p) for p in (f.parents or []) if str(p).isdigit()]}
        for n in [f.value] + [str(x) for x in (f.synonyms or [])]:
            n = " ".join(n.lower().split())
            if len(n) >= 2:
                names.setdefault(n, []).append(f.id)
    for v in values.values():
        v["parents"] = [p for p in v["parents"] if p in values and p != v["id"]]
        for p in v["parents"]:
            children.setdefault(p, []).append(v["id"])
    out_links: dict[int, list[tuple[str, int, str]]] = {}
    in_links: dict[int, list[tuple[str, int, str]]] = {}
    long: dict[tuple[int, str, int], str] = {}        # what to do when following an interaction (its long explanation)
    rows = db.session.query(Link.a_ref, Link.b_ref, Link.kind, Link.note, Link.detail).filter(
        Link.a_ref.like("facet:%"), Link.b_ref.like("facet:%"), Link.status == "approved")
    for a_ref, b_ref, kind, note, detail in rows:
        a, b = a_ref.split(":", 1)[1], b_ref.split(":", 1)[1]
        if a.isdigit() and b.isdigit() and int(a) in values and int(b) in values:
            out_links.setdefault(int(a), []).append((kind, int(b), (note or "").strip()))
            in_links.setdefault(int(b), []).append((kind, int(a), (note or "").strip()))
            if (detail or "").strip():
                long[(int(a), kind, int(b))] = " ".join(detail.split())
    g = {"values": values, "names": names, "children": children, "out": out_links, "in": in_links, "rank": cats,
         "long": long}
    with _LOCK:
        _CACHE.update(stamp=s, graph=g)
    return g


def named(text: str, g: dict[str, Any] | None = None) -> list[int]:
    """The values a text names, in its order: a value's name or one of its other names written as words of
    their own (the longest name wins: "risk engine" before "risk"); a name that is a common word counts only
    written as the value is (HELIX, not "the")."""
    from supagent.knowledge.describe import STOP

    g = g or _graph()
    atoms = [(m.group(0).rstrip(".:/-"), m.start()) for m in ATOM.finditer(text or "")]
    atoms = [(a, at) for a, at in atoms if a]
    found: list[int] = []
    i = 0
    while i < len(atoms):
        hit = 0
        for n in range(min(5, len(atoms) - i), 0, -1):
            words = [a for a, _at in atoms[i:i + n]]
            key = " ".join(w.lower() for w in words)
            ids = g["names"].get(key)
            if not ids:
                continue
            if n == 1 and (len(key) < 3 or key in STOP) and not any(g["values"][x]["name"] == words[0] for x in ids):
                continue
            found += [x for x in ids if x not in found]
            hit = n
            break
        i += hit or 1
    return found


def says_something(i: int, g: dict[str, Any]) -> bool:
    """A value with a description, something it is part of or consists of, or an interaction: a part of the
    system. One that has none of these is a word of the question."""
    v = g["values"][i]
    return bool(v["about"] or v["parents"] or g["children"].get(i) or g["out"].get(i) or g["in"].get(i))


def _first_sentence(text: str, chars: int = 150) -> str:
    s = re.split(r"(?<=[.!?])\s", " ".join((text or "").split()), maxsplit=1)[0]
    return s[:chars].rstrip(" .") + ("…" if len(s) > chars else "")


def _fields_of(cats: list[str], names: dict[str, list[str]]) -> dict[str, list[str]]:
    """Where each category is in the data the user may query: the fields and the metric labels its values are
    read from (categories.fields), else where its named values are found among the learned values."""
    from supagent.knowledge.facets import field_rules
    from supagent.knowledge.resolve import _databases, value_rows
    from supagent.models import KObject, Source

    databases = _databases()
    by_source = {s.id: next((d for d in databases if d.id == s.database_id), None) for s in db.session.query(Source)}
    by_source = {sid: d for sid, d in by_source.items() if d is not None}
    out: dict[str, list[str]] = {}
    if not by_source:
        return out
    rules = [(c, rx) for c, rx in field_rules()]
    if rules:
        # every field and label of the dictionary is looked at (tens of thousands on a platform): once per state of
        # the knowledge and set of databases, whatever the question
        from supagent.knowledge.freshness import stamp

        key = (stamp(), tuple(sorted(d.id for d in by_source.values())), tuple((c, rx.pattern) for c, rx in rules))
        with _LOCK:
            known = _CACHE["fields"].get(key)
        if known is None:
            fields: dict[tuple[str, int, str], list[str]] = {}
            labels: dict[tuple[str, int, str], list[str]] = {}
            rows = db.session.query(KObject.source_id, KObject.kind, KObject.parent, KObject.name).filter(
                KObject.kind.in_(("field", "label")), KObject.gone_at.is_(None), KObject.source_id.in_(list(by_source)))
            for sid, kind, parent, name in rows:
                for cat, rx in rules:
                    if rx.match(name or ""):
                        (fields if kind == "field" else labels).setdefault((cat, by_source[sid].id, name), []).append(parent or "")
            known = {}
            for (cat, dbid, name), tables in sorted(fields.items()):
                known.setdefault(cat, []).append(f'field "{name}" of {_some(sorted(set(tables)), 4)} (database {dbid})')
            for (cat, dbid, name), metrics in sorted(labels.items()):
                known.setdefault(cat, []).append(f'label {name} of {len(set(metrics))} metric(s) such as '
                                                 f'{_some(sorted(set(metrics)), 3)} (database {dbid})')
            with _LOCK:
                _CACHE["fields"] = {key: known}
        out.update({c: list(v) for c, v in known.items() if c in cats})
    todo = {c: names.get(c, [])[:3] for c in cats if c not in out and names.get(c)}
    if todo:
        tokens = [t for ts in todo.values() for t in ts]
        places = value_rows(tokens, list(by_source.values()))
        for cat, ts in todo.items():
            seen: list[str] = []
            for t in ts:
                for (dbid, kind, name), parents in sorted((places.get(t) or {}).items()):
                    what = (f'field "{name}" of {_some(sorted(set(parents)), 3)}' if kind == "field" else
                            f"label {name} of {len(set(parents))} metric(s) such as {_some(sorted(set(parents)), 3)}")
                    line = f"{what} (database {dbid})"
                    if line not in seen:
                        seen.append(line)
            if seen:
                out[cat] = seen[:4]
    return out


def _some(items: list[str], n: int) -> str:
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _joins(tables: set[str]) -> list[str]:
    """How these tables join the others, and which fields hold a usual value (the catalog)."""
    from supagent.tools import _catalog

    cat = _catalog()
    lines: list[str] = []
    indices = cat.get("indices") or {}
    for t in sorted(tables):
        spec = indices.get(t) or {}
        for rel in (spec.get("relationships") or [])[:6]:
            keys = ", ".join(f'"{a}" = "{b}"' for a, b in (rel.get("keys") or {}).items())
            if rel.get("to") and keys:
                lines.append(f'{t} -> {rel["to"]} on {keys}' + (f' ({rel["description"]})' if rel.get("description") else ""))
        for name, fs in (spec.get("fields") or {}).items():
            if not isinstance(fs, dict):
                continue
            if fs.get("usual_of"):
                lines.append(f'"{name}" of {t} is the usual value of "{fs["usual_of"]}" in the same document '
                             f'(compare_groups measure: ratio({fs["usual_of"]}, {name}))')
            elif USUAL_NAME.match(name) and fs.get("description"):
                lines.append(f'"{name}" of {t} holds a usual value: {_first_sentence(str(fs["description"]), 120)}')
    for rel in ((cat.get("metrics") or {}).get("label_relationships") or [])[:8]:
        if rel.get("index") in tables and rel.get("label") and rel.get("field"):
            lines.append(f'metric label {rel["label"]} = "{rel["field"]}" of {rel["index"]}')
    return list(dict.fromkeys(lines))


def _log_tables(tables: set[str]) -> list[str]:
    """The tables among these that hold lines of text (logs, events, messages: a message field), with their
    message and level fields: what compare_logs reads."""
    from supagent.tools import _level_field, _message_field

    out = []
    for t in sorted(tables):
        msg = _message_field(t)
        if msg:
            lvl = _level_field(t)
            out.append(f'{t} (its text: "{msg}"' + (f', its level: "{lvl}"' if lvl else "") + ")")
    return out


def _tables_of(lines: dict[str, list[str]]) -> set[str]:
    from supagent.tools import _catalog

    known = set((_catalog().get("indices") or {}))
    text = " ".join(x for xs in lines.values() for x in xs)
    return {t for t in known if t in text}


def build(question: str, full: bool = True) -> dict[str, Any] | None:
    """The picture of a question: None when it names no value of the categories. `full`: the interactions,
    the data places and the joins (an investigation); else only what the named values consist of. The lines
    come in the order they matter when room is short: what is named, what it depends on and uses, where it is in
    the data with the joins and the usual values, one step further, the health checks, then what depends on it."""
    g = _graph()
    if not g["values"]:
        return None
    V, kids = g["values"], g["children"]
    seeds = [s for s in named(question, g) if says_something(s, g)][:SEEDS]
    if not seeds:
        return None
    rank = {c: i for i, c in enumerate(g["rank"])}
    head: list[str] = []
    actors: list[int] = []
    mentioned: dict[str, list[str]] = {}

    def see(i: int) -> str:
        v = V[i]
        if v["name"] not in mentioned.setdefault(v["cat"], []):
            mentioned[v["cat"]].append(v["name"])
        return v["name"]

    def some(ids: list[int], n: int) -> str:
        return ", ".join(see(i) for i in ids[:n]) + (f" and {len(ids) - n} more" if len(ids) > n else "")

    for s in seeds:
        v = V[s]
        line = f'- {see(s)} ({v["cat"]})'
        if v["about"]:
            line += f': {_first_sentence(v["about"])}'
        if v["parents"]:
            line += ". Part of " + ", ".join(f'{see(p)} ({V[p]["cat"]})' for p in v["parents"][:4])
        parts = sorted(kids.get(s, []), key=lambda i: (rank.get(V[i]["cat"], 99), V[i]["name"].lower()))
        if parts:
            by_cat: dict[str, list[int]] = {}
            for i in parts:
                by_cat.setdefault(V[i]["cat"], []).append(i)
            line += ". Its parts: " + "; ".join(f"{c}s {some(ids, MEMBERS)}" for c, ids in by_cat.items())
            actors += [i for i in parts if i not in actors]
        else:
            actors.append(s)
        head.append(line + ".")
    out: dict[str, Any] = {"seeds": [V[s]["name"] for s in seeds], "lines": head}
    if not full:
        return out if any(kids.get(s) for s in seeds) else None
    actors = actors[:ACTORS]
    everyone = len(actors)
    who = "They" if everyone > 1 else V[actors[0]]["name"]
    further: list[int] = []
    uses: list[str] = []
    used_by: list[str] = []
    for links, incoming in ((g["out"], False), (g["in"], True)):
        by_kind: dict[str, dict[int, dict[str, Any]]] = {}
        for a in actors:
            for kind, b, note in links.get(a, []):
                if b in actors and incoming:
                    continue                       # between the actors: said once, the way it was drawn
                slot = by_kind.setdefault(kind, {}).setdefault(b, {"who": [], "notes": []})
                slot["who"].append(a)
                if note and note not in slot["notes"]:
                    slot["notes"].append(note)
        for kind in sorted(by_kind, key=lambda k: FOLLOWED.index(k) if k in FOLLOWED else 9):
            targets = by_kind[kind]
            bits = []
            for b, slot in list(targets.items())[:TARGETS]:
                extra = []
                if 1 < everyone != len(slot["who"]):
                    extra.append("only " + _some([V[a]["name"] for a in slot["who"]], 3))
                if slot["notes"]:
                    extra.append("; ".join(slot["notes"][:2])[:110])
                bits.append(f'{see(b)} ({V[b]["cat"]}' + (": " + ", ".join(extra) if extra else "") + ")")
                if not incoming and kind in FOLLOWED and b not in further and b not in actors:
                    further.append(b)
            more = f" and {len(targets) - TARGETS} more" if len(targets) > TARGETS else ""
            if incoming:
                used_by.append(f"- {IN.get(kind, kind)[0].upper() + IN.get(kind, kind)[1:]}: " + "; ".join(bits) + more + ".")
            else:
                verb = OUT.get(kind, kind) if everyone > 1 else ONE.get(kind, kind)
                uses.append(f"- {who} {verb}: " + "; ".join(bits) + more + ".")
    steps = []
    for b in further[:FURTHER]:                    # one step further: what they wait for, and what it consists of
        bits = []
        for kind in FOLLOWED:
            ts = [t for k, t, _n in g["out"].get(b, []) if k == kind and t not in actors]
            if ts:
                bits.append(f"{ONE[kind]} {some(ts, 6)}")
        parts = kids.get(b, [])
        if parts:
            bits.append(f'has {len(parts)} {V[parts[0]]["cat"]}(s): {some(parts, 3)}')
        if bits:
            steps.append(f'{V[b]["name"]} ' + ", ".join(bits))
    further_line = ["- One step further: " + "; ".join(steps) + "."] if steps else []
    # what to do when following them: the interactions' long explanations (the team's or the AI's), the ones an
    # investigation follows first (what they depend on, read, run on, call)
    follow, room = [], FOLLOW_CHARS
    rank_kind = {k: i for i, k in enumerate(FOLLOWED)}
    for a, kind, b in sorted(((a, k, t) for a in actors for k, t, _n in g["out"].get(a, [])),
                             key=lambda x: (rank_kind.get(x[1], 9), V[x[0]]["name"].lower())):
        text = g.get("long", {}).get((a, kind, b))
        if not text:
            continue
        bit = f'{V[a]["name"]} {ONE.get(kind, kind)} {V[b]["name"]}: {text}'
        if len(bit) > room:
            break
        follow.append(bit)
        room -= len(bit)
    follow_line = ["- What to do when following them (the map's explanations): " + " | ".join(follow)] if follow else []
    data: list[str] = []
    try:
        places = _fields_of(sorted(mentioned, key=lambda c: rank.get(c, 99)), mentioned)
    except Exception:  # pylint: disable=broad-except   (the picture without the data places)
        log.warning("supagent brief: the data places: not read", exc_info=True)
        db.session.rollback()
        places = {}
    try:                                              # what the team says each category is (categories.about)
        from supagent.knowledge.facets import about

        said = about()
    except Exception:  # pylint: disable=broad-except
        said = {}
    kinds = [f"{c}: {said[c]}" for c in sorted(mentioned, key=lambda c: rank.get(c, 99)) if said.get(c)]
    if kinds:
        data.append("- What these categories are: " + "; ".join(kinds)[:ABOUT_CHARS] + ".")
    if places:
        data.append("- In the data: " + "; ".join(f"{c}s: " + " and ".join(ps[:3]) for c, ps in places.items()) + ".")
        try:
            joins = _joins(_tables_of(places))
        except Exception:  # pylint: disable=broad-except
            joins = []
        if joins:
            data.append("- Joins and usual values (the catalog): " + "; ".join(joins[:10]) + ".")
        try:
            logs = _log_tables(_tables_of(places))
        except Exception:  # pylint: disable=broad-except
            logs = []
        if logs:
            data.append("- Their logs (compare_logs): " + "; ".join(logs[:4]) + ".")
    try:
        from supagent.tools import _catalog

        checks = list((_catalog().get("checks") or {}))
    except Exception:  # pylint: disable=broad-except
        checks = []
    checks_line = ["- Health checks (check_health): " + _some(checks, 16) + "."] if checks else []
    # the first steps of an investigation need the data places, the joins and the usual values: before what is
    # one step further (system_links gives it again)
    out.update(lines=head + uses + data + follow_line + further_line + checks_line + used_by, places=places,
               further=[V[b]["name"] for b in further])
    return out


def brief_block(question: str, full: bool = True) -> str:
    """The picture as a block of the prompt ("" when the question names no value of the categories)."""
    try:
        b = build(question, full)
    except Exception:  # pylint: disable=broad-except   (the agent answers without it)
        log.warning("supagent brief: not built", exc_info=True)
        db.session.rollback()
        return ""
    if not b or not b["lines"]:
        return ""
    head = ("\n\nThe system around this question (from the team's categories, the interactions of the system map and "
            "the catalog: names are exact; start from it, and follow it when a finding points to another part):"
            if full else "\n\nWhat the question names (from the team's categories: names are exact):")
    budget = BRIEF_CHARS if full else SCOPE_CHARS
    kept, used = [], 0
    for line in b["lines"]:
        if used + len(line) > budget and kept:
            break
        kept.append(line[:budget])
        used += len(line)
    return head + "\n" + "\n".join(kept)


def named_line(question: str) -> str:
    """What the question names, in a line, for the router: each part with its category and what it is, then what
    those categories are ("" when it names no part). Short: the router's call is before every answer."""
    try:
        g = _graph()
        ids = [i for i in named(question, g) if says_something(i, g)][:SEEDS]
        if not ids:
            return ""
        from supagent.knowledge.facets import about

        said = about()
    except Exception:  # pylint: disable=broad-except   (the router decides without it)
        db.session.rollback()
        return ""
    V = g["values"]
    parts = []
    for i in ids:
        v = V[i]
        what = _first_sentence(v["about"], 90) if v["about"] else ""
        parts.append(f'{v["name"]} ({v["cat"]}' + (f": {what}" if what else "") + ")")
    cats = [f'{c} = {said[c]}' for c in dict.fromkeys(V[i]["cat"] for i in ids) if said.get(c)]
    text = "The question names these parts of the system (the team's categories): " + "; ".join(parts) + "."
    if cats:
        text += " Categories: " + "; ".join(cats) + "."
    return text[:ROUTER_CHARS]


def links_of(names: list[str]) -> dict[str, Any]:
    """What the system map says of these parts (the tool system_links): each one's category, what it is, what it
    is part of and consists of, its interactions both ways, where its category is in the data."""
    g = _graph()
    V, kids = g["values"], g["children"]
    out: list[dict[str, Any]] = []
    unknown: list[str] = []
    cats: dict[str, list[str]] = {}
    for raw in names[:8]:
        ids = named(str(raw), g) or g["names"].get(" ".join(str(raw).lower().split()), [])
        if not ids:
            unknown.append(str(raw))
            continue
        for i in ids[:2]:
            v = V[i]
            cats.setdefault(v["cat"], []).append(v["name"])
            item: dict[str, Any] = {"name": v["name"], "category": v["cat"]}
            if v["about"]:
                item["what"] = v["about"][:300]
            if v["parents"]:
                item["part_of"] = [f'{V[p]["name"]} ({V[p]["cat"]})' for p in v["parents"][:6]]
            parts = kids.get(i, [])
            if parts:
                item["parts"] = [f'{V[p]["name"]} ({V[p]["cat"]})' for p in parts[:40]]
                if len(parts) > 40:
                    item["parts_total"] = len(parts)
            follow: list[str] = []
            for key, links, verbs in (("it", g["out"], OUT), ("others", g["in"], IN)):
                rel: dict[str, list[str]] = {}
                for kind, b, note in links.get(i, [])[:40]:
                    label = (verbs.get(kind, kind) + "s") if key == "it" else verbs.get(kind, kind)
                    rel.setdefault(label, []).append(f'{V[b]["name"]} ({V[b]["cat"]})' + (f": {note[:120]}" if note else ""))
                    text = g.get("long", {}).get((i, kind, b) if key == "it" else (b, kind, i))
                    if text and len(follow) < 8:
                        a_, b_ = (V[i]["name"], V[b]["name"]) if key == "it" else (V[b]["name"], V[i]["name"])
                        follow.append(f"{a_} {ONE.get(kind, kind)} {b_}: {text[:400]}")
                if rel:
                    item[key] = rel
            if follow:
                item["what_to_do_when_following"] = follow
            out.append(item)
    res: dict[str, Any] = {"parts": out}
    if unknown:
        res["not_in_the_map"] = unknown
    try:
        places = _fields_of(list(cats), cats)
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        places = {}
    if places:
        res["in_the_data"] = places
        joins = _joins(_tables_of(places))
        if joins:
            res["joins_and_usual_values"] = joins[:12]
    try:
        from supagent.knowledge.facets import about

        said = about()
        kinds = {c: said[c] for c in cats if said.get(c)}
        if kinds:
            res["categories"] = kinds
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
    return res
