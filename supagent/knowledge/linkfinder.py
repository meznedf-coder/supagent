"""The interactions the logs show, proposed for the System map (0.9).

A batch scheduler writes what a run waits for ("APP_B.RATES.EU.02 still waiting for its inputs after 40 min:
APP_A.RATES.EU, MD_EOD_RATES"), an application what it calls ("request to MDCACHE took 840 ms") or where it writes
("commit to RESULTS_DB took 3.2 s"). Each such line names the part it comes from (a field of the log table holding
the values of a category: its application) and the parts it names in its text. With the classification, the
recent lines of each log table are read (the latest of each of the last days), grouped into patterns
(knowledge.logs), and each pair of parts a pattern ties on enough lines and days is proposed as an interaction,
its kind read in the pattern's words (waits for, inputs: depends on; calls, requests: calls; reads, loads: reads
from; writes, publishes, commits: sends data to). A pattern that states no interaction (a GC pause on a server, a
free slot) proposes nothing. A proposal waits in To review with its evidence (how many lines, on how many days, an
example), like the ones the LLM reads in the documents; nothing is drawn or followed before an admin approves it,
and a rejected one is never proposed again. Measured on a simulated platform against its real dependencies: 18 of
18 found, none wrong (lab-private research).

Nothing here knows a kind of system: a table with a time field, a text field and a field of a category's values.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections import Counter, defaultdict
from typing import Any

from superset import db

log = logging.getLogger(__name__)
DAYS = 14                 # the last days read (a weekly job, a dependency seen on some nights only)
PER_DAY = 1680            # lines read of each day ...
SLICES = 24               # ... the latest of each of its hours (a wait at night, a call at noon), half of them of
#                           the levels that are not quiet when the table has a level (the waits are warnings, and
#                           the starts and ends of the runs would crowd them out)
LEAST_LINES = 5           # a pair proposed from this many lines ...
LEAST_DAYS = 2            # ... on this many days
PROPOSED = 100            # proposals of one run at most
EXAMPLE_CHARS = 200
# the kind of an interaction, from the words of a line's pattern (the first that matches)
KINDS = (
    ("depends_on", re.compile(r"\b(?:wait(?:s|ing|ed)? (?:for|on|until)|depend\w*|inputs?|needs?|requir\w*|"
                              r"predecessors?|upstream|blocked by)\b", re.I)),
    ("calls", re.compile(r"\b(?:calls?|calling|called|requests?|requested|connect\w*|answered|respond\w*)\b", re.I)),
    ("reads_from", re.compile(r"\b(?:read\w*|load\w*|fetch\w*|download\w*|import\w*|consum\w*)\b", re.I)),
    ("sends_to", re.compile(r"\b(?:writ\w+|wrote|publish\w*|send\w*|sent|upload\w*|export\w*|push\w*|commit\w*)\b",
                            re.I)),
)


class Budget:
    """The time the step may take, checked before each query (a table of a big cluster: the queries left are not
    sent), and the queries sent."""

    def __init__(self, seconds: float) -> None:
        self.until, self.queries, self.cut = time.time() + max(0.0, seconds), 0, False

    def left(self) -> bool:
        if time.time() >= self.until:
            self.cut = True
            return False
        self.queries += 1
        return True


def kind_of(pattern: str) -> str | None:
    """The kind of interaction a pattern's words state (its names, numbers and values left out), or None."""
    words = re.sub(r"<name>|'<value>'|#", " ", pattern or "")
    for kind, rx in KINDS:
        if rx.search(words):
            return kind
    return None


def resolve(name: str, names: dict[str, list[int]]) -> int | None:
    """The value of the categories a name in a line is: the value itself, or a value it starts with followed by
    a separator (a job APP_A.RATES.EU of the application APP_A), the longest first."""
    text = " ".join(str(name or "").split()).lower()
    if not text:
        return None
    if names.get(text):
        return names[text][0]
    for i in range(len(text) - 1, 1, -1):
        if text[i] in "._-:/" and names.get(text[:i]):
            return names[text[:i]][0]
    return None


