"""What the logs say that they do not usually: the messages of a log table grouped into patterns, each pattern
counted against the same window of the earlier days (compare_logs).

A pattern is a message with what changes from one line to the next left out: the numbers, the times, the ids
(`GC overhead on node105: heap 97% used` and `... node107: heap 95% used` are one pattern), the names written
as identifiers (`no free slot on GRID_EU_STD for RISKCALC.RATES.EMEA.01`, a list of them as one) and the
quoted values. What a pattern leaves out is kept by its place: the names (a name that most of its lines hold is
written back when the pattern is said: `no free slot on GRID_EU_STD for <name>`), the numbers (a slow
write of 40 s tonight where it is 8 s every night). Nothing here knows a kind of system or of log: a table, a time
field, a message field, a level field when there is one, and the fields of few values that say where a line comes
from."""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from typing import Any, NamedTuple

NEW_LINES = 5                  # a pattern that is new must have this many lines at least to be said ...
NEW_ERRORS = 2                 # ... an error, this many (errors are rarer, and each one counts)
ERRORS = re.compile(r"^(error|err|fatal|critical|crit|severe|alert|emerg\w*)$", re.I)
# an error said in its words, for a table without a level field
ERROR_WORDS = re.compile(r"\b(error|exception|fatal|failed|failure|killed|refused|timed? ?out|traceback|panic)\b", re.I)
HIGH = 2.0                     # ... a pattern that is there every day, this many times its usual
NOISE = 3.0                    # ... and beyond this many square roots of its usual (counts differ day to day)
GONE_DAYS = 0.75               # a pattern gone: there on this share of the earlier days at least, and none now
SHOWN = 6                      # patterns said in the conclusion
WORDINGS = 3                   # wordings of a pattern kept
WHERE_VALUES = 4               # values of a field said for where a pattern comes from
HELD = 0.8                     # a name most lines of a pattern hold (this share), written back when it is said
EXAMPLE_CHARS = 240
VALUES = 4                     # numbers of a line compared by their place (a duration, a size, a count)
VALUE_HIGH = 2.0               # a pattern as frequent as usual whose numbers are this many times their usual ...
VALUE_FEW = 2.5                # ... when they are known from fewer than half the earlier days
WORDS = 1                      # a pattern keeps its names when it has fewer words than this (`<name>: <name>`)

_NUMBER = re.compile(r"[-+]?\d+(?:[.,:]\d+)*(?:[eE][-+]?\d+)?%?")
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b|\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
                   r"|\b\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?\b")
# a quoted value, or a run of the characters a name is written with
_SPAN = re.compile(r"'[^'\s][^']{0,79}'|\"[^\"\s][^\"]{0,79}\"|[\w./#-]+")
_UNIT = re.compile(r"[-+]?\d+(?:[.,]\d+)?[A-Za-z%µ]{1,5}")          # 120ms, 4GB, 3x: a number with its unit
_COMPOUND = re.compile(r"[a-z]+(?:-[a-z]+)+")                        # out-of-memory, re-run: words
# the places of a pattern: a quoted value (\x01), a name or a list of names (\x00, joined by , ; / & and or),
# a list cut at the end of the line with the piece of name it ends with
_PLACES = re.compile(r"(\x01|\x00(?:(?:\s*[,;/&+]\s*|\s+(?:and|or)\s+)\x00)*(?:\s*[,;/&+]\s*[A-Z0-9_.-]+$)?)")
_WORD = re.compile(r"[A-Za-z]{3,}")


class Shape(NamedTuple):
    wording: str              # the message with its numbers, times and ids left out (#), its names kept
    key: str                  # the pattern: its names (<name>) and quoted values ('<value>') left out too
    names: list[str]          # what the pattern leaves out, by place: the names, the quoted values
    numbers: list[float]      # the numbers of the line (not those of its names), in their order


def _is_name(token: str) -> bool:
    """A token written as an identifier: letters with digits, underscores, or dots or dashes inside (srv-12,
    POOL_A, PNL_APP.RATES.EMEA.01, java.lang.OutOfMemoryError); not a number with its unit (120ms), not words
    joined by dashes (out-of-memory), not a path of plain words (/nas/risk: said as it is)."""
    if not re.search(r"[A-Za-z]", token) or _UNIT.fullmatch(token) or _COMPOUND.fullmatch(token):
        return False
    return bool(re.search(r"[\d_#]|[A-Za-z0-9][.-][A-Za-z0-9]", token))


