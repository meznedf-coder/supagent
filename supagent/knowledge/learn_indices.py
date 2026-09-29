"""Learning the indices of an OpenSearch database (osagg), gently.

Every run (cheap): the list of indices, aliases and data streams, and their field mappings.
Dated or rolled-over indices (logs-otel-2026.09.27, app-logs-2026.09, traces-000123...) are
one family, learned through its latest member and queried with its pattern (logs-otel-*):
they are not new every day and gone the next.

Profiles (for the indices that are due: new ones, then each on its day of the rolling cycle of
learn.profile_every_days days): on a sample of learn.sample_docs documents per shard, for
every field its fill rate, distinct values, ranges and, when a keyword has at most 1,000
values, the values themselves; the fields are asked learn.fields_per_request at a time (an
OTel index with hundreds of attributes costs several small requests, not one huge one).

Every request goes through the throttle (rate limit, breaker): see throttle.py.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Any

from superset import db

from supagent import settings
from supagent.knowledge.learn_metrics import due
from supagent.knowledge.store import mark_gone, matches, upsert
from supagent.knowledge.throttle import Proxy, SourceStopped, Throttle
from supagent.models import KObject, Run, Source

log = logging.getLogger(__name__)
VALUES_KEPT = 1000
# a date or a rollover counter at the end of an index name
ROLLOVER = re.compile(r"^(?P<base>.+?)[-_.](?:\d{4}[.\-_]?\d{2}(?:[.\-_]?\d{2})?(?:[.\-_]?\d{2})?|\d{6})$")


def family_of(name: str) -> str | None:
    """logs-otel-2026.09.27 -> logs-otel-*, traces-000123 -> traces-*, else None."""
    if name.startswith("."):
        return None
    m = ROLLOVER.match(name)
    return f"{m.group('base')}-*" if m and not m.group("base").endswith("*") else None


def group_indices(names: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(object name -> index to profile, family pattern -> members). A pattern is used only when
    at least two indices share it; the latest member (by name: dates and counters sort) is read."""
    families: dict[str, list[str]] = {}
    for n in names:
        fam = family_of(n)
        if fam:
            families.setdefault(fam, []).append(n)
    families = {f: sorted(m) for f, m in families.items() if len(m) >= 2}
    in_family = {n for members in families.values() for n in members}
    objects = {n: n for n in names if n not in in_family}
    for fam, members in families.items():
        objects[fam] = members[-1]
    return objects, families


def _ms_to_iso(v: Any, tz: Any = None) -> str | None:
    """Epoch ms -> local time of the database (osagg's timezone), as its SQL shows it."""
    if v is None:
        return None
    t = dt.datetime.fromtimestamp(float(v) / 1000, dt.timezone.utc)
    if tz is None:
        return t.strftime("%Y-%m-%d %H:%M UTC")
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def computed_text(f: Any) -> str:
    """What a column the connector computes is (osagg's business-date label and time)."""
    kind, *args = str(f.virtual).split(":")
    if kind == "shift" and len(args) >= 2:
        return f"computed by the connector: {args[1]} moved onto the D-1 position date of {args[0]}"
    return f"computed by the connector: the business-day label of {args[0] if args else 'a date'} (D, D-1, W-1, Y-1...)"


def computed_fields(conn: Any, index: str) -> dict[str, dict[str, Any]]:
    """The columns the connector computes for an index (none: no request)."""
    if not (getattr(conn, "label_column", None) or getattr(conn, "label_time_column", None)):
        return {}
    meta = conn.table_meta(index)
    return {f.name: {"type": f.sql_type.lower(), "computed": computed_text(f)}
            for f in (meta.fields.values() if meta is not None else []) if f.virtual}


