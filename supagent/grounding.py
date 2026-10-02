"""Are the numbers of an answer in what the LLM was given? Every number of the answer's text (not
its code, links, dates or times) must be a number the tools returned, or one of the question, of
the chat or of the knowledge given with it: as written, rounded as the answer shows it, converted
(a share as a percentage, seconds in minutes or hours, bytes in kB/MB/GB), a total of a column,
the total of its first rows, a row count, or a rate within a row (a / b as a percentage). Other
numbers are the model's own: it is asked once to take them from a query, then they are marked."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from supagent.dates import MONTH

SKIP = [re.compile(p, re.S | re.I) for p in (
    r"```.*?```", r"`[^`\n]*`", r"\]\([^)]*\)", r"https?://\S+", r"/[\w./?=&%#-]+",   # code, links, paths
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b", r"\b\d{1,2}[:h]\d{2}(?::\d{2})?\b",   # dates, times
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",
    rf"\b\d{{1,2}}(?:st|nd|rd|th|er)?\s+(?:of\s+)?{MONTH}",                        # 23 September, not 5 markets
    rf"\b{MONTH}\s+\d{{1,2}}\b",
    r"\b(?:19|20)\d{2}\b",                                                          # years
    r"\b[A-Za-z]+[-_][A-Za-z0-9_-]*\d[\w-]*\b", r"\b[A-Za-z]+\d+[\w-]*\b",          # names: srv-amer-002, p95, W-1
    r"\b[DWMY][-+]\d+\b", r"\bid\s*[:#]?\s*\d+\b", r"#\d+\b",
    r"\*?First \d+ of \d+ rows[^\n]*",                                            # the page's own note
)]
COMPOUND = re.compile(r"\b(\d+)\s*(h|hours?|heures?|min|minutes?|mn)\s*(?:and|et|,)?\s*(\d+)\s*"
                      r"(min|minutes?|mn|s|sec|secs|seconds?|secondes?)\b", re.I)
UNIT_S = {"h": 3600, "m": 60, "s": 1}
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\u202f\u00a0' ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
                    r"(\s?(?:%|k\b|K\b|M\b|million|millions|thousand|milliers?|bn\b|billion|milliards?))?"
                    r"(?!\+)")                     # "12+ metrics": a count said as at least, not a figure
TIME = re.compile(r"(?:\d{4}-\d{2}-\d{2}[ T])?(\d{1,2}):(\d{2})(?::(\d{2}))?")
SCALES = {"k": 1e3, "K": 1e3, "thousand": 1e3, "millier": 1e3, "milliers": 1e3, "M": 1e6, "million": 1e6,
          "millions": 1e6, "bn": 1e9, "billion": 1e9, "milliard": 1e9, "milliards": 1e9}
CONVERSIONS = (1.0, 100.0, 1 / 60, 1 / 3600, 1 / 86400, 1e-3, 1e-6, 1e-9, 1 / 1024, 1 / 1024 ** 2,
               1 / 1024 ** 3)             # as is, a share in %, seconds in minutes / hours / days, bytes in kB...GB
MAX_ROWS = 300
APPROX = re.compile(r"\b(about|around|roughly|approximately|approx|nearly|almost|some|over|under|more than|less than|"
                    r"environ|pr[èe]s de|presque|autour de|plus de|moins de)\b|~|≈", re.I)
UNIT_CONSTANTS = (60.0, 24.0, 1440.0, 1024.0, 3600.0, 86400.0, 1024.0 ** 2, 1024.0 ** 3)   # said when converting
# ("one hour (60 minutes)", "a 24-hour target", bytes per GiB...): as written only
RATE_ROWS = 50           # rates within a row: of the first rows only (more would make any number "found")


def _values(text: str) -> list[float]:
    out = []
    for m in NUMBER.finditer(text or ""):
        out += [v for v, _tol in _readings(m.group(1), m.group(2))]
    return out


def _readings(raw: str, unit: str | None) -> list[tuple[float, float]]:
    """(value, tolerance) of a number as written; "1,234" is 1234 (en) or 1.234 (fr)."""
    unit = (unit or "").strip()
    scale = SCALES.get(unit, 1.0)
    texts = []
    if re.fullmatch(r"\d{1,3}(?:[\u202f\u00a0' ]\d{3})+(?:[.,]\d+)?", raw):
        texts.append(re.sub(r"[\u202f\u00a0' ]", "", raw).replace(",", "."))
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        texts.append(raw.replace(",", ""))
        if raw.count(",") == 1 and "." not in raw:
            texts.append(raw.replace(",", "."))
    else:
        texts.append(raw.replace(",", "."))
    out = []
    for t in texts:
        try:
            v = float(t)
        except ValueError:
            continue
        decimals = len(t.split(".")[1]) if "." in t else 0
        out.append((v * scale, 0.5 * 10 ** -decimals * scale + 1e-9))
    return out


def answer_numbers(answer: str) -> list[tuple[str, list[tuple[float, float]]]]:
    """The numbers of the answer's text to check: (as written, its readings). A duration in two
    units ("4 minutes and 53 seconds") is one number, in seconds; a duration between two times of
    the same line ("from 02:30 to 05:30 (180 minutes)") is the answer's own subtraction: not checked."""
    text = answer or ""
    for rx in SKIP[:2]:                                # code first: its times are not the line's
        text = rx.sub(" ", text)
    text = "\n".join(_without_spans(line) for line in text.split("\n"))
    compounds = []

    def compound(m: re.Match) -> str:
        a, ua, b, ub = int(m.group(1)), m.group(2).lower(), int(m.group(3)), m.group(4).lower()
        seconds = a * UNIT_S["h" if ua.startswith("h") else "m"] + b * UNIT_S["m" if ub.startswith("m") else "s"]
        tol = 1.0 if ub.startswith("s") else 30.0
        compounds.append((m.group(0), [(float(seconds), tol), (seconds / 60, tol / 60), (seconds / 3600, tol / 3600)]))
        return " "

    text = COMPOUND.sub(compound, text)
    for rx in SKIP[2:]:
        text = rx.sub(" ", text)
    out = list(compounds)
    for m in NUMBER.finditer(text):
        readings = _readings(m.group(1), m.group(2))
        if not readings:
            continue
        digits = re.sub(r"[^\d.]", "", m.group(1))
        zeros = len(digits) - len(digits.rstrip("0")) if "." not in digits else 0
        if zeros and APPROX.search(text[max(0, m.start() - 25):m.start()]):   # "roughly 12,300": to the hundred
            readings = [(v, max(t, 0.5 * 10 ** zeros) if v >= 10 ** zeros and v % 10 ** zeros == 0 else t)
                        for v, t in readings]
        unit = (m.group(2) or "").strip()
        if unit != "%" and all(v < 10 and float(v).is_integer() for v, _t in readings):
            continue                                   # 1 to 9: counts of what is listed, ranks
        out.append((m.group(0).strip(), readings))
    return out