def _number(text: str) -> float | None:
    try:
        return float(text.rstrip("%").replace(",", ".").split(":")[0])
    except ValueError:
        return None


def shape(message: str) -> Shape:
    """A message's wording, pattern, names and numbers (see Shape)."""
    text = " ".join(str(message or "").split())[:600]
    text = _HEX.sub("#", _TIME.sub("#", text))
    wording, key, names, nums = [], [], [], []
    at = 0

    def plain(part: str) -> None:
        for m in _NUMBER.finditer(part):
            v = _number(m.group(0))
            if v is not None:
                nums.append(v)
        masked = _NUMBER.sub("#", part)
        wording.append(masked)
        key.append(masked)

    for m in _SPAN.finditer(text):
        token = m.group(0)
        if token[0] in "'\"":
            plain(text[at:m.start()])
            wording.append(token)
            key.append("\x01")
            names.append(token[1:-1])
            at = m.end()
            continue
        core = token.strip("./-")
        if not core or not _is_name(core):
            continue                                   # (said with the text around it)
        lead = token.index(core)
        plain(text[at:m.start() + lead])
        wording.append(core)
        key.append("\x00")
        names.append(core)
        at = m.start() + lead + len(core)
    plain(text[at:])
    # a list of names is one name, however many it has (its names joined as written); a list cut at the end of a
    # line (a line written up to a length) keeps the piece of name it ends with
    rebuilt, merged, k = [], [], 0
    for part in _PLACES.split("".join(key)):
        if part == "\x01":
            rebuilt.append("'<value>'")
            merged.append(names[k])
            k += 1
        elif part.startswith("\x00"):
            n = part.count("\x00")
            cut = None if part.endswith("\x00") else re.search(r"[,;/&+]\s*([\w.-]+)$", part)
            merged.append(", ".join(names[k:k + n]) + (f", {cut.group(1)}" if cut else ""))
            rebuilt.append("<name>")
            k += n
        elif part:
            rebuilt.append(part)
    words = "".join(wording)
    key_text = "".join(rebuilt).rstrip(" .,;:")
    if merged and len(_WORD.findall(re.sub(r"<name>|'<value>'", " ", key_text))) < WORDS:
        return Shape(words, words.rstrip(" .,;:"), [], nums)     # (only names: the names are what it says)
    return Shape(words, key_text, merged, nums)


def wording(message: str) -> str:
    """A message with its numbers, times and ids left out (`#`): the wording of a pattern, its names kept."""
    return shape(message).wording


def pattern(message: str) -> str:
    """A message's pattern: its wording with the names written as identifiers left out too (`<name>`, a list of
    them as one), and the quoted values (`'<value>'`)."""
    return shape(message).key


def numbers(message: str) -> list[float]:
    """The numbers of a message, in their order (a duration, a size, a count...), not those of its names."""
    return shape(message).numbers


def read(samples: list[list[dict[str, Any]]], totals: list[float], message: str,
         fields: list[str], level: str | None = None) -> dict[str, dict[str, Any]]:
    """The patterns of each window: {pattern: {"counts": [estimated lines per window], "wordings": Counter,
    "example": str, "where": {field: Counter}, "names": {place: Counter}, "values": {place: [[numbers] per
    window]}, "first", "last", "level"}}. `samples[i]`: rows read of window i (a message, the fields, "_t" its
    time), `totals[i]`: the lines of window i the sample stands for (more than its rows when the window has more
    lines than were read). Window 0 is the one asked: where, when and the names are said of it."""
    out: dict[str, dict[str, Any]] = {}
    for i, rows in enumerate(samples):
        scale = (totals[i] / len(rows)) if rows and totals[i] > len(rows) else 1.0
        for row in rows:
            msg = str(row.get(message) or "")
            if not msg.strip():
                continue
            s = shape(msg)
            p = out.get(s.key)
            if p is None:
                p = out[s.key] = {"counts": [0.0] * len(samples), "wordings": Counter(), "example": msg[:EXAMPLE_CHARS],
                                  "where": defaultdict(Counter), "names": defaultdict(Counter), "first": None,
                                  "last": None, "read": 0,
                                  "level": level if level is not None else ("ERROR" if ERROR_WORDS.search(msg) else None),
                                  "values": defaultdict(lambda: [[] for _ in samples])}
            p["counts"][i] += scale
            for k, v in enumerate(s.numbers[:VALUES]):    # each number of the line by its place
                p["values"][k][i].append(v)
            if i == 0:                                    # where and when: the window asked
                p["read"] += 1
                p["wordings"][s.wording] += 1
                for k, name in enumerate(s.names):
                    p["names"][k][name] += 1
                for f in fields:
                    if row.get(f) not in (None, ""):
                        p["where"][f][str(row[f])] += 1
                t = row.get("_t")
                if t is not None:
                    p["first"] = t if p["first"] is None or t < p["first"] else p["first"]
                    p["last"] = t if p["last"] is None or t > p["last"] else p["last"]
    return out


