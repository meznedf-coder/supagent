"""Parts that look retired, proposed to an admin (0.9). Nothing is retired by itself.

Two signs are looked for with the daily learning (and a classification run):

  not in the data any more   a value the learning read in the data (its source is the data: a server from a field
                             NODE, a host from a metric label) that none of the fields and labels of its category
                             has shown for categories.retire_days days (14), seen so by two checks on two days.
                             Looked up in the data itself, with the time: the values a field had over that period,
                             the values a label had on any metric over that period. Never concluded from a list that
                             may be cut, a query that failed, an index with no time field, or data that stopped as a
                             whole (no value of the category seen at all: the data is late, not the part retired).
  said retired               any value (the ones an admin added by hand too) that a document, a catalog entry, a
                             Context page or a team note names in a sentence saying it was retired or
                             decommissioned. The sentence is kept, word for word.

A value an admin put by hand, with no base in the data, is only ever proposed by the second sign.

Each proposal waits in To review ("Parts that look retired") with its reason: Retire (the value is kept, marked
retired, no longer used nor drawn; adding it again by hand brings it back) or Keep (the same reason is not
proposed again: a new absence after it was seen again, or another sentence, is). A value seen again in the data
loses its proposal.

Kept in the value's `suggested` (no table, no column added):
  absent  {"since": day of the first check that did not see it, "checks": n, "last": day of the last one}
  retire  {"kind": "absent" | "said" | "unnamed", "why": ..., "evidence": ..., "at": day}
  kept    {"absent": the `since` that was declined, "said": [hashes of the sentences declined], "unnamed": true}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from typing import Any, Iterator

from superset import db

log = logging.getLogger(__name__)
PLACES = 80               # fields and labels asked in one check (the next check takes the next ones)
VALUES = 20_000           # values read from one of them at most (more: nothing is concluded from it)
PROPOSED = 200            # proposals made by one check
QUOTE_CHARS = 400
SAID = re.compile(
    r"\b(retired|decomm?ission(?:ed|ing)|end[- ]of[- ]life|shut down for good|switched off for good|"
    r"removed from service|taken out of service|out of service|no longer (?:in use|used|in service|exists?|running)|"
    r"dismantled|phased out|d[ée]commissionn[ée]e?s?|retir[ée]e?s? du service|arr[êe]t[ée]e?s? d[ée]finitivement|"
    r"hors service|mise? hors service|n'est plus (?:utilis[ée]e?|en service))\b", re.I)
# between a part's name and the word that follows it: "X was officially decommissioned", "X: retired", never
# "X replaced the retired Y" (the word is said of Y)
BEFORE = re.compile(r"^[\s,:(]*(?:(?:was|were|is|are|has been|have been|had been|will be|got|being|a [ée]t[ée]|"
                    r"ont [ée]t[ée]|est|sont|sera|seront)\s+)?(?:(?:officially|finally|now|already|fully|recently|just|"
                    r"then|permanently|completely|d[ée]finitivement|officiellement|maintenant)\s+)?$", re.I)
AFTER = re.compile(r"^[\s:,(]*(?:(?:the|server|servers|host|hosts|node|nodes|application|applications|service|"
                   r"services|feed|feeds|pool|pools|component|components|le|la|les|du|de|des|serveur|serveurs)\s+){0,3}$",
                   re.I)
LIST = re.compile(r"^[\s,;:(]*(?:(?:and|et|&)\s*)?$", re.I)       # between two names of a list


def _today() -> dt.date:
    return dt.datetime.utcnow().date()


def days() -> int:
    from supagent import settings

    try:
        return max(0, int(settings.get("categories.retire_days") or 0))
    except (TypeError, ValueError):
        return 0


def _digest(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).lower().encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------------- #
# not in the data any more
# --------------------------------------------------------------------------------------------- #
def places() -> dict[str, list[dict[str, Any]]]:
    """Where each category read from the data is looked up: {category: [{"kind": "field", "source", "database",
    "index", "name", "time"} | {"kind": "label", "source", "database", "name"}]} (a label once per metrics
    database, whatever the number of metrics that carry it)."""
    from supagent.knowledge.facets import field_rules
    from supagent.models import KObject, Source
    from supagent.tools import _catalog

    rules = field_rules()
    if not rules:
        return {}
    sources = {s.id: s for s in db.session.query(Source)}
    times: dict[tuple[int, str], str | None] = {}
    for o in db.session.query(KObject.source_id, KObject.name, KObject.stats).filter(
            KObject.kind == "index", KObject.gone_at.is_(None)):
        times[(o[0], o[1])] = (o[2] or {}).get("time_field")
    declared = {name: (spec or {}).get("time_field") for name, spec in ((_catalog().get("indices") or {}).items())}
    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[Any, ...]] = set()
    rows = db.session.query(KObject.kind, KObject.name, KObject.parent, KObject.source_id).filter(
        KObject.kind.in_(("field", "label")), KObject.gone_at.is_(None)).distinct()
    for kind, name, parent, source_id in rows:
        cats = [cat for cat, rx in rules if rx.match(name or "")]
        src = sources.get(source_id)
        if not cats or src is None:
            continue
        key = (kind, source_id, name, parent if kind == "field" else "")
        if key in seen:
            continue
        seen.add(key)
        place = {"kind": kind, "source": source_id, "database": src.database_id, "name": name}
        if kind == "field":
            place.update(index=parent or "", time=declared.get(parent or "") or times.get((source_id, parent or "")))
        for cat in cats:
            out.setdefault(cat, []).append(place)
    return out


def _recent_values(place: dict[str, Any], since_days: int) -> set[str] | None:
    """The values this field or label had in the last `since_days` days, in lower case; None when it cannot be
    told (no time field, a failed or cut answer): nothing is then concluded for its category."""
    from superset.models.core import Database

    from supagent import tools

    database = db.session.get(Database, place["database"])
    if database is None:
        return None
    try:
        if place["kind"] == "label":
            conn = tools._promagg_connection(database)
            try:
                end = tools._now_ms(conn)
                got = conn.client.label_values(place["name"], None, end - since_days * 86_400_000, end, limit=VALUES + 1)
            finally:
                conn.close()
            return None if len(got) > VALUES else {str(v).strip().lower() for v in got if str(v).strip()}
        if not place.get("time") or not place.get("index"):
            return None
        from supagent.knowledge import groups as G

        with tools._db_connection(database, extract=False) as conn:
            since = tools._local_now(conn) - dt.timedelta(days=since_days)
            field, index, tf = G.name(place["name"]), G.name(place["index"]), G.name(place["time"])
            cur = conn.cursor()
            cur.execute(f'SELECT "{field}" AS v, COUNT(*) AS n FROM "{index}" WHERE "{tf}" >= {G.lit(since)} '
                        f'GROUP BY "{field}" LIMIT {VALUES + 1}')
            rows = cur.fetchall()
        return None if len(rows) > VALUES else {" ".join(str(r[0]).split()).lower() for r in rows if r[0] is not None}
    except Exception as ex:  # pylint: disable=broad-except
        log.info("supagent retire: %s %s: not read (%s)", place["kind"], place["name"], str(ex)[:200])
        db.session.rollback()
        return None


def recent(seconds: float = 120.0) -> dict[str, dict[str, Any]]:
    """Per category read from the data: {"values": what its fields and labels showed over the period, "complete":
    every one of them answered, "asked": n}. A category whose places could not all be asked within the time, or
    one of which did not answer, is not complete."""
    n = days()
    out: dict[str, dict[str, Any]] = {}
    if not n:
        return out
    t0 = time.time()
    asked: dict[tuple[Any, ...], set[str] | None] = {}
    for cat, where in places().items():
        got: set[str] = set()
        complete = True
        for place in where:
            key = (place["kind"], place["source"], place["name"], place.get("index", ""))
            if key not in asked:
                if len(asked) >= PLACES or time.time() - t0 > seconds:
                    complete = False
                    continue
                asked[key] = _recent_values(place, n)
            if asked[key] is None:
                complete = False
            else:
                got |= asked[key]
        out[cat] = {"values": got, "complete": complete, "asked": len(where)}
    return out


# --------------------------------------------------------------------------------------------- #
# said retired in a text
# --------------------------------------------------------------------------------------------- #
def texts() -> Iterator[dict[str, Any]]:
    """The texts of the team: documents, every catalog entry, Context pages, team notes."""
    from supagent.models import ContextPage, Doc, Entry, Note

    for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.content.isnot(None)):
        yield {"ref": f"doc:{d.id}", "title": d.title or d.url or "", "text": d.content or ""}
    for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True)):
        yield {"ref": f"entry:{e.id}", "title": e.title, "text": e.content or ""}
    for p in db.session.query(ContextPage):
        yield {"ref": f"context:{p.id}", "title": p.title, "text": p.content or ""}
    for n in db.session.query(Note).filter(Note.scope == "team"):
        yield {"ref": f"note:{n.id}", "title": n.title or "", "text": n.text or ""}


def _spots(sentence: str, names: list[str]) -> list[tuple[int, int]]:
    low = sentence.lower()
    out = []
    for name in names:
        for m in re.finditer(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", low):
            out.append((m.start(), m.end()))
    return sorted(out)


def said_in(sentence: str, g: dict[str, Any]) -> list[int]:
    """The values a sentence says retired: the only part it names; with several, the ones the word stands next
    to ("X was decommissioned", "the retired server X"), or every one of a list that follows it ("Decommissioned:
    X, Y and Z"). "X replaced the retired Y" says it of Y only."""
    from supagent.knowledge.brief import named

    m = SAID.search(sentence)
    if not m:
        return []
    ids = named(sentence, g)
    if len(ids) <= 1:
        return ids
    V = g["values"]
    spots = {i: _spots(sentence, [V[i]["name"]] + [n for n, xs in g["names"].items() if i in xs]) for i in ids}
    every = sorted(s for ss in spots.values() for s in ss)
    out = []
    for i in ids:
        for start, end in spots[i]:
            if end <= m.start() and BEFORE.match(sentence[end:m.start()]):
                out.append(i)
                break
            if start >= m.end() and AFTER.match(sentence[m.end():start]):
                out.append(i)
                break
    after = [s for s in every if s[0] >= m.end()]             # a list of names right after the word
    if after and AFTER.match(sentence[m.end():after[0][0]]) is not None:
        ok, at = True, after[0][1]
        for start, end in after[1:]:
            if not LIST.match(sentence[at:start]):
                ok = False
                break
            at = end
        if ok and not sentence[at:].strip(" .;)"):
            out += [i for i in ids if any(s in after for s in spots[i])]
    return list(dict.fromkeys(out))


def said_retired(g: dict[str, Any]) -> list[tuple[int, str, str]]:
    """(value id, the sentence, where it is written) for every sentence of the team's texts that says a known part
    was retired."""
    out = []
    for it in texts():
        if not SAID.search(it["text"] or ""):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", it["text"]):
            sentence = " ".join(sentence.split())
            if len(sentence) < 12 or not SAID.search(sentence):
                continue
            for i in said_in(sentence, g):
                out.append((i, sentence[:QUOTE_CHARS], (it["title"] or it["ref"])[:80]))
    return out


# --------------------------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------------------------- #
def check(seconds: float = 120.0) -> dict[str, Any]:
    """Look for the two signs and propose (never retire). What it did, counted."""
    from supagent.knowledge.brief import _graph
    from supagent.knowledge.facets import editable, named as names_something
    from supagent.models import Facet

    out: dict[str, Any] = {"proposed": 0, "absent": 0, "seen_again": 0, "said": 0, "categories_checked": 0}
    today = _today().isoformat()
    n = days()
    seen = recent(seconds) if n else {}
    rows = db.session.query(Facet).filter(Facet.status == "approved", Facet.facet.in_(list(editable()))).all()
    for cat, state in seen.items():
        if not state["complete"]:
            out.setdefault("not_checked", {})[cat] = "a field or a label of it could not be read over the period"
            continue
        if not state["values"]:
            out.setdefault("not_checked", {})[cat] = "none of its values is in the data of the period: the data is late"
            continue
        out["categories_checked"] += 1
        for f in rows:
            if f.facet != cat or f.source != "data":
                continue                              # put by hand: never from an absence
            sug = dict(f.suggested or {})
            low = " ".join((f.value or "").lower().split())
            others = {str(s).lower() for s in (f.synonyms or [])}
            if low in state["values"] or others & state["values"]:
                if sug.get("absent") or (sug.get("retire") or {}).get("kind") == "absent":
                    sug.pop("absent", None)
                    if (sug.get("retire") or {}).get("kind") == "absent":
                        sug.pop("retire")
                    kept = dict(sug.get("kept") or {})
                    kept.pop("absent", None)          # seen again: a later absence is another one
                    sug["kept"] = kept
                    if not kept:
                        sug.pop("kept")
                    f.suggested = sug or None
                    out["seen_again"] += 1
                continue
            was = dict(sug.get("absent") or {})
            if was.get("last") != today:
                was = {"since": was.get("since") or today, "checks": int(was.get("checks") or 0) + 1, "last": today}
                sug["absent"] = was
                out["absent"] += 1
            if (was["checks"] >= 2 and was["since"] < today and not sug.get("retire")
                    and (sug.get("kept") or {}).get("absent") != was["since"] and out["proposed"] < PROPOSED):
                where = "; ".join(str(o) for o in (f.origins or []) if str(o).startswith(("field ", "label ")))[:300]
                sug["retire"] = {"kind": "absent", "at": today,
                                 "why": f"not in the data of the last {n} days (looked for on {was['since']} and "
                                        f"on {today}), while other values of the category are",
                                 "evidence": where}
                out["proposed"] += 1
            f.suggested = sug
    # said retired in a text: any value in use, the ones put by hand too
    try:
        g = _graph()
        by_id = {f.id: f for f in rows}
        for fid, quote, origin in said_retired(g):
            f = by_id.get(fid)
            if f is None:
                continue
            sug = dict(f.suggested or {})
            h = _digest(quote)
            if h in ((sug.get("kept") or {}).get("said") or []):
                continue
            cur = sug.get("retire") or {}
            if cur.get("kind") == "said" and cur.get("hash") == h:
                continue
            if cur.get("kind") == "said" or out["proposed"] >= PROPOSED:
                continue                              # one sentence at a time
            sug["retire"] = {"kind": "said", "at": today, "why": "a text says it was retired", "hash": h,
                             "evidence": f"{quote} ({origin})"}
            f.suggested = sug
            out["said"] += 1
            out["proposed"] += 1
    except Exception:  # pylint: disable=broad-except   (the other sign stays)
        log.warning("supagent retire: the texts: not read", exc_info=True)
    # a code that names nothing, seeded from the data by an older rule
    for f in rows:
        if f.facet == "application" and f.source == "data" and not names_something(f.value):
            sug = dict(f.suggested or {})
            if sug.get("retire") or (sug.get("kept") or {}).get("unnamed") or out["proposed"] >= PROPOSED:
                continue
            sug["retire"] = {"kind": "unnamed", "at": today, "evidence": "",
                             "why": "a code of one character or with no letter: it names nothing a question could say"}
            f.suggested = sug
            out["proposed"] += 1
    db.session.commit()
    return out


def waiting(limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """The proposals for the review: ([{id, facet, value, source, kind, why, evidence, at, items}], how many)."""
    from supagent.knowledge.facets import editable
    from supagent.models import Facet

    rows = [f for f in db.session.query(Facet).filter(Facet.status == "approved", Facet.suggested.isnot(None),
                                                       Facet.facet.in_(list(editable())))
            if (f.suggested or {}).get("retire")]
    rows.sort(key=lambda f: (f.facet, f.value.lower()))
    out = []
    for f in rows[:limit]:
        r = f.suggested["retire"]
        out.append({"id": f.id, "facet": f.facet, "value": f.value, "source": f.source, "kind": r.get("kind"),
                    "why": r.get("why") or "", "evidence": r.get("evidence") or "", "at": r.get("at")})
    return out, len(rows)


def decide(f: Any, retire: bool, by: str) -> str:
    """An admin's answer to a proposal: retired (kept, not used), or kept (that reason is not proposed again)."""
    sug = dict(f.suggested or {})
    r = sug.pop("retire", None) or {}
    if retire:
        f.status = "rejected"
        sug.pop("absent", None)
    else:
        kept = dict(sug.get("kept") or {})
        if r.get("kind") == "absent":
            kept["absent"] = (sug.get("absent") or {}).get("since")
        elif r.get("kind") == "said" and r.get("hash"):
            kept["said"] = list(dict.fromkeys(list(kept.get("said") or []) + [r["hash"]]))[-50:]
        elif r.get("kind") == "unnamed":
            kept["unnamed"] = True
        sug["kept"] = kept
    f.suggested = sug or None
    f.reviewed_by, f.reviewed_at = by, dt.datetime.utcnow()
    return f.status