def _without_spans(line: str) -> str:
    """A line without the durations between its times (02:30 ... 05:30 ... 180 min, 3 h)."""
    stamps = [_minutes(m.group(0)) for m in TIME.finditer(line)]
    stamps = [x for x in stamps if x is not None][:6]
    spans = {abs(a - b) for a in stamps for b in stamps if a != b}
    if not spans:
        return line

    def keep(m: re.Match) -> str:
        if m.group("time"):                            # a time stays whole (removed later)
            return m.group(0)
        for v, tol in _readings(m.group("n"), None):
            for span in spans:
                if abs(v - span) <= tol or abs(v - span / 60) <= tol or abs(v - span * 60) <= tol:
                    return " "
        return m.group(0)

    return re.sub(rf"(?P<time>{TIME.pattern})|(?P<n>\d+(?:[.,]\d+)?)\s*(?:min|minutes?|mn|h|hours?|heures?|s|sec|"
                  r"seconds?)?\b", keep, line)


def _rows(value: Any) -> Iterable[list[Any]]:
    """The tables in a tool result: lists of rows (lists or dicts)."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _rows(v)
    elif isinstance(value, list) and value:
        if all(isinstance(r, dict) for r in value):
            yield [list(r.values()) for r in value[:MAX_ROWS]]
        elif all(isinstance(r, (list, tuple)) for r in value):
            yield [list(r) for r in value[:MAX_ROWS]]
        for v in value[:50]:
            if isinstance(v, (dict, list)):
                yield from _rows(v)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", v.strip()):
        return float(v)
    return None


def _minutes(v: str) -> float | None:
    """A time of day or a timestamp as minutes (days apart counted)."""
    text = v.strip()[:19]
    m = TIME.fullmatch(text) if len(v) <= 40 else None
    if m is None:
        return None
    days = 0.0
    date = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if date:
        import datetime as dt

        try:
            days = float(dt.date(*map(int, date.groups())).toordinal())
        except ValueError:
            return None
    return days * 1440 + int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3) or 0) / 60


def derived(rows: list[list[Any]]) -> set[float]:
    """Totals of the columns, of their first rows (in the result's order, and from the largest), the row
    count, the rates within a row and between column totals."""
    out: set[float] = {float(len(rows))}
    width = max((len(r) for r in rows), default=0)
    totals = []
    for i in range(width):
        col = [x for x in (_num(r[i]) for r in rows if i < len(r)) if x is not None]
        if not col:
            continue
        for order in (col, sorted(col, reverse=True)):
            run = 0.0
            for x in order:
                run += x
                out.add(run)
        totals.append(sum(col))
        out.add(sum(col) / len(col))                   # the average of a column
        if sum(col):
            out.update(x / sum(col) for x in col[:RATE_ROWS])      # a row's share of the total
    for r in rows[:RATE_ROWS]:                         # from / to of a row: its duration
        stamps = [_minutes(v) for v in r if isinstance(v, str)]
        stamps = [x for x in stamps if x is not None][:6]
        for a in stamps:
            for b in stamps:
                if a > b:
                    out.update({(a - b) * 60, a - b, (a - b) / 60})
    for group in [totals] + [[x for x in (_num(v) for v in r) if x is not None] for r in rows[:RATE_ROWS]]:
        group = group[:12]
        for a in group:
            for b in group:
                if b:
                    out.add(a / b)                     # shown as a percentage by the conversions
                    out.add((a - b) / b)
                out.add(a - b)
    return out


def seen_numbers(messages: list[dict]) -> list[float]:
    """Every number the LLM was given before its answer (tools, question, chat, knowledge) and
    what the tools' tables give by totals and rates."""
    values: set[float] = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or m.get("role") == "system":
            continue
        found = _values(content)
        values.update(found)
        if m.get("role") == "user":                     # a period of the question in minutes or hours (not
            periods = content                           # its dates and times: 30 September is not 30 days)
            for rx in SKIP[5:12]:
                periods = rx.sub(" ", periods)
            values.update(v * f for v in _values(periods) for f in (60, 24, 1440, 7))
        if m.get("role") == "tool":
            try:
                data = json.loads(content)
            except (ValueError, TypeError):             # cut by the size limit: the durations of its objects
                for chunk in re.split(r"[}\n]", content)[:2000]:
                    values |= derived([[t.group(0) for t in TIME.finditer(chunk)][:6]])
                continue
            for rows in _rows(data):
                values |= derived(rows)
        for call in m.get("tool_calls") or []:          # what the model asked for (a limit, a threshold)
            values.update(_values(str((call.get("function") or {}).get("arguments") or "")))
    return sorted(values)


NAME = re.compile(r"(?<![\w./-])([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*[-_][A-Za-z0-9]*\d[A-Za-z0-9]*)(?![\w-])")
CODE_NAME = re.compile(r"^[DWMY][-+]\d+$|^[A-Za-z]+\d*$|^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")   # codes, metric names


def answer_names(answer: str) -> list[str]:
    """Names with a digit in the answer's text (servers, hosts, pods: srv-amer-002), in `code` too, its
    code blocks and links left out: a name the answer writes must be one the tools gave."""
    text = answer or ""
    text = SKIP[0].sub(" ", text)                     # code blocks only: `srv-amer-200` in a sentence is a claim
    text = re.sub(r"https?://\S+|\]\([^)]*\)", " ", text)
    return list(dict.fromkeys(n for n in NAME.findall(text) if not CODE_NAME.match(n)))


def ungrounded(answer: str, messages: list[dict]) -> list[str]:
    """The numbers and names of the answer that nothing given to the LLM supports (as written)."""
    out = []
    candidates = answer_numbers(answer)
    if candidates:
        seen = seen_numbers(messages)
        kept = [(text, readings) for text, readings in candidates
                if not any(_close(v, tol, seen) for v, tol in readings)
                and not any(abs(v - c) <= tol for v, tol in readings for c in UNIT_CONSTANTS)]   # as written only:
        # a constant converted is no figure (3600 s as 60 min would make 59 to 61 of anything "found")
        # the rest of a share the answer shows too (99.00% available next to 1.00% of errors): shares only,
        # written with a % or decimals (12 jobs and 88 servers are not a share and its rest)
        share = re.compile(r"%|\d[.,]\d")
        shown = sorted(v for text, readings in candidates if (text, readings) not in kept and share.search(text)
                       for v, _t in readings)
        rests = sorted({100 - v for v in shown if 0 <= v <= 100} | {1 - v for v in shown if 0 <= v <= 1})
        kept = [(text, readings) for text, readings in kept
                if not (share.search(text) and any(_close(v, tol, rests) for v, tol in readings))]
        if kept:                                        # "BILLING and PAYROLL: 871" = the sum of the rows it names
            tables = [t for m in messages if m.get("role") == "tool" for t in _tables(m.get("content"))]
            kept = [(text, readings) for text, readings in kept if not _named_sum(text, readings, answer, tables)]
        out += [text for text, _readings in kept]
    names = answer_names(answer)
    if names:
        given = "\n".join(str(m.get("content") or "") + json.dumps(m.get("tool_calls") or "")
                          for m in messages if m.get("role") != "system").lower()
        out += [n for n in names if n.lower() not in given and not _given_range(n.lower(), given)]
    return list(dict.fromkeys(out))


RANGE_WORDS = re.compile(r"\b(up to|through|thru|until|to|continuing|and so on|etc|jusqu'?[àa]|jusqu)\b|\.\.\.|…|–|—",
                         re.I)


def drop_extrapolated(answer: str, names: list[str]) -> tuple[str, list[str]]:
    """The lines that continue a list up to a name no result gave ("... up to `srv-amer-199`") taken out
    of the answer: they are the model's own series, the lines before them stay. (answer, names left)."""
    if not names:
        return answer, names
    kept, dropped = [], set()
    for line in (answer or "").split("\n"):
        found = [n for n in names if n.lower() in line.lower()]
        if found and RANGE_WORDS.search(line):
            dropped |= set(found)
            continue
        kept.append(line)
    left = [n for n in names if n not in dropped or any(n.lower() in ln.lower() for ln in kept)]
    return ("\n".join(kept), left) if dropped else (answer, names)


def _tables(content: Any) -> list[list[dict]]:
    """The tables of rows (dicts) of a tool result."""
    try:
        data = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return []
    out: list[list[dict]] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list) and v:
            if all(isinstance(r, dict) for r in v):
                out.append(v[:MAX_ROWS])
            for x in v[:50]:
                if isinstance(x, (dict, list)):
                    walk(x)

    walk(data)
    return out