def merge(into: dict[str, dict[str, Any]], part: dict[str, dict[str, Any]]) -> None:
    """The patterns of one level added to those of the others (a pattern of several levels: its worst)."""
    for key, p in part.items():
        q = into.get(key)
        if q is None:
            into[key] = p
            continue
        q["counts"] = [x + y for x, y in zip(q["counts"], p["counts"])]
        if ERRORS.match(str(p.get("level") or "")):
            q["level"] = p["level"]
        q["read"] += p["read"]
        q["wordings"].update(p["wordings"])
        for f, c in p["where"].items():
            q["where"][f].update(c)
        for k, c in p["names"].items():
            q["names"][k].update(c)
        for k, per in p["values"].items():
            mine = q["values"][k]
            for i, vs in enumerate(per):
                mine[i].extend(vs)
        for edge, better in (("first", min), ("last", max)):
            if p.get(edge) is not None:
                q[edge] = p[edge] if q.get(edge) is None else better(q[edge], p[edge])


def said(key: str, p: dict[str, Any]) -> str:
    """A pattern as it is said: each name most of its lines hold written back (`no free slot on GRID_A for
    <name>`), the others left as <name>."""
    names = p.get("names") or {}
    total = p.get("read") or 0
    k = -1

    def back(m: re.Match) -> str:
        nonlocal k
        k += 1
        top = names.get(k).most_common(1) if names.get(k) else []
        if top and total and top[0][1] >= HELD * total:
            return f"'{top[0][0]}'" if m.group(0) == "'<value>'" else top[0][0]
        return m.group(0)

    return re.sub(r"<name>|'<value>'", back, key)


def fragment(key: str) -> str | None:
    """The longest piece of a pattern that every line of it holds word for word (no number, no name), to count
    its lines in the database; None when no piece is long enough to tell it from other lines."""
    parts = re.split(r"<name>|'<value>'|#", key)
    best = max((x.strip(" :,;()[]") for x in parts), key=len, default="")
    return best if len(best) >= 12 and "%" not in best and "_" not in best and "\\" not in best else None


def judge(p: dict[str, Any], days: int, dates: list[str] | None = None) -> dict[str, Any]:
    """A pattern now against its usual (the median of the earlier windows): new, above usual, rare (there on
    fewer than half the earlier days: a weekly job, a decoy, or the start of something), as usual, below usual or
    gone. An error counts from fewer lines than a warning. `dates`: the earlier windows' days, to say when a rare
    pattern was there."""
    now = p["counts"][0]
    earlier = p["counts"][1:days + 1]
    usual = statistics.median(earlier) if earlier else 0.0
    seen_on = sum(1 for c in earlier if c > 0)
    least = NEW_ERRORS if ERRORS.match(str(p.get("level") or "")) else NEW_LINES
    verdict = "as usual"
    if not seen_on and now >= least:
        verdict = "new"
    elif usual and now >= HIGH * usual and now - usual > NOISE * math.sqrt(usual) and now - usual >= least:
        verdict = "above usual"
    elif not usual and seen_on and now >= least:
        verdict = "rare"
    elif usual >= NEW_LINES and not now and seen_on >= GONE_DAYS * len(earlier):
        verdict = "gone"
    elif usual and now <= usual / HIGH and usual - now > NOISE * math.sqrt(usual):
        verdict = "below usual"
    out = {"now": round(now), "usual": round(usual, 1), "earlier_days_with_it": seen_on, "verdict": verdict}
    if verdict == "rare":
        out["seen_on"] = {(dates[i] if dates and i < len(dates) else f"day -{i + 1}"): round(c)
                          for i, c in enumerate(earlier) if c > 0}
    moved = numbers_moved(p, days)
    if moved:                             # as many lines, but what they say is far from what they say every day
        out["numbers"] = moved
        if verdict == "as usual":
            out["verdict"] = "numbers above usual" if moved[0]["ratio"] >= 1 else "numbers below usual"
    return out


