"""Backups of the knowledge, and their restore, whole or by part (0.9).

What people and the learning put into the agent's knowledge is long to make again: the categories with their
values, what each is part of, the items they were given to and the interactions of the System map; the catalog
with its versions; the team memory; the documents; the notes; the Context; the learned answers and investigation
paths; the descriptions of the data's objects; the settings. A backup is one zip file holding the latest state of
all of it, a part per folder, written to a directory of the server (backup.dir). Every day at backup.hour one is
made (backup.enabled) and the last backup.keep are kept: the history, to go back to a day before a mistake.

A restore takes one part or several from one file and puts them back as they were then (the present rows of the
part are replaced), after saving the present state in a backup of its own: a restore can be undone. A part is
restored alone (the categories without the catalog): what names an item that no longer exists is simply not shown.

  categories   the values of the categories, what they are part of, the items they are given to, the relations
               and the interactions of the System map, the map's arrangement, the categories themselves
               (categories.custom, categories.fields, categories.about)
  catalog      the catalog's entries and their versions
  memory       the team's and the users' memories
  documents    the documents and sites (their texts; a site's sign-in secret is never written to a file: on the
               same Superset it is kept, elsewhere it is entered again)
  notes        the notes
  context      the Context pages
  learned      the learned answers and the investigation paths, the examples, the words that lead to a table,
               the kinds of work people confirmed
  dictionary   the descriptions of the data's objects (written by people or by the LLM), their units, other names
               and categories: put back on the objects of the same name (the objects themselves are learned again
               from the data)
  settings     the settings (no secret)
  vectors      (backup.vectors, off) the vectors of the search pieces: a full restore then needs no embedding

The chats, the usage figures and the runs are not knowledge: they are not in a backup. A file never holds a secret.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import logging
import os
import re
import time
import zipfile
from typing import Any, Iterator

import sqlalchemy as sa
from superset import db

from supagent import settings

log = logging.getLogger(__name__)
FORMAT = 1
NAME = re.compile(r"^supagent-knowledge-\d{8}-\d{6}(?:-[a-z][a-z-]{0,30})?\.zip$")
PARTS = ("categories", "catalog", "memory", "documents", "notes", "context", "learned", "dictionary", "settings",
         "vectors")
CATEGORY_SETTINGS = ("categories.custom", "categories.fields", "categories.about")
BATCH = 500
BEFORE_RESTORE = 5        # backups made before a restore that are kept


def _models() -> dict[str, list[Any]]:
    from supagent import models as M

    return {"categories": [M.Facet, M.Tag, M.Link, M.Classified],
            "catalog": [M.Entry, M.EntryVersion, M.Document],
            "memory": [M.Memory],
            "documents": [M.Doc],
            "notes": [M.Note],
            "context": [M.ContextPage],
            "learned": [M.Recipe, M.Example, M.Association]}


def directory() -> str:
    """Where the backups are written: backup.dir, else supagent-backups in Superset's home."""
    given = str(settings.get("backup.dir") or "").strip()
    if given:
        return os.path.abspath(os.path.expanduser(given))
    home = os.environ.get("SUPERSET_HOME") or os.path.join(os.path.expanduser("~"), ".superset")
    return os.path.join(home, "supagent-backups")


def _secret(col: Any) -> bool:
    return "Encrypted" in type(col.type).__name__


