"""What an investigation blames, checked against the system (0.9).

An investigation's answer that names a part of the system as the cause (a server, a pool, a service, a feed...)
must tie it to what the question is about. The check is plain: the part is tied when the system map links it to
the question's parts within two steps (what they run on, depend on, read, call, consist of, are part of), or when
a result of the answer's own queries on those parts shows it. A part that is neither (servers of another pool
that had an alert that night) is sent back once, then marked: the cause of somebody else's problem.

It judges nothing else: whether a tied part really is the cause stays the investigation's own work.
"""

from __future__ import annotations

import json
import re
from typing import Any

BLAME = re.compile(r"\b(caus\w*|because|due to|root cause|explain\w*|responsible|led to|leads? to|driven by|culprit|"
                   r"result(?:s|ed)? (?:of|from)|origin\w*|trigger\w*|slow(?:s|ed|ing)? (?:down )?|"
                   r"[àa] cause|provoqu\w*|responsable|en raison|d[ûu] [àa])\b", re.I)
RULED = re.compile(r"\b(not|no|n't|unrelated|ruled out|rule[sd]? out|without|normal|unaffected|independent\w*|"
                   r"coincid\w*|separate\w*|unlikely|however|but|although|though|cannot|pas|aucun\w*|sans|"
                   r"hors de cause|[ée]cart[ée]e?s?)\b", re.I)
HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$|[^.!?]{3,80}:\s*$)")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
RANGE = re.compile(r"\b([A-Za-z][A-Za-z_\-]*?)(\d+)\s*(?:-|–|to|through|à)\s*(?:\1)?(\d+)\b")
HOPS = 2
QUERY_TOOLS = ("execute_sql", "compare_groups", "compare_logs", "export_excel")


def _expanded(line: str) -> str:
    """"srv-901-904", "srv101 to srv104": the names of the range, written out (at most 20)."""
    def names(m: re.Match) -> str:
        prefix, a, b = m.group(1), m.group(2), m.group(3)
        if len(b) < len(a):
            b = a[:len(a) - len(b)] + b
        lo, hi = int(a), int(b)
        if not 0 < hi - lo <= 20:
            return m.group(0)
        return ", ".join(f"{prefix}{i:0{len(a)}d}" for i in range(lo, hi + 1))

    return RANGE.sub(names, line)


def blaming_lines(answer: str) -> list[str]:
    """The lines of an answer that say what the cause is: a sentence with a word of cause and none that rules
    out, and the items listed under a heading or a lead-in that says so ("Root cause:", "driven by:")."""
    out: list[str] = []
    under = False
    for raw in (answer or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if HEADING.match(line) and not ITEM.match(line):
            under = bool(BLAME.search(line)) and not RULED.search(line)
            continue
        if under and not RULED.search(line):
            out.append(line)
            continue
        for s in re.split(r"(?<=[.!?])\s+", line):
            if BLAME.search(s) and not RULED.search(s):
                out.append(s)
    return out


def reachable(start: list[int], g: dict[str, Any], hops: int = HOPS) -> set[int]:
    """The values within `hops` steps of these on the system map, whatever the way (it depends on them or they
    on it, it is part of them or they of it)."""
    V, kids = g["values"], g["children"]
    seen, level = set(start), list(start)
    for _ in range(hops):
        nxt: list[int] = []
        for i in level:
            near = [b for _k, b, _n in g["out"].get(i, [])] + [a for _k, a, _n in g["in"].get(i, [])]
            near += list(V[i]["parents"]) + list(kids.get(i, []))
            for j in near:
                if j in V and j not in seen:
                    seen.add(j)
                    nxt.append(j)
        level = nxt
    return seen


def untied(question: str, answer: str, trace: list[dict]) -> tuple[list[str], str]:
    """(the parts the answer blames that nothing ties to the question's parts, the question's parts by name);
    ([], "") when the question names no part, or the answer blames none."""
    from supagent.knowledge.brief import _graph, named

    g = _graph()
    V = g["values"]

    def bare(i: int) -> bool:
        """A word of the map that is no part of the system: no description, part of nothing, nothing part of it,
        no interaction (a category of the catalog kept as a subject: "Batch", "memory", "cache"). Nothing can
        tie it or fail to."""
        v = V[i]
        return not (v.get("about") or v.get("parents") or g["children"].get(i) or g["out"].get(i) or g["in"].get(i))

    seeds = [i for i in named(question, g) if not bare(i)]
    if not seeds:
        return [], ""
    actors = list(seeds)
    for s in seeds:
        actors += [i for i in g["children"].get(s, []) if i not in actors]
    blamed: list[int] = []
    for line in blaming_lines(answer):
        for i in named(_expanded(line), g):
            if i not in blamed and not bare(i):
                blamed.append(i)
    if not blamed:
        return [], ""
    near = reachable(actors, g)
    scope = {V[i]["name"].lower() for i in actors}
    shown = ""                                           # what the answer's queries on those parts returned
    for t in trace:
        if t.get("status") != "done" or (t.get("called") or t.get("tool")) not in QUERY_TOOLS:
            continue
        asked = json.dumps(t.get("args") or {}, default=str).lower()
        if any(n in asked for n in scope):
            shown += "\n" + str(t.get("full") or t.get("result") or "")
    names = []
    for i in blamed:
        name = V[i]["name"]
        if i in near or re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", shown, re.I):
            continue
        names.append(name)
    return (names, ", ".join(V[s]["name"] for s in seeds[:3])) if names else ([], "")
