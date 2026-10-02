"""Does a query cover the period of the question? A question with a date or a period ("on 23 September",
"yesterday", "last week", "right now") needs queries that filter the time of what they read: the index's
time field (the dictionary's) or ts for metrics, never business dates (position dates, D-1, W-1) unless
the question speaks of them. A question about one whole day ("on 23 September", no hours, no comparison)
needs the query to cover that day: not a part of it, not another day.

A query that does not is sent back once with the reason (sent again unchanged, it runs). Queries that only
list values (SELECT DISTINCT without aggregates) are not checked."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from superset import db

from supagent.dates import MONTH_END, MONTH_WORDS

MONTHS = {"jan": 1, "feb": 2, "fev": 2, "fév": 2, "mar": 3, "apr": 4, "avr": 4, "may": 5, "mai": 5, "jun": 6,
          "juin": 6, "jul": 7, "juil": 7, "aug": 8, "aou": 8, "aoû": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
          "déc": 12}
MONTH = rf"({MONTH_WORDS}){MONTH_END}"            # a month's name (not "markets", "decisions"): one group
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s+(?:of\s+)?{MONTH}(?:\s+(\d{{4}}))?", re.I)
MONTH_DAY = re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DAY_RANGE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s*(?:to|-|–|and|until|till|au|à|et)\s*(\d{{1,2}})"
                       rf"(?:st|nd|rd|th|er)?\s+{MONTH}(?:\s+(\d{{4}}))?", re.I)
HOURS = re.compile(r"\b\d{1,2}[:h]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\b(night|morning|afternoon|evening|nuit|matin|soir|"
                   r"window|fen[êe]tre)\b", re.I)
# a day said relative to another ("And on the 23rd?", "Et le 23 ?", "the day before", "la veille", "that day"): its
# month, or its day, is the one of the day named just before it (in the same text, or else in the questions before)
BARE_DAY = re.compile(
    r"\b(?:the|on)\s+(?P<en>\d{1,2})(?:st|nd|rd|th)(?=\s*(?:[?.!,;:)]|$|(?:and|or|instead|too|also|then|please|at|"
    r"as well)\b))|\ble\s+(?P<fr>\d{1,2})(?:er)?(?=\s*(?:[?.!,;:)]|$|(?:et|ou|aussi|alors|plut[ôo]t|[àa])\b))", re.I)
TWO_DAYS_AGO = re.compile(r"\b(?:the\s+)?day\s+before\s+yesterday\b|\bavant[- ]hier\b", re.I)
DAY_BEFORE = re.compile(r"\b(?:the\s+)?day\s+before\b(?!\s+yesterday)|\bthe\s+previous\s+day\b|\bla\s+veille\b|"
                        r"\ble\s+jour\s+(?:d'avant|pr[ée]c[ée]dent)\b", re.I)
DAY_AFTER = re.compile(r"\b(?:the\s+)?(?:next|following)\s+day\b|\b(?:the\s+)?day\s+after\b(?!\s+tomorrow)|"
                       r"\ble\s+lendemain\b|\ble\s+jour\s+(?:d'apr[èe]s|suivant)\b", re.I)
SAME_DAY = re.compile(r"\b(?:that|the same|this same) day\b(?!\s+(?:last|of last|the previous|previous|a week|one "
                      r"week|the week|a month|of the previous|before|after)\b)|\bce jour[- ]l[àa]\b|"
                      r"\ble m[êe]me jour\b(?!\s+(?:de la semaine|du mois|la semaine|le mois))", re.I)
# a period that is not a day, as a follow-up gives it ("And last week?", "Et cette semaine ?")
WINDOW = re.compile(
    r"\b(?:last|past|previous|this|next)\s+(?:\d+\s+)?(?:weeks?|months?|years?|quarters?|days?|hours?)\b|"
    r"\b(?:la\s+semaine|le\s+mois|l'ann[ée]e)\s+(?:derni[èe]re?|pass[ée]e?|prochaine?)\b|"
    r"\b(?:cette|ce)\s+(?:semaine|mois|ann[ée]e|trimestre)\b|"
    r"\b(?:les|ces)\s+\d+\s+derni[èe]r(?:e?s)\s+(?:jours|heures|semaines|mois)\b|"
    r"\bdepuis\s+\d+\s+(?:jours|heures|semaines)\b", re.I)
RELATIVE_DAY = {"yesterday": -1, "hier": -1, "today": 0, "aujourd'hui": 0, "aujourd hui": 0, "aujourd’hui": 0}
PERIOD_WORDS = re.compile(
    r"\b(yesterday|today|tonight|hier|aujourd|now|right now|currently|at the moment|maintenant|en ce moment|"
    r"actuellement|last|past|previous|this (?:week|month|morning|year)|since|until|between|during|over the|"
    r"week|weeks|month|months|day|days|hours?|minutes?|semaine|semaines|mois|jour|jours|heures?|dernier|"
    r"derni[èe]re|depuis|entre|pendant)\b", re.I)
PART_OF_DAY = re.compile(
    r"\b\d{1,2}[:h]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\b(night|morning|afternoon|evening|midnight|noon|window|between|"
    r"from\s+\S+\s+to|until|before|after|first|last|nuit|matin|apr[èe]s-midi|soir|fen[êe]tre|entre|jusqu|avant|"
    r"hours? of|minutes? of)\b", re.I)
COMPARED = re.compile(r"\b(compar\w*|previous|usual|normal|trend|versus|vs\.?|than|earlier|before|week before|"
                      r"pr[ée]c[ée]dent\w*|habitu\w*|tendance|par rapport)\b", re.I)
BUSINESS = re.compile(r"\b(position|business date|date m[ée]tier|valeur|[DWMY][-+]\d+)\b|POSITION_", re.I)
DATE_LIT = re.compile(r"'(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?'")
AGGREGATE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|PERCENTILE\w*|QUANTILE\w*|HISTOGRAM_QUANTILE|STDDEV\w*|RATE|"
                       r"INCREASE)\s*\(", re.I)
SQL_TOOLS = ("execute_sql", "export_excel", "chart_from_sql", "create_virtual_dataset", "save_sql_query")
WHOLE_DAY_H = 23.0             # a range this long or longer covers the day (BETWEEN ... 23:59:59)
PEEK_ROWS = 20                 # SELECT * FROM t LIMIT 10: a look at the data, not a figure


def _now() -> dt.datetime:
    from supagent.agent import now

    return now()


def days_named(question: str, today: dt.date, anchor: dt.date | None = None) -> list[dt.date]:
    """The days the question names, in order (23 September, September 23, 2026-09-23, yesterday, today; "the 23rd",
    "the day before" from the day named before it, or from the anchor: the day of the questions before)."""
    return list(dict.fromkeys(d for _s, _e, d, _full in _days_in(question or "", today, anchor)))


def _explicit(text: str, today: dt.date) -> list[tuple[int, int, dt.date]]:
    """The days written in full (23 September, September 23, 2026-09-23, yesterday, today), in order: (start, end,
    day)."""
    found: list[tuple[int, int, dt.date | None]] = []
    for m in DAY_MONTH.finditer(text):
        found.append((m.start(), m.end(), _day(int(m.group(1)), m.group(2), m.group(3), today)))
    for m in MONTH_DAY.finditer(text):
        found.append((m.start(), m.end(), _day(int(m.group(2)), m.group(1), m.group(3), today)))
    for m in ISO_DAY.finditer(text):
        try:
            found.append((m.start(), m.end(), dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            pass
    for m in TWO_DAYS_AGO.finditer(text):
        found.append((m.start(), m.end(), today - dt.timedelta(days=2)))
    low = TWO_DAYS_AGO.sub(lambda m: " " * len(m.group(0)), text.lower())
    for word, delta in RELATIVE_DAY.items():
        for m in re.finditer(rf"(?<![\w']){re.escape(word)}(?![\w'])", low):
            found.append((m.start(), m.end(), today + dt.timedelta(days=delta)))
    return sorted(((s, e, d) for s, e, d in found if d is not None), key=lambda x: x[0])


def _days_in(text: str, today: dt.date, anchor: dt.date | None = None) -> list[tuple[int, int, dt.date, bool]]:
    """Every day the text names, in order: (start, end, day, written in full). A day said relative to another
    ("the 23rd", "le 23", "the day before", "la veille") is read from the day named just before it in the text, or
    from one written right after it ("the day before 23 September"), or else from the anchor; with none it is no
    day ("And the 2nd?" after "Which application failed most?")."""
    items: list[tuple[int, int, Any, bool]] = [(s, e, d, True) for s, e, d in _explicit(text, today)]
    for m in BARE_DAY.finditer(text):
        items.append((m.start(), m.end(), ("day", int(m.group("en") or m.group("fr"))), False))
    for rx, delta in ((DAY_BEFORE, -1), (DAY_AFTER, 1), (SAME_DAY, 0)):
        for m in rx.finditer(text):
            items.append((m.start(), m.end(), ("shift", delta), False))
    items.sort(key=lambda x: x[0])
    out: list[tuple[int, int, dt.date, bool]] = []
    last = anchor
    for i, (start, end, v, full) in enumerate(items):
        if full:
            out.append((start, end, v, True))
            last = v
            continue
        right_after = next((x[2] for x in items[i + 1:] if x[3] and 0 <= x[0] - end <= 3), None)
        base = right_after if v[0] == "shift" and right_after else last
        if base is None:
            continue
        try:
            day = base.replace(day=v[1]) if v[0] == "day" else base + dt.timedelta(days=v[1])
        except ValueError:
            continue
        out.append((start, end, day, False))
        last = day
    return out


def anchor_of(questions: list[str], today: dt.date) -> dt.date | None:
    """The day a follow-up is relative to: the last day the questions before it named (oldest first, each one
    read relative to the ones before)."""
    anchor = None
    for q in questions or []:
        days = _days_in(q or "", today, anchor)
        if days:
            anchor = days[-1][2]
    return anchor


def follow_up_text(question: str, earlier: list[str], today: dt.date | None = None) -> str | None:
    """The text whose period the checks read for a follow-up that names its own day or period ("And on the 23rd?",
    "And the day before?", "Et le 23 ?", "And last week?"): the earlier question that wrote its day in full, with
    the follow-up's day (or period) in place of it, so its other words stay ("parcels shipped on 22 September
    during the night", then "And on the 23rd?": "parcels shipped on 23 September 2026 during the night"), then the
    follow-up with its days written in full. None when the follow-up names no day nor period of its own (it keeps
    the earlier one), or when one of the two names several days or a span (both are read together then)."""
    today = today or _now().date()
    text = question or ""
    own = _days_in(text, today, anchor_of(earlier, today))
    window = WINDOW.search(text)
    if (not own and not window) or len(own) > 1 or ranges_named(text, today):
        return None
    base = next((q for q in reversed(earlier or []) if any(full for *_x, full in _days_in(q or "", today))), None)
    if base is None or ranges_named(base, today):
        return None
    spans = [(s, e) for s, e, _d, full in _days_in(base, today) if full]
    if len(spans) != 1:
        return None
    said = f"{own[0][2].day} {own[0][2]:%B %Y}" if own else window.group(0)
    rewritten = text
    for s, e, d, full in sorted(own, key=lambda x: -x[0]):
        if not full:
            rewritten = rewritten[:s] + f"{d.day} {d:%B %Y}" + rewritten[e:]
    s, e = spans[0]
    return f"{base[:s]}{said}{base[e:]}\n{rewritten}"


def _day(day: int, month: str, year: str | None, today: dt.date) -> dt.date | None:
    key = month.lower().rstrip(".")
    number = next((v for k, v in MONTHS.items() if key.startswith(k)), None)
    try:
        return dt.date(int(year) if year else today.year, number, day) if number else None
    except ValueError:
        return None


def ranges_named(question: str, today: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
    """The spans of whole days the question names ("the week of 14 to 20 September"): [start, end)."""
    out = []
    for m in DAY_RANGE.finditer(question or ""):
        a, b = _day(int(m.group(1)), m.group(3), m.group(4), today), _day(int(m.group(2)), m.group(3), m.group(4), today)
        if a and b and a < b:
            out.append((dt.datetime(a.year, a.month, a.day), dt.datetime(b.year, b.month, b.day) + dt.timedelta(days=1)))
    return out


def has_period(question: str, today: dt.date) -> bool:
    return bool(days_named(question, today) or PERIOD_WORDS.search(question or ""))


def whole_day(question: str, today: dt.date) -> dt.date | None:
    """The one day the question is about, whole (no hours, no part of the day, no comparison; "on 17 and 18
    September" is a span of two days, not the 18th)."""
    days = days_named(question, today)
    if len(days) != 1 or PART_OF_DAY.search(question or "") or COMPARED.search(question or "") \
            or ranges_named(question, today):
        return None
    return days[0]


# the time field the question's words name: "shipped on 17 September" is the period of SHIPPED_TIME, "orders of 22
# September" of ORDER_DATE, whatever the index's main time field (the learner's) is
EVENT_GENERIC = {"time", "date", "timestamp", "datetime", "at", "ts", "day", "dt", "utc", "on", "of", "local", "the"}
DATE_PHRASES = (DAY_RANGE, DAY_MONTH, MONTH_DAY, ISO_DAY,
                re.compile(rf"\b(?:in|during|for|of|since)\s+{MONTH}", re.I),
                re.compile(r"\b(yesterday|today|hier|aujourd)", re.I))
EVENT_WINDOW = 3                       # words before a date phrase that may name its event


def _date_fields(tables: set[str]) -> dict[str, list[str]]:
    """The date fields of each index (the dictionary's)."""
    from supagent.models import KObject

    out: dict[str, list[str]] = {}
    for parent, name in db.session.query(KObject.parent, KObject.name).filter(
            KObject.kind == "field", KObject.parent.in_(list(tables) or ["-"]), KObject.gone_at.is_(None),
            KObject.data_type.in_(("date", "date_nanos", "timestamp", "datetime"))):
        out.setdefault(parent, []).append(name)
    return out


def _alike(a: str, b: str) -> bool:
    """The same word, or one with a common start of 5 letters at least (delivery, delivered)."""
    if a == b:
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 5 and min(len(a), len(b)) >= 5


def event_words(question: str) -> set[str]:
    """The words just before each date phrase of the question ("parcels shipped on 17 September": parcels,
    shipped, on)."""
    text = question or ""
    out: set[str] = set()
    for rx in DATE_PHRASES:
        for m in rx.finditer(text):
            before = re.findall(r"[a-zà-ÿ]+", text[:m.start()].lower())
            out |= set(before[-EVENT_WINDOW:])
    return out - EVENT_GENERIC


def _fields_said(words: set[str], fields: list[str] | set[str]) -> set[str]:
    """The fields among these whose name one of these words says (shipped: SHIPPED_TIME)."""
    out: set[str] = set()
    for f in fields:
        parts = [t for t in re.split(r"[^a-z0-9]+", f.lower()) if t and t not in EVENT_GENERIC]
        if parts and any(_alike(p, w) for p in parts for w in words):
            out.add(f)
    return out


def named_among(question: str, fields: list[str] | set[str]) -> set[str]:
    """The date fields among these whose name the question says next to a date."""
    said = event_words(question)
    return _fields_said(said, fields) if said else set()


def named_days(question: str, fields: list[str] | set[str], today: dt.date) -> dict[dt.date, set[str]]:
    """{a day the question names: the date fields its words name just before it} ("orders of 22 September were
    shipped on 24 September or later": 22 September, ORDER_DATE; 24 September, SHIPPED_TIME). A span: its first
    day."""
    text = question or ""
    found: list[tuple[int, dt.date | None]] = []
    found += [(m.start(), _day(int(m.group(1)), m.group(3), m.group(4), today)) for m in DAY_RANGE.finditer(text)]
    found += [(m.start(), _day(int(m.group(1)), m.group(2), m.group(3), today)) for m in DAY_MONTH.finditer(text)]
    found += [(m.start(), _day(int(m.group(2)), m.group(1), m.group(3), today)) for m in MONTH_DAY.finditer(text)]
    for m in ISO_DAY.finditer(text):
        try:
            found.append((m.start(), dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            pass
    out: dict[dt.date, set[str]] = {}
    for start, day in found:
        if day is None:
            continue
        words = set(re.findall(r"[a-zà-ÿ]+", text[:start].lower())[-EVENT_WINDOW:]) - EVENT_GENERIC
        hit = _fields_said(words, fields)
        if hit:
            out.setdefault(day, set()).update(hit)
    return out


def named_time_fields(question: str, tables: set[str]) -> dict[str, set[str]]:
    """{index: its date fields whose name the question says next to a date}."""
    if not event_words(question):
        return {}
    out: dict[str, set[str]] = {}
    for table, fields in _date_fields(tables).items():
        hit = named_among(question, fields)
        if hit:
            out[table] = hit
    return out


def _time_fields(tables: set[str]) -> dict[str, str]:
    """The time column of each table: the index's time field (the dictionary), ts for a metric."""
    from supagent.models import KObject

    out: dict[str, str] = {}
    for o in db.session.query(KObject).filter(KObject.kind.in_(("index", "metric")), KObject.name.in_(list(tables)),
                                              KObject.gone_at.is_(None)):
        if o.kind == "metric":
            out[o.name] = "ts"
        elif (o.stats or {}).get("time_field"):
            out[o.name] = str(o.stats["time_field"])
    return out


def _literals(sql: str) -> list[dt.datetime]:
    out = []
    for m in DATE_LIT.finditer(sql or ""):
        try:
            day = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        out.append(dt.datetime(day.year, day.month, day.day, int(m.group(2) or 0), int(m.group(3) or 0),
                               int(m.group(4) or 0)))
    return out


def _timed_date_fields(tables: set[str]) -> dict[str, str]:
    """The date fields of these indices whose values carry a time of day ({field: a value, as learned}): a date
    alone (= '2026-09-23') matches only its 00:00 there, which such data never has."""
    from supagent.models import KObject

    out: dict[str, str] = {}
    for o in db.session.query(KObject).filter(KObject.kind == "field", KObject.parent.in_(list(tables)),
                                              KObject.data_type.in_(("date", "date_nanos")), KObject.gone_at.is_(None)):
        st = o.stats or {}
        seen = [str(v) for v in (st.get("min"), st.get("max")) if v]
        timed = [v for v in seen if re.search(r"[ T](\d{1,2}):(\d{2})", v) and not re.search(r"[ T]0?0:00(:00)?$", v)]
        if timed:
            out[o.name] = timed[-1]
    return out


def day_equality(sql: str, tables: set[str]) -> str | None:
    """A date field whose values carry a time, compared with a date alone ("TRADE_DATE" = '2026-09-23', = DATE
    '2026-09-23', = TIMESTAMP '2026-09-23 00:00'): it finds nothing. The day is a range."""
    fields = _timed_date_fields(tables) if tables else {}
    for field, sample in sorted(fields.items()):
        m = re.search(rf"(?<![\w@])\"?{re.escape(field)}\"?\s*(?:=|\bIN\s*\(\s*)\s*(?:(?:DATE|TIMESTAMP)\s+)?"
                      rf"'(\d{{4}}-\d{{2}}-\d{{2}})(?:[ T]00:00(?::00(?:\.0+)?)?)?'", sql, re.I)      # DATE '...' too
        if m is None:
            continue
        try:
            day = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        nxt = day + dt.timedelta(days=1)
        return (f"tool error (not run: date): {field} holds a date and a time (e.g. {sample}), so {field} = "
                f"'{day}' matches only {day} 00:00 and finds nothing. For that day use \"{field}\" >= '{day}' AND "
                f"\"{field}\" < '{nxt}'. If that exact instant is meant, send this same call again unchanged.")
    return None


def refusal(question: str, tool: str, args: dict, today: dt.date | None = None) -> str | None:
    """Why this query does not cover the question's period (sent back once), or None."""
    if tool not in SQL_TOOLS or not question:
        return None
    from supagent.knowledge.excluded import query_texts
    from supagent.knowledge.rulecheck import _tables

    sqls = [q for q in query_texts(args) if q]
    if sqls:
        same_day = day_equality(sqls[-1], _tables(sqls[-1]))
        if same_day:
            return same_day
    today = today or _now().date()
    if not has_period(question, today):
        return None
    if not sqls:
        return None
    sql = sqls[-1]
    if re.search(r"\bSELECT\s+DISTINCT\b", sql, re.I) and not AGGREGATE.search(sql):
        return None                                     # a list of values, not a figure of the period
    peek = re.search(r"\bLIMIT\s+(\d+)", sql, re.I)
    if peek and int(peek.group(1)) <= PEEK_ROWS and not AGGREGATE.search(sql) and not re.search(r"\bWHERE\b", sql, re.I):
        return None                                     # a look at a few rows (the columns, the values)
    tables = _tables(sql)
    fields = _time_fields(tables) if tables else {}
    if not fields:
        return None
    asks_business = bool(BUSINESS.search(question))
    named = named_time_fields(question, tables)
    dates = _date_fields(set(named)) if named else {}
    for table, field in sorted(fields.items()):
        names = named.get(table) or set()
        if names:                                       # the question names the event of its period
            used = {f for f in dates.get(table, []) if re.search(rf"(?<![\w@]){re.escape(f)}(?![\w])", sql)}
            if used & names:
                continue
            want = " or ".join(f'"{f}"' for f in sorted(names))
            if used:
                return (f"tool error (not run: period): the question's words name the time of its period: {want} of "
                        f"{table}, and this query puts the period on {', '.join(sorted(used))}. Put the question's "
                        f"period on {want}. If {sorted(used)[0]} is meant, send this same call again unchanged.")
            return (f"tool error (not run: period): the question is about a period and this query has no filter on "
                    f"{want} of {table}, the time its words name: it would count every date. Add the question's "
                    "period on it. If that is meant, send this same call again unchanged.")
        uses_time = re.search(rf"(?<![\w@]){re.escape(field)}(?![\w])", sql) is not None
        if re.search(r"\bPOSITION_(DATE|LABEL|TIME)\b", sql) and not asks_business:
            return (f"tool error (not run: period): the question gives a date, not a position (business) date, and "
                    f"this query filters POSITION_... Filter the time field \"{field}\" of {table} on the question's "
                    "period instead. If the question does mean position dates, send this same call again unchanged.")
        if not uses_time and not (asks_business and "POSITION_" in sql):
            return (f"tool error (not run: period): the question is about a period and this query has no filter on "
                    f"the time field \"{field}\" of {table}: it would count every date (or only the default window). "
                    f"Add the question's period on \"{field}\" (dates from the question and from now). If that is "
                    "meant, send this same call again unchanged.")
    lits = _literals(sql)
    spans = ranges_named(question, today) if not HOURS.search(question) else []
    if spans and len(lits) >= 2:                        # "the week of 14 to 20 September": that span exactly
        low, high = min(lits), max(lits)
        day = dt.timedelta(days=1)                      # the span with a boundary a few hours off, not another span
        near = [sp for sp in spans if abs(low - sp[0]) <= day and abs(high - sp[1]) <= day]
        if near and not any(low == a and high == b for a, b in spans):
            said = "; ".join(f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}" for a, b in spans)
            return (f"tool error (not run: period): the question's period is {said} (whole days), and this query "
                    f"covers {low:%Y-%m-%d %H:%M} to {high:%Y-%m-%d %H:%M}. Use the question's days exactly. If "
                    "that window is meant, send this same call again unchanged.")
    day = whole_day(question, today)
    if day is None:
        return None
    if not lits:
        return None
    start = dt.datetime(day.year, day.month, day.day)
    end = start + dt.timedelta(days=1)
    if not any(start <= t < end for t in lits):          # the next midnight alone: another day
        seen = ", ".join(sorted({t.strftime("%Y-%m-%d") for t in lits}))
        return (f"tool error (not run: period): the question is about {day:%d %B %Y} and this query's dates are "
                f"{seen}. Use {day:%Y-%m-%d} 00:00 to {end:%Y-%m-%d} 00:00. If another day is meant, send this same "
                "call again unchanged.")
    inside = [t for t in lits if start <= t <= end]
    if len(inside) >= 2 and (max(inside) - min(inside)).total_seconds() / 3600 < WHOLE_DAY_H:
        return (f"tool error (not run: period): the question is about the whole of {day:%d %B %Y} and this query "
                f"covers only {min(inside):%H:%M} to {max(inside):%H:%M}. Use {day:%Y-%m-%d} 00:00 to {end:%Y-%m-%d} "
                "00:00. If only that part of the day is meant, send this same call again unchanged.")
    return None


def describe(question: str, today: dt.date) -> dict[str, Any]:
    return {"days": days_named(question, today), "period": has_period(question, today),
            "whole_day": whole_day(question, today)}
