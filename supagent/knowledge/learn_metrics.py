"""Learning the metrics of a Prometheus / Mimir database (promagg), gently.

Every run (cheap, for every metric): the list of metrics and the metadata API (type, unit,
HELP text) - two or three requests for the whole database.

Profiles (for the metrics that are due): a metric is profiled when it is new, and then on its
own day of a rolling cycle of learn.profile_every_days days (its name decides the day), so
that the work of a week is spread over the week. A profile costs about one request of its own:
  * how many series have a sample now: one query for up to 50 metrics (count by __name__, the
    series index of the live samples; metrics known to be big are counted alone);
  * the value statistics over the window, one query per metric: rates for counters, p50 / p95
    for histograms, min / max / avg for gauges (skipped above learn.stats_max_series series, one
    hour of data above 50,000);
  * a sample of the series: the labels and their values (the values are what the relations with
    index fields are measured on); metrics with a few series share one series request, the
    others have their own (at most learn.series_sample series).
The depth of the history (first sample, series index only, no samples read) is measured with
the time left once every due metric is profiled: never measured first, then every 30 days; for
live metrics about 8 label-index requests per 50 metrics (to the window: 60, 45, 30, 21, 14, 7,
3, 1 days), a metric that stopped alone (bisected to the day); the last sample of a metric that
stopped, when it stops. Thousands of metrics are thus profiled in
a few runs (each run continues where the previous one stopped), then refreshed day by day.

Every request goes through the throttle (rate limit, breaker): see throttle.py.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import zlib
from typing import Any

from superset import db

from supagent import settings
from supagent.knowledge.store import mark_gone, matches, relate, unit_from_name, upsert
from supagent.knowledge.throttle import Proxy, SourceStopped, Throttle
from supagent.models import KObject, Run, Source

log = logging.getLogger(__name__)
LABEL_VALUES_KEPT = 1000
BIG_METRIC = 50_000               # more series: value statistics over one hour only
FAMILY_SUFFIXES = ("_bucket", "_sum", "_count")
HISTORY_REFRESH_DAYS = 30
BATCH = 50                        # metrics per request: live series counts, label samples of small metrics
SMALL_SERIES = 200                # a metric with at most this many series shares a series request
DAY_MS = 86_400_000


def _iso(ms: int | None, conn: Any = None) -> str | None:
    """Epoch ms -> local time of the metrics database (promagg's timezone), as its SQL shows it."""
    if ms is None:
        return None
    zone = getattr(conn, "zone", None)
    if zone is not None:
        try:
            return f"{zone.local(ms):%Y-%m-%d %H:%M}"
        except Exception:  # pylint: disable=broad-except
            pass
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M UTC")


def due(name: str, stats: dict[str, Any] | None, every_days: int, today: dt.date) -> bool:
    """Profile now: never profiled, its day of the cycle, or overdue (twice the cycle)."""
    last = (stats or {}).get("profiled_on")
    if not last:
        return True
    try:
        age = (today - dt.date.fromisoformat(last)).days
    except ValueError:
        return True
    if age <= 0:
        return False
    every = max(1, every_days)
    return age >= 2 * every or (today.toordinal() % every) == (zlib.crc32(name.encode()) % every)


def kind_from_name(name: str) -> str:
    """The type when the metadata API does not know it (Prometheus naming conventions)."""
    if name.endswith("_bucket"):
        return "histogram"
    if name.endswith(("_total", "_created")):
        return "counter"
    if name.endswith(("_sum", "_count")):
        return "summary"
    return "gauge"


def _vector(conn: Any, expr: str, t_ms: int) -> list[Any]:
    try:
        return conn.client.query(expr, t_ms, timeout=int(settings.get("learn.request_timeout")))
    except SourceStopped:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        log.info("supagent learn: %s: %s", expr[:100], ex)
        return []


def _value(series: Any) -> float | None:
    if not series or not series.points:
        return None
    v = series.points[0][1]
    return None if v != v else round(float(v), 6)       # NaN -> None


def _stat_query(sel: str, kind: str, name: str, w: str) -> str:
    """One query returning several statistics, one series each (label "stat")."""
    if name.endswith("_bucket"):
        parts = {f"p{int(q * 100)}": f"histogram_quantile({q}, sum by (le) (rate({sel}[{w}])))" for q in (0.5, 0.95)}
    elif kind in ("counter", "histogram", "summary"):
        parts = {"rate_total": f"sum(rate({sel}[{w}]))", "rate_max_series": f"max(rate({sel}[{w}]))"}
    else:
        parts = {"min": f"min(min_over_time({sel}[{w}]))", "max": f"max(max_over_time({sel}[{w}]))",
                 "avg": f"avg(avg_over_time({sel}[{w}]))"}
    return " or ".join(f'label_replace({q}, "stat", "{k}", "", "")' for k, q in parts.items())


def _exists(conn: Any, sel: str, a: int, b: int) -> bool:
    try:
        return bool(conn.client.series([sel], a, b, limit=1))
    except SourceStopped:
        raise
    except Exception:  # pylint: disable=broad-except
        return False


def _first_sample(conn: Any, sel: str, start: int, end: int, resolution_ms: int = 86_400_000) -> int | None:
    """About when the data starts (to the day): the series index bisected, no samples read."""
    if not _exists(conn, sel, start, end):
        return None
    lo, hi = start, end
    while hi - lo > resolution_ms:
        mid = (lo + hi) // 2
        if _exists(conn, sel, lo, mid):
            hi = mid
        else:
            lo = mid
    return lo


def _last_sample(conn: Any, sel: str, start: int, end: int, resolution_ms: int = 3_600_000) -> int | None:
    if not _exists(conn, sel, start, end):
        return None
    lo, hi = start, end
    while hi - lo > resolution_ms:
        mid = (lo + hi) // 2
        if _exists(conn, sel, mid, hi):
            lo = mid
        else:
            hi = mid
    return hi


def profile_metric(conn: Any, name: str, meta: Any, kind: str, hours: int, old: dict[str, Any],
                   live: int | None = None, history: bool = True) -> dict[str, Any]:
    """`live`: its series with a sample now, when already counted (batch_counts); `history`:
    measure the depth of the history now if due (else the learner does it with the time left)."""
    sel = '{__name__="%s"}' % name
    now = conn.now_ms()
    stats: dict[str, Any] = {"profiled_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                             "profiled_on": dt.date.today().isoformat()}
    if live is None:
        live = _value(next(iter(_vector(conn, f"count({sel})", now)), None))
    if live:
        end = now
        stats["series"] = int(live)
    else:
        prev = old.get("data_to_ms")
        if prev and not _exists(conn, sel, int(prev) + 1, now) and _exists(conn, sel, int(prev) - 3_600_000, int(prev)):
            end = int(prev)                  # no newer data since the last profile: 2 lookups, no bisection
        else:
            end = _last_sample(conn, sel, max(int(prev or 0), now - 60 * 86_400_000), now)
        if end is None:
            stats["note"] = "no data in the last 60 days"
            return stats
        stats["series"] = int(_value(next(iter(_vector(conn, f"count(last_over_time({sel}[1h]))", end)), None)) or 0)
    stats["data_to"] = _iso(end, conn)
    stats["data_to_ms"] = int(end)
    # the depth of the history: measured once, then every HISTORY_REFRESH_DAYS days
    checked = old.get("history_checked_on")
    if old.get("data_from") and checked and \
            (dt.date.today() - dt.date.fromisoformat(checked)).days < HISTORY_REFRESH_DAYS:
        stats["data_from"], stats["history_checked_on"] = old["data_from"], checked
    elif history:
        first = _first_sample(conn, sel, end - 60 * DAY_MS, end)
        stats["data_from"] = _iso(first, conn)
        stats["history_checked_on"] = dt.date.today().isoformat()
    else:                                    # measured later with the time left (learn_history)
        for k in ("data_from", "history_checked_on"):
            if old.get(k):
                stats[k] = old[k]
    series = stats["series"]
    if series and series <= int(settings.get("learn.stats_max_series")):
        w = f"{hours}h" if series <= BIG_METRIC else "1h"
        stats["window"] = w
        for s in _vector(conn, _stat_query(sel, kind, name, w), end):
            stat = (getattr(s, "labels", None) or {}).get("stat")
            if stat:
                stats[stat] = _value(s)
    stats["_end_ms"] = end
    return stats


def label_sample(conn: Any, name: str, end_ms: int) -> tuple[dict[str, dict[str, Any]], bool]:
    """Labels and their values from one sample of the metric's series; (labels, complete)."""
    limit = int(settings.get("learn.series_sample"))
    sel = '{__name__="%s"}' % name
    try:
        series = conn.client.series([sel], end_ms - 3_600_000, end_ms, limit=limit)
    except SourceStopped:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        return {"__error__": {"error": str(ex)[:200]}}, False
    complete = len(series) < limit
    return _label_stats(series, complete), complete


def _label_stats(series: list[Any], complete: bool) -> dict[str, dict[str, Any]]:
    values: dict[str, set[str]] = {}
    for s in series:
        labels = s if isinstance(s, dict) else getattr(s, "labels", {}) or {}
        for k, v in labels.items():
            if k != "__name__" and v != "":
                values.setdefault(k, set()).add(str(v))
    out: dict[str, dict[str, Any]] = {}
    for label, vals in values.items():
        many = len(vals) > LABEL_VALUES_KEPT
        stat: dict[str, Any] = {"cardinality": len(vals) if complete and not many else f">={len(vals)}"}
        if many:
            stat["sample"] = sorted(vals)[:20]
        else:
            stat["values"] = sorted(vals)
            if not complete:
                stat["partial"] = True             # from a sample of the series
        out[label] = stat
    return out


def batch_counts(conn: Any, names: list[str], t_ms: int) -> dict[str, int] | None:
    """Series with a sample now, for many metrics in one query ({name: count}, 0 when not live);
    None when the query failed (each metric is then counted alone)."""
    if not names:
        return {}
    rx = "|".join(re.escape(n) for n in names)
    try:
        res = conn.client.query(f'count by (__name__) ({{__name__=~"{rx}"}})', t_ms,
                                timeout=int(settings.get("learn.request_timeout")))
    except SourceStopped:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        log.info("supagent learn: series counts of %d metrics: %s", len(names), ex)
        return None
    found: dict[str, int] = {}
    for s in res or []:
        name = (getattr(s, "labels", None) or {}).get("__name__")
        v = _value(s)
        if name and v:
            found[name] = int(v)
    return {n: found.get(n, 0) for n in names}


def batch_labels(conn: Any, counts: dict[str, int], end_ms: int) -> dict[str, tuple[dict[str, dict[str, Any]], bool]]:
    """The labels of the metrics with a few series (at most SMALL_SERIES), several metrics per
    series request; the others (and a request that fails or is cut) are sampled one by one."""
    limit = int(settings.get("learn.series_sample"))
    groups: list[list[str]] = []
    group: list[str] = []
    total = 0
    for name, n in counts.items():
        if not 0 < n <= SMALL_SERIES:
            continue
        if group and (total + n > limit // 2 or len(group) >= BATCH):
            groups.append(group)
            group, total = [], 0
        group.append(name)
        total += n
    if group:
        groups.append(group)
    out: dict[str, tuple[dict[str, dict[str, Any]], bool]] = {}
    for g in groups:
        try:
            series = conn.client.series(['{__name__="%s"}' % n for n in g], end_ms - 3_600_000, end_ms, limit=limit)
        except SourceStopped:
            raise
        except Exception as ex:  # pylint: disable=broad-except
            log.info("supagent learn: labels of %d metrics: %s", len(g), ex)
            continue
        if len(series) >= limit:
            continue                                   # cut: each one alone
        by_name: dict[str, list[Any]] = {}
        for s in series:
            labels = s if isinstance(s, dict) else getattr(s, "labels", {}) or {}
            by_name.setdefault(labels.get("__name__", ""), []).append(labels)
        for n in g:
            out[n] = (_label_stats(by_name.get(n, []), True), True)
    return out


HISTORY_DAYS = (60, 45, 30, 21, 14, 7, 3, 1)       # window starts, in days before the end of the data


def batch_history(conn: Any, names: list[str], end_ms: int) -> dict[str, int] | None:
    """About when the data of many metrics starts (series index only): for windows between 60
    days before `end_ms` and `end_ms`, the metric names that have series there, one label-index
    request per window for up to BATCH metrics; {name: start of its first window} (60 days =
    at least 60 days). None when a request failed (each metric is then bisected alone)."""
    rx = "|".join(re.escape(n) for n in names)
    bounds = [end_ms - d * DAY_MS for d in HISTORY_DAYS] + [end_ms]
    first: dict[str, int] = {}
    for a, b in zip(bounds, bounds[1:]):
        try:
            present = conn.client.label_values("__name__", [f'{{__name__=~"{rx}"}}'], a, b)
        except SourceStopped:
            raise
        except Exception as ex:  # pylint: disable=broad-except
            log.info("supagent learn: history of %d metrics: %s", len(names), ex)
            return None
        for n in present or []:
            first.setdefault(n, a)
        if len(first) == len(names):
            break                                  # every metric placed: the later windows add nothing
    return first


def learn_history(conn: Any, source: Source, deadline: float) -> dict[str, int]:
    """The depth of the history of the metrics that need it, with the time left: never measured
    first, then the oldest. Metrics whose data is live share requests (batch_history, about 8
    for 50 metrics); a metric whose data stopped is bisected alone (about 7 lookups)."""
    if time.time() >= deadline:
        return {"history": 0, "history_pending": 0}
    today = dt.date.today()
    now = conn.now_ms()
    todo = []
    for o in db.session.query(KObject).filter(KObject.source_id == source.id, KObject.kind == "metric",
                                              KObject.gone_at.is_(None)):
        st = o.stats or {}
        checked = st.get("history_checked_on")
        if not st.get("data_to_ms") or (checked and (today - dt.date.fromisoformat(checked)).days < HISTORY_REFRESH_DAYS):
            continue
        todo.append((bool(checked), checked or "", o.name, o))
    todo.sort(key=lambda x: x[:3])
    live = [x for x in todo if int(x[3].stats["data_to_ms"]) >= now - 3_600_000]
    alone = [x for x in todo if int(x[3].stats["data_to_ms"]) < now - 3_600_000]
    done = 0

    def save(o: KObject, first_ms: int | None) -> None:
        st = dict(o.stats or {})
        st["data_from"], st["history_checked_on"] = _iso(first_ms, conn), today.isoformat()
        o.stats = st

    for i in range(0, len(live), BATCH):
        if time.time() >= deadline:
            break
        chunk = [x[3] for x in live[i:i + BATCH]]
        found = batch_history(conn, [o.name for o in chunk], now)
        if found is None:
            alone += live[i:i + BATCH]
            continue
        for o in chunk:
            save(o, found.get(o.name))
            done += 1
        db.session.commit()
    for _c, _d, name, o in alone:
        if time.time() >= deadline:
            break
        end = int(o.stats["data_to_ms"])
        save(o, _first_sample(conn, '{__name__="%s"}' % name, end - 60 * DAY_MS, end))
        db.session.commit()
        done += 1
    return {"history": done, "history_pending": len(todo) - done}


def learn_metrics(run: Run, source: Source, database: Any, deadline: float) -> dict[str, Any]:
    from supagent.tools import _promagg_connection

    include, exclude = settings.get("learn.metrics"), settings.get("learn.metrics_exclude")
    hours = int(settings.get("learn.profile_hours"))
    limit = int(settings.get("learn.max_objects"))
    every = int(settings.get("learn.profile_every_days"))
    throttle = Throttle(int(settings.get("learn.max_requests_per_minute")), int(settings.get("learn.stop_after_errors")),
                        name=database.database_name)
    conn = _promagg_connection(database)
    conn.client = Proxy(conn.client, throttle, ("query", "query_range", "series", "label_values", "metadata",
                                                "label_names", "tenants"))
    out: dict[str, Any] = {"metrics": 0, "profiled": 0, "labels": 0, "not_due": 0, "complete": True}
    today = dt.date.today()
    try:
        names = [n for n in conn.list_tables() if matches(n, include, exclude)]
        listed_all = len(names) <= limit
        if not listed_all:
            names, out["complete"] = names[:limit], False
        try:
            metadata = conn.client.metadata()
        except SourceStopped:
            raise
        except Exception:  # pylint: disable=broad-except
            metadata = {}
        known = {o.name: o for o in db.session.query(KObject).filter_by(source_id=source.id, kind="metric")}
        # new metrics first, then the ones profiled longest ago
        names.sort(key=lambda n: (n in known, ((known[n].stats or {}).get("profiled_on") or "") if n in known else ""))
        seen_metrics: set[tuple[str, str]] = set()
        seen_labels: set[tuple[str, str]] = set()
        families: dict[str, list[KObject]] = {}
        due_names = [n for n in names if due(n, known[n].stats if n in known else None, every, today)]
        out["due"] = len(due_names)
        due_set = set(due_names)
        counted: dict[str, int] = {}                  # live series counts, batch by batch
        sampled: dict[str, tuple[dict[str, dict[str, Any]], bool]] = {}
        prepared: set[str] = set()

        def prepare(name: str) -> None:
            """Counts (and small label samples) for this metric and the next due ones."""
            i = due_names.index(name)
            chunk = [n for n in due_names[i:i + BATCH] if n not in prepared]
            prepared.update(chunk)
            big = {n for n in chunk if n in known and ((known[n].stats or {}).get("series") or 0) > BIG_METRIC}
            counts = batch_counts(conn, [n for n in chunk if n not in big], conn.now_ms())
            if counts is None:
                return                                 # counted one by one
            counted.update(counts)
            sampled.update(batch_labels(conn, counts, conn.now_ms()))

        def keep_labels(metric: str) -> None:
            seen_labels.update((metric, lb.name) for lb in db.session.query(KObject).filter_by(
                source_id=source.id, kind="label", parent=metric))

        for name in names:
            seen_metrics.add(("", name))
            old = known.get(name)
            md = (metadata.get(name) or [{}])[0]
            kind = md.get("type") or (old.metric_type if old is not None else None) or "unknown"
            if name.endswith("_bucket"):
                kind = "histogram"
            facts: dict[str, Any] = {"metric_type": kind, "unit": md.get("unit") or unit_from_name(name),
                                     "backend_help": md.get("help") or None}
            profile_now = time.time() < deadline and due(name, old.stats if old is not None else None, every, today)
            if not profile_now:
                keep_labels(name)
                if old is None:          # new but no time left: known by name, profiled next run
                    obj = upsert(run, source, "metric", "", name, facts)
                else:
                    obj = old
                    for k in ("metric_type", "unit", "backend_help"):
                        if facts.get(k) and not getattr(obj, k):
                            setattr(obj, k, facts[k])
                    obj.last_seen, obj.gone_at = dt.datetime.utcnow(), None
                    out["not_due"] += 1
                if time.time() >= deadline and name in due_set:
                    out["complete"] = False            # due, left for the next run
            else:
                if kind == "unknown":
                    kind = kind_from_name(name)
                    facts["metric_type"] = kind
                if name not in prepared:
                    prepare(name)
                stats = profile_metric(conn, name, None, kind, hours, (old.stats or {}) if old is not None else {},
                                       live=counted.get(name), history=False)
                end_ms = stats.pop("_end_ms", conn.now_ms())
                if "note" in stats:
                    labels = {}
                elif name in sampled and counted.get(name) and end_ms >= conn.now_ms() - 600_000:
                    labels = sampled[name][0]
                else:
                    labels, _complete = label_sample(conn, name, end_ms)
                if "quantile" in labels and not name.endswith(FAMILY_SUFFIXES):
                    facts["metric_type"] = kind = "summary"
                stats["labels"] = sorted(k for k in labels if not k.startswith("__error"))
                obj = upsert(run, source, "metric", "", name, {**facts, "stats": stats})
                out["profiled"] += 1
                for label, lstats in labels.items():
                    if label.startswith("__error"):
                        continue
                    upsert(run, source, "label", name, label, {"data_type": "string", "stats": lstats})
                    seen_labels.add((name, label))
                    out["labels"] += 1
                if not labels:
                    keep_labels(name)
            out["metrics"] += 1
            base = re.sub(r"_(bucket|sum|count)$", "", name)
            if base != name:
                families.setdefault(base, []).append(obj)
            db.session.commit()
        for base, parts in families.items():
            if len(parts) < 2 and not any(p.name.endswith("_bucket") for p in parts):
                continue
            fam = upsert(run, source, "family", "", base, {"metric_type": "histogram" if any(
                p.name.endswith("_bucket") for p in parts) else "summary", "stats": {"parts": sorted(p.name for p in parts)}})
            for p in parts:
                relate(fam, p, "family_part", {"family": base}, 1.0)
        out.update(learn_history(conn, source, deadline))     # with the time left
        out["gone"] = 0
        if listed_all:                                # every metric was listed, even if not all profiled
            out["gone"] += mark_gone(run, source, "metric", seen_metrics)
            out["gone"] += mark_gone(run, source, "label", seen_labels)
        db.session.commit()
    except SourceStopped as ex:
        db.session.commit()
        out.update(complete=False, stopped=str(ex))
    finally:
        conn.close()
    out.update(throttle.stats())
    return out