def profile_index(conn: Any, index: str, meta: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    sample_docs = int(settings.get("learn.sample_docs"))
    per_request = max(5, int(settings.get("learn.fields_per_request")))
    tz = getattr(conn, "tz", None)
    fields = [f for f in meta.fields.values() if not f.virtual and f.agg_field and f.name != "_id"]
    fstats: dict[str, dict[str, Any]] = {f.name: {"type": f.os_type} for f in fields}
    sampled = 0
    small: list[Any] = []
    for i in range(0, len(fields), per_request):
        batch = fields[i:i + per_request]
        # the documents the aggregations saw (with terminate_after, hits.total counts others)
        aggs: dict[str, Any] = {"_seen": {"filter": {"match_all": {}}}}
        for f in batch:
            aggs[f"n:{f.name}"] = {"value_count": {"field": f.agg_field}}
            if f.sql_type == "VARCHAR":
                aggs[f"c:{f.name}"] = {"cardinality": {"field": f.agg_field, "precision_threshold": 3000}}
            elif f.is_numeric or f.is_date:
                aggs[f"s:{f.name}"] = {"stats": {"field": f.agg_field}}
        res = conn.transport.search(index, {"size": 0, "terminate_after": sample_docs, "aggs": aggs,
                                            "timeout": f"{int(settings.get('learn.request_timeout'))}s"})
        aggr = res.get("aggregations") or {}
        seen = (aggr.get("_seen") or {}).get("doc_count") or 0
        sampled = max(sampled, seen)
        for f in batch:
            st = fstats[f.name]
            count = (aggr.get(f"n:{f.name}") or {}).get("value")
            if seen and count is not None:
                st["filled_pct"] = round(100.0 * count / seen, 1)
            if f.sql_type == "VARCHAR":
                card = (aggr.get(f"c:{f.name}") or {}).get("value")
                st["cardinality"] = card
                if card is not None and card <= VALUES_KEPT:
                    small.append(f)
            else:
                s = aggr.get(f"s:{f.name}") or {}
                if s.get("count"):
                    if f.is_date:
                        st.update(min=_ms_to_iso(s.get("min"), tz), max=_ms_to_iso(s.get("max"), tz))
                    else:
                        st.update(min=s.get("min"), max=s.get("max"), avg=round(s.get("avg") or 0, 4))
    for i in range(0, len(small), per_request):
        batch = small[i:i + per_request]
        terms = {f"t:{f.name}": {"terms": {"field": f.agg_field, "size": VALUES_KEPT + 1}} for f in batch}
        tres = conn.transport.search(index, {"size": 0, "terminate_after": sample_docs, "aggs": terms})
        for f in batch:
            buckets = ((tres.get("aggregations") or {}).get(f"t:{f.name}") or {}).get("buckets") or []
            fstats[f.name]["values"] = sorted(str(b["key"]) for b in buckets)
            fstats[f.name]["top"] = [[str(b["key"]), b["doc_count"]] for b in buckets[:12]]
    info: dict[str, Any] = {"sampled_docs": sampled, "profiled_on": dt.date.today().isoformat(),
                            "profiled_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    try:
        full = conn.transport.search(index, {"size": 0, "track_total_hits": True})
        info["docs"] = full["hits"]["total"]["value"]
    except SourceStopped:
        raise
    except Exception:  # pylint: disable=broad-except
        pass
    for f in meta.fields.values():            # computed by the connector (osagg): usable in SQL, not stored
        if f.virtual and f.name not in fstats:
            fstats[f.name] = {"type": f.sql_type.lower(), "computed": computed_text(f)}
    dates = [f for f in fields if f.is_date]
    if dates:
        tf = next((f for f in dates if re.search(r"timestamp|time|date", f.name, re.I)), dates[0])
        info["time_field"] = tf.name
        info["time_range"] = [fstats[tf.name].get("min"), fstats[tf.name].get("max")]
        if tz is not None:
            info["timezone"] = str(tz)
    return info, fstats


def learn_indices(run: Run, source: Source, database: Any, deadline: float, progress: Any = None) -> dict[str, Any]:
    from supagent.tools import _connection

    include, exclude = settings.get("learn.indices"), settings.get("learn.indices_exclude")
    limit = int(settings.get("learn.max_objects"))
    every = int(settings.get("learn.profile_every_days"))
    throttle = Throttle(int(settings.get("learn.max_requests_per_minute")), int(settings.get("learn.stop_after_errors")),
                        name=database.database_name)
    conn = _connection(database, extract=False)
    conn.transport = Proxy(conn.transport, throttle, ("search", "count", "get_mapping", "list_tables"))
    out: dict[str, Any] = {"indices": 0, "profiled": 0, "fields": 0, "not_due": 0, "complete": True}
    if progress is not None:
        progress(phase="indices and fields")
    today = dt.date.today()
    families: dict[str, list[str]] = {}
    try:
        # a pattern (osagg-lab-*) is a table over several indices: its fields are the indices' own
        names = [n for n in conn.list_tables() if matches(n, include, exclude) and not any(c in n for c in "*?[")]
        listed_all = len(names) <= limit
        if not listed_all:
            names, out["complete"] = names[:limit], False
        if settings.get("learn.group_rollover"):
            objects, families = group_indices(names)
        else:
            objects = {n: n for n in names}
        known = {o.name: o for o in db.session.query(KObject).filter_by(source_id=source.id, kind="index")}
        seen_idx = {("", n) for n in objects}          # the listing is complete: gone indices are known
        seen_fields: set[tuple[str, str]] = set()

        def keep_fields(obj_name: str) -> None:
            """Not profiled this time: its fields stay as they are."""
            seen_fields.update((obj_name, f.name) for f in db.session.query(KObject).filter_by(
                source_id=source.id, kind="field", parent=obj_name))

        order = sorted(objects, key=lambda n: (n in known, ((known[n].stats or {}).get("profiled_on") or "")
                                               if n in known else ""))
        for obj_name in order:
            index = objects[obj_name]
            old = known.get(obj_name)
            out["indices"] += 1
            if time.time() >= deadline or not due(obj_name, old.stats if old is not None else None, every, today):
                if time.time() >= deadline:
                    out["complete"] = False
                keep_fields(obj_name)
                if old is not None and time.time() < deadline and not any(
                        (f.stats or {}).get("computed") for f in db.session.query(KObject).filter_by(
                            source_id=source.id, kind="field", parent=obj_name)):
                    try:                               # learned before they were: at once, not at the next profile
                        for fname, st in computed_fields(conn, index).items():
                            ftype = st.pop("type", None)
                            upsert(run, source, "field", obj_name, fname, {"data_type": ftype, "stats": st})
                            seen_fields.add((obj_name, fname))
                            out["fields"] += 1
                    except SourceStopped:
                        raise
                    except Exception as ex:  # pylint: disable=broad-except
                        log.info("supagent learn: computed columns of %s: %s", index, ex)
                if old is None:
                    upsert(run, source, "index", "", obj_name, {"stats": _family_info(obj_name, families, index)})
                else:
                    old.last_seen, old.gone_at = dt.datetime.utcnow(), None
                    out["not_due"] += 1
                db.session.commit()
                continue
            try:
                meta = conn.table_meta(index)
                info, fstats = profile_index(conn, index, meta) if meta is not None else (None, None)
            except SourceStopped:
                raise
            except Exception as ex:  # pylint: disable=broad-except
                log.warning("supagent learn: index %s: %s", index, ex)
                out.setdefault("errors", {})[obj_name] = str(ex)[:300]
                info = None
            if info is None:
                keep_fields(obj_name)
                continue
            info.update(_family_info(obj_name, families, index))
            upsert(run, source, "index", "", obj_name, {"stats": info})
            out["profiled"] += 1
            for fname, st in fstats.items():
                ftype = st.pop("type", None)
                upsert(run, source, "field", obj_name, fname, {"data_type": ftype, "stats": st})
                seen_fields.add((obj_name, fname))
                out["fields"] += 1
            db.session.commit()
        if listed_all:
            out["gone"] = mark_gone(run, source, "index", seen_idx) + mark_gone(run, source, "field", seen_fields)
        db.session.commit()
    except SourceStopped as ex:
        db.session.commit()
        out.update(complete=False, stopped=str(ex))
    finally:
        conn.close()
    out["families"] = len(families)
    out.update(throttle.stats())
    return out


def _family_info(obj_name: str, families: dict[str, list[str]], index: str) -> dict[str, Any]:
    members = families.get(obj_name)
    if not members:
        return {}
    return {"family": True, "members": len(members), "latest": index, "first": members[0]}