def _out(col: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__b64__": base64.b64encode(bytes(value)).decode()}
    return value


def _in(col: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"__b64__"}:
        return base64.b64decode(value["__b64__"])
    if isinstance(col.type, sa.DateTime) and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    if isinstance(col.type, sa.Date) and isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    return value


def _rows(model: Any) -> Iterator[dict[str, Any]]:
    table = model.__table__
    cols = [c for c in table.columns if not _secret(c)]
    key = list(table.primary_key.columns)
    q = db.session.execute(sa.select(*cols).order_by(*key).execution_options(yield_per=BATCH))
    for row in q:
        yield {c.name: _out(c, v) for c, v in zip(cols, row)}


# --------------------------------------------------------------------------------------------- #
# the backup
# --------------------------------------------------------------------------------------------- #
def _dictionary_rows() -> Iterator[dict[str, Any]]:
    from supagent.models import KObject, Source

    names = dict(db.session.query(Source.id, Source.database_name))
    q = db.session.query(KObject.source_id, KObject.kind, KObject.parent, KObject.name, KObject.description,
                         KObject.description_source, KObject.verified, KObject.unit, KObject.synonyms,
                         KObject.category).filter(sa.or_(KObject.description.isnot(None), KObject.synonyms.isnot(None),
                                                         KObject.category.isnot(None), KObject.unit.isnot(None)))
    for sid, kind, parent, name, description, source, verified, unit, synonyms, category in q.yield_per(BATCH):
        if sid in names:
            yield {"database": names[sid], "kind": kind, "parent": parent or "", "name": name, "description": description,
                   "description_source": source, "verified": bool(verified), "unit": unit, "synonyms": synonyms,
                   "category": category}


def _settings_rows() -> dict[str, Any]:
    out = {}
    for spec in settings.SPECS:
        if spec.secret:
            continue
        from supagent.models import Setting

        row = db.session.get(Setting, spec.key)
        if row is not None and row.value is not None:
            out[spec.key] = row.value
    return out


def _vector_rows() -> Iterator[dict[str, Any]]:
    from supagent.models import Chunk

    q = db.session.query(Chunk.ref, Chunk.content_hash, Chunk.embed_model, Chunk.vector).filter(Chunk.vector.isnot(None))
    for ref, h, model, vector in q.yield_per(BATCH):
        yield {"ref": ref, "content_hash": h, "embed_model": model, "vector": base64.b64encode(bytes(vector)).decode()}


def _route_rows() -> Iterator[dict[str, Any]]:
    from supagent.models import Route

    table = Route.__table__
    cols = list(table.columns)
    for row in db.session.execute(sa.select(*cols).where(table.c.signal.isnot(None)).order_by(table.c.id)
                                  .execution_options(yield_per=BATCH)):
        yield {c.name: _out(c, v) for c, v in zip(cols, row)}


def _write_lines(z: zipfile.ZipFile, name: str, rows: Iterator[dict[str, Any]]) -> int:
    n = 0
    with z.open(name, "w") as fh:
        for row in rows:
            fh.write((json.dumps(row, default=str, ensure_ascii=False) + "\n").encode())
            n += 1
    return n


def make(reason: str = "manual", by: str = "", vectors: bool | None = None, tag: str = "",
         progress: Any = None) -> dict[str, Any]:
    """Write a backup now; what it holds: {"name", "path", "bytes", "parts": {part: {table: rows}}, "seconds"}.
    `progress(part, counts)` is called after each part."""
    from supagent import __version__
    from supagent.models import SCHEMA_VERSION, Meta, Source

    t0 = time.time()
    folder = directory()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    name = f"supagent-knowledge-{stamp}" + (f"-{tag}" if tag else "") + ".zip"
    path = os.path.join(folder, name)
    tmp = path + ".part"
    vectors = bool(settings.get("backup.vectors")) if vectors is None else vectors
    parts: dict[str, dict[str, int]] = {}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for part, models in _models().items():
            counts = {m.__tablename__: _write_lines(z, f"{part}/{m.__tablename__}.jsonl", _rows(m)) for m in models}
            if part == "categories":
                row = db.session.get(Meta, "system_map")
                z.writestr("categories/meta.json", json.dumps({"system_map": row.value if row is not None else None}))
                z.writestr("categories/settings.json", json.dumps({k: v for k, v in _settings_rows().items()
                                                                    if k in CATEGORY_SETTINGS}))
            if part == "learned":
                counts["supagent_route"] = _write_lines(z, "learned/supagent_route.jsonl", _route_rows())
            parts[part] = counts
            if progress:
                progress(part, counts)
        parts["dictionary"] = {"objects": _write_lines(z, "dictionary/objects.jsonl", _dictionary_rows())}
        if progress:
            progress("dictionary", parts["dictionary"])
        conf = _settings_rows()
        z.writestr("settings/settings.json", json.dumps(conf))
        parts["settings"] = {"settings": len(conf)}
        if vectors:
            parts["vectors"] = {"supagent_chunk": _write_lines(z, "vectors/supagent_chunk.jsonl", _vector_rows())}
            if progress:
                progress("vectors", parts["vectors"])
        manifest = {"format": FORMAT, "supagent": __version__, "schema": SCHEMA_VERSION,
                    "created_at": dt.datetime.utcnow().isoformat(timespec="seconds"), "by": by, "reason": reason,
                    "parts": parts, "databases": {str(i): n for i, n in db.session.query(Source.database_id, Source.database_name)}}
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    removed = prune()
    return {"name": name, "path": path, "bytes": os.path.getsize(path), "parts": parts, "removed": removed,
            "seconds": round(time.time() - t0, 1)}


def prune() -> list[str]:
    """The oldest backups beyond backup.keep are removed (those made before a restore: beyond BEFORE_RESTORE)."""
    keep = max(1, int(settings.get("backup.keep") or 14))
    files = sorted((f for f in os.listdir(directory()) if NAME.match(f)), reverse=True)
    usual = [f for f in files if "-before-restore" not in f]
    safety = [f for f in files if "-before-restore" in f]
    gone = usual[keep:] + safety[BEFORE_RESTORE:]
    for f in gone:
        try:
            os.remove(os.path.join(directory(), f))
        except OSError:
            log.warning("supagent backup: %s was not removed", f, exc_info=True)
    return gone


def manifest_of(path: str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("manifest.json"))


def listing() -> list[dict[str, Any]]:
    """The backups of the directory, the latest first: name, when, size, who, why, what each part holds."""
    folder = directory()
    out = []
    if not os.path.isdir(folder):
        return out
    for f in sorted((f for f in os.listdir(folder) if NAME.match(f)), reverse=True):
        path = os.path.join(folder, f)
        item: dict[str, Any] = {"name": f, "bytes": os.path.getsize(path)}
        try:
            m = manifest_of(path)
            item.update(created_at=m.get("created_at"), by=m.get("by"), reason=m.get("reason"), supagent=m.get("supagent"),
                        schema=m.get("schema"), parts={p: sum(c.values()) for p, c in (m.get("parts") or {}).items()})
        except Exception as ex:  # pylint: disable=broad-except   (a file that is not a backup: said)
            item["error"] = f"not readable: {str(ex)[:120]}"
        out.append(item)
    return out


def path_of(name: str) -> str:
    """The file of a backup by its name (nothing else: no path may be given)."""
    if not NAME.match(str(name or "")):
        raise ValueError("not the name of a backup")
    path = os.path.join(directory(), name)
    if not os.path.isfile(path):
        raise ValueError(f"no backup {name} in {directory()} on this server")
    return path


# --------------------------------------------------------------------------------------------- #
# the restore
# --------------------------------------------------------------------------------------------- #
def _lines(z: zipfile.ZipFile, name: str) -> Iterator[dict[str, Any]]:
    try:
        fh = z.open(name)
    except KeyError:
        return
    with fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            raw = raw.strip()
            if raw:
                yield json.loads(raw)


def _secrets(model: Any) -> dict[Any, dict[str, Any]]:
    """The secrets of a table's rows (a site's sign-in), by id: they are in no file, and stay on the rows that a
    restore puts back."""
    table = model.__table__
    hidden = [c for c in table.columns if _secret(c)]
    out: dict[Any, dict[str, Any]] = {}
    if hidden and "id" in table.c:
        for obj in db.session.query(model):
            vals = {c.name: getattr(obj, c.name) for c in hidden if getattr(obj, c.name) is not None}
            if vals:
                out[obj.id] = vals
        db.session.expunge_all()
    return out


def _put_back(model: Any, rows: Iterator[dict[str, Any]], kept: dict[Any, dict[str, Any]]) -> int:
    """The rows of the file into the (emptied) table; `kept`: the secrets of the rows that were there."""
    table = model.__table__
    cols = {c.name: c for c in table.columns if not _secret(c)}
    n, batch = 0, []
    for row in rows:
        batch.append({k: _in(cols[k], v) for k, v in row.items() if k in cols})
        if len(batch) >= BATCH:
            db.session.execute(table.insert(), batch)
            n, batch = n + len(batch), []
    if batch:
        db.session.execute(table.insert(), batch)
        n += len(batch)
    for i, vals in kept.items():
        obj = db.session.get(model, i)
        if obj is not None:
            for k, v in vals.items():
                setattr(obj, k, v)
    db.session.flush()
    if db.engine.dialect.name == "postgresql" and "id" in table.c and isinstance(table.c.id.type, sa.Integer):
        db.session.execute(sa.text(
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), COALESCE((SELECT MAX(id) FROM {table.name}), 1))"))
    return n


def _restore_part(z: zipfile.ZipFile, part: str) -> dict[str, int]:
    from supagent.models import Chunk, KObject, Meta, Route, Source

    out: dict[str, int] = {}
    models = _models().get(part)
    if models:
        kept = {m: _secrets(m) for m in models}
        for m in reversed(models):                    # what refers to another table goes first
            db.session.execute(m.__table__.delete())
        for m in models:
            out[m.__tablename__] = _put_back(m, _lines(z, f"{part}/{m.__tablename__}.jsonl"), kept[m])
    if part == "categories":
        try:
            meta = json.loads(z.read("categories/meta.json"))
            row = db.session.get(Meta, "system_map")
            if meta.get("system_map") is None:
                if row is not None:
                    db.session.delete(row)
            elif row is None:
                db.session.add(Meta(key="system_map", value=meta["system_map"]))
            else:
                row.value = meta["system_map"]
            for k, v in json.loads(z.read("categories/settings.json")).items():
                if k in CATEGORY_SETTINGS:
                    settings.set_value(k, v, by="restore")
            for k in CATEGORY_SETTINGS:               # one the backup did not have: as it was then (the default)
                if k not in json.loads(z.read("categories/settings.json")):
                    settings.set_value(k, None, by="restore")
        except KeyError:
            pass
    elif part == "learned":
        cols = {c.name: c for c in Route.__table__.columns}
        n = 0
        for row in _lines(z, "learned/supagent_route.jsonl"):
            db.session.merge(Route(**{k: _in(cols[k], v) for k, v in row.items() if k in cols}))
            n += 1
        out["supagent_route"] = n
    elif part == "dictionary":
        by_name = {name: sid for sid, name in db.session.query(Source.id, Source.database_name)}
        n = missing = 0
        for row in _lines(z, "dictionary/objects.jsonl"):
            sid = by_name.get(row.get("database"))
            objs = [] if sid is None else db.session.query(KObject).filter(
                KObject.source_id == sid, KObject.kind == row["kind"], KObject.name == row["name"],
                KObject.parent == (row.get("parent") or "")).all()
            if not objs and sid is not None and not row.get("parent"):
                objs = db.session.query(KObject).filter(KObject.source_id == sid, KObject.kind == row["kind"],
                                                        KObject.name == row["name"], KObject.parent.is_(None)).all()
            if not objs:
                missing += 1
                continue
            for o in objs:
                o.description, o.description_source = row.get("description"), row.get("description_source")
                o.verified, o.unit = bool(row.get("verified")), row.get("unit")
                o.synonyms, o.category = row.get("synonyms"), row.get("category")
                n += 1
        out.update(objects=n, not_learned_now=missing)
    elif part == "settings":
        conf = json.loads(z.read("settings/settings.json"))
        n = 0
        for k, v in conf.items():
            spec = settings.BY_KEY.get(k)
            if spec is None or spec.secret or k.startswith("backup."):
                continue                               # where and when the backups are made: of this server
            settings.set_value(k, v, by="restore")
            n += 1
        out["settings"] = n
    elif part == "vectors":
        n = 0
        for row in _lines(z, "vectors/supagent_chunk.jsonl"):
            n += db.session.query(Chunk).filter(Chunk.ref == row["ref"], Chunk.content_hash == row["content_hash"]).update(
                {"embed_model": row["embed_model"], "vector": base64.b64decode(row["vector"])}, synchronize_session=False)
        out["supagent_chunk"] = n
    return out


def restore(name: str, parts: list[str] | None = None, by: str = "", safety: bool = True,
            progress: Any = None) -> dict[str, Any]:
    """Put back the parts asked (all of the file's when none is given) as they were in that backup. The present
    state is saved first in a backup of its own (`safety`). Each part is one transaction: a part that fails
    leaves its present rows as they were, and the parts before it restored."""
    from supagent.knowledge.freshness import touch
    from supagent.models import SCHEMA_VERSION

    path = path_of(name)
    manifest = manifest_of(path)
    if int(manifest.get("format") or 0) > FORMAT or int(manifest.get("schema") or 0) > SCHEMA_VERSION:
        raise ValueError(f"this backup was made by a later version (supagent {manifest.get('supagent')}): upgrade first")
    have = [p for p in PARTS if p in (manifest.get("parts") or {})]
    wanted = [p for p in (parts or have)]
    unknown = [p for p in wanted if p not in have]
    if unknown:
        raise ValueError(f"not in this backup: {', '.join(unknown)} (it has: {', '.join(have)})")
    t0 = time.time()
    out: dict[str, Any] = {"name": name, "parts": {}, "of": manifest.get("created_at")}
    if safety:
        out["saved_first"] = make(reason=f"before the restore of {name}", by=by, tag="before-restore")["name"]
        if progress:
            progress("the present state, saved first", {"file": out["saved_first"]})
    with zipfile.ZipFile(path) as z:
        for part in [p for p in PARTS if p in wanted]:
            try:
                out["parts"][part] = _restore_part(z, part)
                touch()
                db.session.commit()
            except Exception as ex:  # pylint: disable=broad-except
                db.session.rollback()
                log.exception("supagent restore: %s of %s", part, name)
                out["parts"][part] = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
                out["error"] = f"{part} was not restored: {str(ex)[:200]}"
            if progress:
                progress(part, out["parts"][part])
    try:                                              # the search follows what was put back
        from supagent.knowledge.index import sync

        out["search"] = sync()
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        out["search"] = {"error": str(ex)[:200]}
    out["seconds"] = round(time.time() - t0, 1)
    return out


# --------------------------------------------------------------------------------------------- #
# the runs (listed in the settings like the learning's) and the schedule
# --------------------------------------------------------------------------------------------- #
def _run(kind: str, reason: str, work: Any) -> dict[str, Any]:
    import traceback

    from supagent.knowledge.learner import Steps, _start_run, running_run
    from supagent.models import Run

    busy = running_run()
    run_id = _start_run(reason, kind=kind) if busy is None else None
    if run_id is None:
        busy = busy or running_run()
        return {"run": busy.id if busy else None, "status": "skipped",
                "reason": f"run {busy.id} is still {busy.status}" if busy else "another run started at the same time"}
    stats: dict[str, Any] = {}
    steps = Steps(run_id, stats)
    status, error = "done", None
    t0 = time.time()

    def progress(part: str, counts: dict[str, Any]) -> None:
        steps.begin(part)
        steps.end(**{k: v for k, v in counts.items() if isinstance(v, (int, str))})

    try:
        res = work(progress)
        stats.update({k: v for k, v in res.items() if k not in ("parts", "path")})
        if res.get("error"):
            status = "partial"
    except Exception as ex:  # pylint: disable=broad-except
        db.session.rollback()
        log.exception("supagent %s: run %s failed", kind, run_id)
        status, error = "error", "".join(traceback.format_exception_only(type(ex), ex))[-2000:]
        steps.interrupted(error)
    stats["seconds"] = round(time.time() - t0, 1)
    run = db.session.get(Run, run_id)
    run.status, run.error, run.stats, run.finished_at = status, error, stats, dt.datetime.utcnow()
    db.session.commit()
    return {"run": run_id, "status": status, "error": error, **stats}


def run_backup(reason: str = "manual", by: str = "", vectors: bool | None = None) -> dict[str, Any]:
    return _run("backup", reason, lambda progress: make(reason=reason, by=by, vectors=vectors, progress=progress))


def run_restore(name: str, parts: list[str] | None = None, by: str = "") -> dict[str, Any]:
    return _run("restore", f"restore {name}"[:64], lambda progress: restore(name, parts, by=by, progress=progress))


def due(now: dt.datetime | None = None) -> bool:
    """The daily backup is due: enabled, its hour has come (the server's clock), its days, none made today."""
    if not settings.get("backup.enabled"):
        return False
    now = now or dt.datetime.now()
    wanted = [str(d).strip().lower()[:3] for d in settings.get("backup.days") or []]
    if wanted and now.strftime("%a").lower()[:3] not in wanted:
        return False
    if now.hour < int(settings.get("backup.hour") or 0):
        return False
    today = now.strftime("%Y%m%d")
    utc_today = dt.datetime.utcnow().strftime("%Y%m%d")
    try:
        names = [f for f in os.listdir(directory()) if NAME.match(f) and "-before-restore" not in f]
    except OSError:
        names = []
    return not any(f.startswith(f"supagent-knowledge-{today}") or f.startswith(f"supagent-knowledge-{utc_today}")
                   for f in names)