def log_tables() -> list[dict[str, Any]]:
    """The log tables the classification reads: a table with a time field, a text field (its message) and a field
    of a category's values (where its lines come from), with the database it is in."""
    from supagent.knowledge.retire import places
    from supagent.tools import _message_field

    tables: dict[tuple[int, str], dict[str, Any]] = {}
    for cat, spots in places().items():
        for p in spots:
            if p.get("kind") != "field" or not p.get("index") or not p.get("time"):
                continue
            t = tables.setdefault((p["database"], p["index"]), {"database": p["database"], "table": p["index"],
                                                                "time": p["time"], "owners": {}})
            t["owners"].setdefault(p["name"], cat)
    out = []
    for t in tables.values():
        try:
            msg = _message_field(t["table"])
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            msg = None
        if msg and msg not in t["owners"]:
            out.append({**t, "message": msg})
    return out


def _lines(t: dict[str, Any], days: int, per_day: int, budget: "Budget") -> list[dict[str, Any]]:
    """The latest lines of each hour of each of the last days (of any level, then of the levels that are not
    quiet): the owners' fields, the message, the day."""
    from superset.models.core import Database

    from supagent import tools
    from supagent.knowledge import groups as G

    database = db.session.get(Database, t["database"])
    if database is None:
        return []
    owners = list(t["owners"])
    msg, tf, table = G.name(t["message"]), G.name(t["time"]), G.name(t["table"])
    cols = ", ".join(f'"{G.name(c)}"' for c in owners) + f', "{msg}", "{tf}"'
    lvl = tools._level_field(t["table"])
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    with tools._db_connection(database, extract=False) as conn:
        today = tools._local_now(conn).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        cur = conn.cursor()
        loud: list[str] = []
        if lvl and budget.left():                        # the levels that are not quiet (WARN, ERROR...)
            cur.execute(f'SELECT "{G.name(lvl)}", COUNT(*) AS n FROM "{table}" WHERE "{tf}" >= '
                        f'{G.lit(today - dt.timedelta(days=days))} GROUP BY "{G.name(lvl)}"')
            loud = [str(r[0]) for r in cur.fetchall() if r[0] is not None and str(r[0]).lower() not in tools.LOG_LEVELS_QUIET]
        ons = [""] + ([f' AND "{G.name(lvl)}" IN (' + ", ".join("'" + v.replace("'", "''") + "'" for v in loud) + ")"]
                      if loud else [])
        each = max(1, per_day // SLICES // len(ons))
        step = dt.timedelta(days=1) / SLICES
        for k in range(days):
            day = today - dt.timedelta(days=k + 1)
            for i in range(SLICES):
                a, b = day + step * i, day + step * (i + 1)
                for on in ons:
                    if not budget.left():
                        return out
                    cur.execute(f'SELECT {cols} FROM "{table}" WHERE "{tf}" >= {G.lit(a)} AND "{tf}" < {G.lit(b)} '
                                f'AND "{msg}" IS NOT NULL{on} ORDER BY "{tf}" DESC LIMIT {each}')
                    for row in cur.fetchall():
                        key = (row[-1], str(row[-2]))
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({"owners": dict(zip(owners, row[:-2])), "message": row[-2], "day": f"{day:%Y-%m-%d}",
                                    "at": row[-1]})
    return out


PATTERNS = 8              # patterns stating an interaction read whole (their own lines) ...
PER_PATTERN = 300         # ... this many of their first and as many of their last lines a day (the waits of the
#                           first runs of a night, and of the last ones)


def _pattern_lines(t: dict[str, Any], pieces: list[str], days: int, budget: "Budget") -> list[dict[str, Any]]:
    """The lines of the patterns that state an interaction, read through a piece of their text (a sample of a
    whole table misses a pattern a burst of other lines crowds out): the first and the last of each day."""
    from superset.models.core import Database

    from supagent import tools
    from supagent.knowledge import groups as G

    database = db.session.get(Database, t["database"])
    if database is None or not pieces:
        return []
    owners = list(t["owners"])
    msg, tf, table = G.name(t["message"]), G.name(t["time"]), G.name(t["table"])
    cols = ", ".join(f'"{G.name(c)}"' for c in owners) + f', "{msg}"'
    out: list[dict[str, Any]] = []
    with tools._db_connection(database, extract=False) as conn:
        today = tools._local_now(conn).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        cur = conn.cursor()
        for k in range(days):
            a, b = today - dt.timedelta(days=k + 1), today - dt.timedelta(days=k)
            for piece in pieces:
                like = "'%" + piece.replace("'", "''") + "%'"
                got: dict[tuple[Any, ...], Any] = {}
                for order in ("ASC", "DESC"):
                    if not budget.left():
                        return out
                    cur.execute(f'SELECT {cols}, "{tf}" FROM "{table}" WHERE "{tf}" >= {G.lit(a)} AND "{tf}" < {G.lit(b)} '
                                f'AND "{msg}" LIKE {like} ORDER BY "{tf}" {order} LIMIT {PER_PATTERN}')
                    for row in cur.fetchall():
                        got[(row[-1], str(row[-2]))] = row
                out += [{"owners": dict(zip(owners, row[:-2])), "message": row[-2], "day": f"{a:%Y-%m-%d}", "at": row[-1]}
                        for row in got.values()]
    return out


def stating(lines: list[dict[str, Any]], limit: int = PATTERNS) -> list[str]:
    """The pieces of text of the patterns of these lines that state an interaction and name something (to read
    their lines whole), the most frequent first."""
    from supagent.knowledge import logs as L

    found: Counter = Counter()
    for row in lines:
        s = L.shape(str(row.get("message") or ""))
        if kind_of(s.key) and (s.names or re.search(r"[A-Z][A-Z0-9_]{2,}", s.key)):
            piece = L.fragment(s.key)
            if piece:
                found[piece] += 1
    return [p for p, _n in found.most_common(limit)]


def owner_field(owners: dict[str, str], g: dict[str, Any]) -> str | None:
    """The field of a log table that says which part a line comes from: of its fields of a category, the one of
    the category that acts the most in the System map (the source of its approved interactions: the applications,
    not the pools they run on), the applications first when the map has none."""
    V = g["values"]
    acting = Counter(V[a]["cat"] for a, links in g["out"].items() if links)
    ranked = sorted(owners.items(), key=lambda fc: (-acting.get(fc[1], 0), fc[1] != "application"))
    return ranked[0][0] if ranked else None


def allowed(g: dict[str, Any]) -> tuple[set[tuple[str, str, str]], set[tuple[str, str]]]:
    """What a log line can tie, from the System map: the (category, kind, category) of its interactions (an
    application calls a service, depends on another application...) and their pairs of categories. A kind the
    map has: its own triples only; another kind: the pairs; a map with no interaction yet: anything."""
    V = g["values"]
    triples = {(V[a]["cat"], k, V[b]["cat"]) for a, links in g["out"].items() for k, b, _n in links}
    return triples, {(x, z) for x, _k, z in triples}


def fits(cat_a: str, kind: str, cat_b: str, rules: tuple[set[tuple[str, str, str]], set[tuple[str, str]]]) -> bool:
    triples, cats = rules
    if any(k == kind for _x, k, _z in triples):
        return (cat_a, kind, cat_b) in triples
    return not cats or (cat_a, cat_b) in cats


def pairs(lines: list[dict[str, Any]], owner: str, g: dict[str, Any]) -> dict[tuple[int, str, int], dict[str, Any]]:
    """The interactions the lines show: {(owner value, kind, named value): {"lines", "days", "example",
    "pattern"}}, a pair counted once per line. `owner`: the field of the part a line comes from."""
    from supagent.knowledge import logs as L
    from supagent.knowledge.brief import named as named_in

    V, names = g["values"], g["names"]
    rules = allowed(g)
    found: dict[tuple[int, str, int], dict[str, Any]] = {}
    for row in lines:
        text = str(row.get("message") or "")
        s = L.shape(text)
        kind = kind_of(s.key)
        if kind is None:
            continue
        value = (row.get("owners") or {}).get(owner)
        mine = {resolve(str(value), names)} - {None} if value not in (None, "") else set()
        if not mine:
            continue
        named: set[int] = set(named_in(text, g))          # the map's names written as words (LEDGERDB) ...
        for place in s.names:                              # ... and the identifiers that start with one (A.EU.02)
            for name in re.split(r",\s*", place):
                i = resolve(name, names)
                if i is not None:
                    named.add(i)
        seen: set[tuple[int, str, int]] = set()
        for a in mine:
            for b in named - mine:
                if b in V[a]["parents"] or a in V[b]["parents"]:
                    continue                     # one is part of the other: no interaction
                if not fits(V[a]["cat"], kind, V[b]["cat"], rules):
                    continue                     # (a pool named in a line of a call: not what it calls)
                key = (a, kind, b)
                if key in seen:
                    continue
                seen.add(key)
                f = found.setdefault(key, {"lines": 0, "days": set(), "example": str(row["message"])[:EXAMPLE_CHARS],
                                           "pattern": s.key[:EXAMPLE_CHARS]})
                f["lines"] += 1
                f["days"].add(row.get("day"))
    return found


def run(seconds: float = 300.0, days: int = DAYS, per_day: int = PER_DAY, propose: bool = True) -> dict[str, Any]:
    """The log tables read (see the module), the pairs they show proposed (To review). Returns its counts;
    `propose` False: nothing written, every pair seen listed under "seen" (a check of what it would find)."""
    from supagent.knowledge.brief import _graph
    from supagent.knowledge.stopping import check
    from supagent.models import Link

    t0 = time.time()
    budget = Budget(seconds)
    out: dict[str, Any] = {"tables": 0, "lines": 0, "proposed": 0}
    g = _graph()
    if len(g["values"]) < 2:
        return out
    every: dict[tuple[int, str, int], dict[str, Any]] = {}
    where: dict[tuple[int, str, int], str] = {}
    for t in log_tables():
        if budget.cut or time.time() >= budget.until:
            budget.cut = True
            out["left"] = True
            break
        check()
        try:
            lines = _lines(t, days, per_day, budget)
        except Exception as ex:  # pylint: disable=broad-except   (one table not read: the others are)
            db.session.rollback()
            log.info("supagent linkfinder: %s not read (%s)", t["table"], str(ex)[:200])
            out.setdefault("not_read", []).append(t["table"])
            continue
        try:                                     # the patterns that state an interaction, read whole
            seen = {(r.get("at"), str(r.get("message"))) for r in lines}
            lines += [r for r in _pattern_lines(t, stating(lines), days, budget)
                      if (r.get("at"), str(r.get("message"))) not in seen]
        except Exception as ex:  # pylint: disable=broad-except   (the sample alone)
            db.session.rollback()
            log.info("supagent linkfinder: %s: the patterns not read whole (%s)", t["table"], str(ex)[:200])
        out["tables"] += 1
        out["lines"] += len(lines)
        owner = owner_field(t["owners"], g)
        for key, f in (pairs(lines, owner, g) if owner else {}).items():
            have = every.get(key)
            if have is None or f["lines"] > have["lines"]:
                every[key], where[key] = f, t["table"]
    out["queries"] = budget.queries
    if budget.cut:                                   # (what was read is used; the rest at the next run)
        out["cut"] = f"the step's time ({seconds:g} s) ran out: the queries left were not sent"
    V = g["values"]
    if not propose:
        out["seen"] = [{"from": V[a]["name"], "kind": kind, "to": V[b]["name"], "lines": f["lines"],
                        "days": len(f["days"]), "enough": f["lines"] >= LEAST_LINES and len(f["days"]) >= LEAST_DAYS}
                       for (a, kind, b), f in sorted(every.items(), key=lambda kv: -kv[1]["lines"])]
        out["seconds"] = round(time.time() - t0, 1)
        return out
    for (a, kind, b), f in sorted(every.items(), key=lambda kv: -kv[1]["lines"]):
        if out["proposed"] >= PROPOSED:
            break
        if f["lines"] < LEAST_LINES or len(f["days"]) < LEAST_DAYS:
            continue
        a_ref, b_ref = f"facet:{a}", f"facet:{b}"
        if db.session.query(Link.id).filter(Link.a_ref == a_ref, Link.b_ref == b_ref, Link.kind == kind).first():
            continue                             # drawn, proposed or rejected already
        db.session.add(Link(a_ref=a_ref, b_ref=b_ref, kind=kind, confidence=0.8, source="logs", status="proposed",
                            evidence=(f"{f['lines']} lines of {where[(a, kind, b)]} on {len(f['days'])} of the last "
                                      f"{days} days, such as \"{f['example']}\"")))
        out["proposed"] += 1
        log.info("supagent linkfinder: proposed %s %s %s", V[a]["name"], kind, V[b]["name"])
    db.session.commit()
    out["seconds"] = round(time.time() - t0, 1)
    return out
