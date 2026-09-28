"""Learning the metrics of a Prometheus / Mimir database (promagg), gently.

Every run (cheap, for every metric): the list of metrics and the metadata API (type, unit,
HELP text) - two or three requests for the whole database.

Profiles (for the metrics that are due): a metric is profiled when it is new, and then on its
own day of a rolling cycle of learn.profile_every_days days (its name decides the day), so
that the work of a week is spread over the week. A profile costs about 3 requests:
  * how many series have a sample now (count);
  * the value statistics over the window, in one query: rates for counters, p50 / p95 for
    histograms, min / max / avg for gauges (skipped above learn.stats_max_series series, one
    hour of data above 50,000);
  * a sample of at most learn.series_sample series: the labels and their values (the values
    are what the relations with index fields are measured on).
The depth of the history (first sample) is measured once (series index only, no samples read)
and then every 30 days; the last sample of a metric that stopped, when it stops.

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


def profile_metric(conn: Any, name: str, meta: Any, kind: str, hours: int, old: dict[str, Any]) -> dict[str, Any]:
    sel = '{__name__="%s"}' % name
    now = conn.now_ms()
    stats: dict[str, Any] = {"profiled_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                             "profiled_on": dt.date.today().isoformat()}
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
    else:
        first = _first_sample(conn, sel, end - 60 * 86_400_000, end)
        stats["data_from"] = _iso(first, conn)
        stats["history_checked_on"] = dt.date.today().isoformat()
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
    return out, complete


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
                if time.time() >= deadline:
                    out["complete"] = False
            else:
                if kind == "unknown":
                    kind = kind_from_name(name)
                    facts["metric_type"] = kind
                stats = profile_metric(conn, name, None, kind, hours, (old.stats or {}) if old is not None else {})
                end_ms = stats.pop("_end_ms", conn.now_ms())
                labels, _complete = label_sample(conn, name, end_ms) if "note" not in stats else ({}, True)
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
