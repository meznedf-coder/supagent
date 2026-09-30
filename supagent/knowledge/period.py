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

MONTHS = {"jan": 1, "feb": 2, "fev": 2, "fév": 2, "mar": 3, "apr": 4, "avr": 4, "may": 5, "mai": 5, "jun": 6,
          "juin": 6, "jul": 7, "juil": 7, "aug": 8, "aou": 8, "aoû": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
          "déc": 12}
MONTH = r"(jan|feb|f[ée]v|mar|apr|avr|may|mai|jun|juin|jul|juil|aug|ao[uû]|sep|oct|nov|d[ée]c)[a-zéû]*\.?"
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s+{MONTH}(?:\s+(\d{{4}}))?", re.I)
MONTH_DAY = re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DAY_RANGE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s*(?:to|-|–|and|until|till|au|à|et)\s*(\d{{1,2}})"
                       rf"(?:st|nd|rd|th|er)?\s+{MONTH}(?:\s+(\d{{4}}))?", re.I)
HOURS = re.compile(r"\b\d{1,2}[:h]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\b(night|morning|afternoon|evening|nuit|matin|soir|"
                   r"window|fen[êe]tre)\b", re.I)
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


def days_named(question: str, today: dt.date) -> list[dt.date]:
    """The days the question names (23 September, September 23, 2026-09-23, yesterday, today)."""
    out: list[dt.date] = []
    text = question or ""
    for m in DAY_MONTH.finditer(text):
        out.append(_day(int(m.group(1)), m.group(2), m.group(3), today))
    for m in MONTH_DAY.finditer(text):
        out.append(_day(int(m.group(2)), m.group(1), m.group(3), today))
    for m in ISO_DAY.finditer(text):
        try:
            out.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    low = text.lower()
    for word, delta in RELATIVE_DAY.items():
        if re.search(rf"(?<![\w']){re.escape(word)}(?![\w'])", low):
            out.append(today + dt.timedelta(days=delta))
    return list(dict.fromkeys(d for d in out if d is not None))


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
    """The one day the question is about, whole (no hours, no part of the day, no comparison)."""
    days = days_named(question, today)
    if len(days) != 1 or PART_OF_DAY.search(question or "") or COMPARED.search(question or ""):
        return None
    return days[0]


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


def refusal(question: str, tool: str, args: dict, today: dt.date | None = None) -> str | None:
    """Why this query does not cover the question's period (sent back once), or None."""
    if tool not in SQL_TOOLS or not question:
        return None
    from supagent.knowledge.excluded import query_texts
    from supagent.knowledge.rulecheck import _tables

    today = today or _now().date()
    if not has_period(question, today):
        return None
    sqls = [q for q in query_texts(args) if q]
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
    for table, field in sorted(fields.items()):
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