def numbers_moved(p: dict[str, Any], days: int) -> list[dict[str, Any]]:
    """The numbers of a pattern (by their place in the line) whose median now is far from their usual median:
    the same slow I/O warning every night, but 45 s tonight against 9 s usually. Known from fewer than half the
    earlier days (a weekly line), it must be further from it."""
    out = []
    for place, per_window in sorted((p.get("values") or {}).items()):
        now = per_window[0]
        earlier = [statistics.median(w) for w in per_window[1:days + 1] if len(w) >= 3]
        if len(now) < 5 or not earlier:
            continue
        a, b = statistics.median(now), statistics.median(earlier)
        if not b or not a or (a > 0) != (b > 0):
            continue
        ratio = a / b
        bar = VALUE_HIGH if len(earlier) >= max(2, days // 2) else VALUE_FEW
        if ratio >= bar or ratio <= 1 / bar:
            out.append({"place": place + 1, "now": round(a, 2), "usual": round(b, 2), "ratio": round(ratio, 2),
                        "days": len(earlier)})
    return sorted(out, key=lambda x: -max(x["ratio"], 1 / x["ratio"]))


def number_label(key: str, place: int) -> str:
    """A number of a pattern by the words around it ("after [#] min", "waiting, [#] of # slots"), its place in
    the line being no name a reader knows."""
    words = key.split()
    seen = 0
    for i, w in enumerate(words):
        n = w.count("#")
        if seen + n >= place:
            k = place - seen                           # (the k-th # of this word)
            parts = w.split("#")
            marked = "#".join(parts[:k]) + "[#]" + "#".join(parts[k:])
            return " ".join(words[max(0, i - 1):i] + [marked] + words[i + 1:i + 4])
        seen += n
    return f"#{place}"


def where(p: dict[str, Any], fields: list[str]) -> list[str]:
    """Where a pattern's lines of the window come from: for each field of few values, its values when they are a
    few ("HOST: node105 (60), node106 (55)"); a field whose lines are spread over many values says nothing.
    The names of the message too, by their place, when they are a few and not one (one is written back), and not
    the values of a field already said. Counts read from a part of the lines are scaled to the lines counted."""
    out = []
    read = p.get("read") or sum(p["wordings"].values()) or 1
    now = (p.get("counts") or [read])[0] or read
    counted = p.get("where_counted") or set()
    said_values: list[set[str]] = []

    def few(c: Counter, total: float, scale: float) -> str | None:
        top = c.most_common(WHERE_VALUES)
        held = sum(n for _v, n in top)
        if len(c) <= WHERE_VALUES or held >= HELD * total:
            return ", ".join(f"{v} ({round(n * scale)})" for v, n in top) + \
                (f" and {len(c) - WHERE_VALUES} more" if len(c) > WHERE_VALUES else "")
        return None

    for f in fields:
        c = p["where"].get(f)
        if not c:
            continue
        exact = f in counted
        text = few(c, sum(c.values()) if exact else read, 1.0 if exact else max(1.0, now / read))
        if text:
            out.append(f"{f}: {text}")
            said_values.append(set(c))
    for k, c in sorted((p.get("names") or {}).items()):
        top = c.most_common(1)
        if len(c) < 2 or (top and top[0][1] >= HELD * read) or any(set(c) <= v for v in said_values):
            continue                                   # (one name, or the values of a field said: said once)
        text = few(c, read, max(1.0, now / read))
        if text:
            out.append(f"name #{k + 1}: {text}")
    return out


def line(key: str, p: dict[str, Any], j: dict[str, Any], fields: list[str], refs: bool = False) -> str:
    seen = ", ".join(f"{d} with {n}" for d, n in list((j.get("seen_on") or {}).items())[:3])
    said_now = {"new": f"{j['now']} lines, none on the earlier days",
                "above usual": f"{j['now']} lines against {j['usual']:g} usually",
                "rare": f"{j['now']} lines, there on only {j['earlier_days_with_it']} of the earlier days ({seen})",
                "gone": f"none now against {j['usual']:g} usually",
                "below usual": f"{j['now']} lines against {j['usual']:g} usually",
                "as usual": f"{j['now']} lines, as usual",
                "numbers above usual": f"{j['now']} lines, as many as usually",
                "numbers below usual": f"{j['now']} lines, as many as usually"}[j["verdict"]]
    for m in (j.get("numbers") or [])[:1]:
        said_now += (f"; its number \"{number_label(key, m['place'])}\" is {m['now']:g} against {m['usual']:g} "
                     + ("usually" if m.get("days", 2) >= 2 else "the day it was there") + f" (x{m['ratio']:g})")
    for ref, n in list((p.get("on") or {}).items())[:3] if refs else []:      # (the reference days asked)
        said_now += f"; {ref}: {n:g}"
    text = f"\"{said(key, p)[:200]}\" ({(p.get('level') + ', ') if p.get('level') else ''}{said_now}"
    if p.get("first") and p.get("last"):
        text += f", from {p['first']:%H:%M} to {p['last']:%H:%M}"
    text += ")"
    places = where(p, fields)
    if places:
        text += ": " + "; ".join(places)
    return text


OFF = ("new", "above usual", "rare", "numbers above usual")


def survey(found: dict[str, dict[str, Any]], days: int, fields: list[str], dates: list[str] | None = None,
           refs: bool = False) -> dict[str, Any]:
    """What the logs say against their usual: the patterns that are new, far above usual, rare or with numbers far
    from usual (the errors first, then the most lines), what is gone, and the patterns of every day (said once,
    as usual). `refs`: the reference days asked, said in the conclusion too."""
    judged = [(k, p, judge(p, days, dates)) for k, p in found.items()]
    for k, _p, j in judged:
        for m in j.get("numbers") or []:
            m["label"] = number_label(k, m["place"])
    off = [x for x in judged if x[2]["verdict"] in OFF]
    off.sort(key=lambda x: (not ERRORS.match(str(x[1].get("level") or "")), x[2]["verdict"] == "numbers above usual",
                            -x[2]["now"]))
    gone = sorted((x for x in judged if x[2]["verdict"] == "gone"), key=lambda x: -x[2]["usual"])
    usual = sorted((x for x in judged if x[2]["verdict"] == "as usual" and x[2]["now"] >= NEW_LINES),
                   key=lambda x: -x[2]["now"])
    patterns = []
    for k, p, j in (off + gone)[:SHOWN] + usual[:3]:
        patterns.append({"pattern": said(k, p)[:200], **({"level": p["level"]} if p.get("level") else {}), **j,
                         "wordings": [w[:200] for w, _n in p["wordings"].most_common(WORDINGS)],
                         "where": where(p, fields), "example": p["example"],
                         **({"on": p["on"]} if p.get("on") else {}),
                         **({"counted": "line by line"} if p.get("exact") else {}),
                         **({"from": f"{p['first']:%Y-%m-%d %H:%M}", "to": f"{p['last']:%Y-%m-%d %H:%M}"}
                            if p.get("first") and p.get("last") else {})})
    if off:
        text = "What the logs say that they do not usually (the errors first, then the most lines): " + " ".join(
            f"({i + 1}) {line(k, p, j, fields, refs)}." for i, (k, p, j) in enumerate(off[:SHOWN]))
        if len(off) > SHOWN:
            text += f" And {len(off) - SHOWN} more of fewer lines."
    else:
        text = "Nothing new in the logs: every pattern of the window is there on the earlier days as much."
    if gone:
        text += " Gone (there every day, none now): " + "; ".join(
            f"\"{said(k, p)[:120]}\" ({j['usual']:g} usually)" for k, p, j in gone[:3]) + "."
    if usual:
        text += " As every day: " + "; ".join(f"\"{said(k, p)[:100]}\" ({j['now']} lines)" for k, p, j in usual[:3]) + "."
    return {"conclusion": text, "patterns": patterns, "new_or_more": len(off)}