def _named_sum(text: str, readings: list[tuple[float, float]], answer: str, tables: list[list[dict]]) -> bool:
    """The number is the total of the rows its sentence names (two or more), in one column of a result."""
    sentence = next((s for s in re.split(r"(?<=[.!?])\s+|\n", answer or "") if text in s), "")
    if not sentence:
        return False
    for rows in tables:
        named = [r for r in rows if any(isinstance(v, str) and len(v) >= 2 and
                                        re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", sentence) for v in r.values())]
        if len(named) < 2:
            continue
        for col in named[0]:
            vals = [_num(r.get(col)) for r in named]
            if all(v is not None for v in vals) and any(abs(sum(vals) - v) <= max(t, 1e-9) for v, t in readings):
                return True
    return False


def _given_range(name: str, given: str) -> bool:
    """"n1-n5": two names the tools gave, written as a range (not a name of its own)."""
    m = re.fullmatch(r"([a-z][a-z0-9_]*\d)-([a-z][a-z0-9_]*\d)", name)
    return bool(m) and all(re.search(rf"(?<![\w-]){re.escape(p)}(?![\w-])", given) for p in m.groups())


def _close(value: float, tol: float, seen: list[float]) -> bool:
    import bisect

    for factor in CONVERSIONS:
        for sign in (1.0, -1.0):
            target = sign * value / factor              # the seen value that, converted, is shown as `value`
            margin = (tol if factor == 1.0 else 2 * tol) / factor   # converted: rounded twice (19.1949 GiB, 19.20)
            i = bisect.bisect_left(seen, target - margin)
            if i < len(seen) and seen[i] <= target + margin:
                return True
    return False
