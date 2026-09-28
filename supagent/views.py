"""Pages and JSON API inside Superset (Flask-AppBuilder views, Superset's login, CSRF and
permissions):

  /supagent/                 chat, the tab "Chat" (permission "can read / can write on AIAgent")
  /supagent/dictionary/      the learned data dictionary ("can read / can write on AIAgentDictionary")
  /supagent/admin/           settings, LLM test, learning runs, catalog (admins only)

`superset supagent init` creates the role "AI Agent" with the chat and the dictionary; admins
have everything. The API answers only with the caller's own conversations, and the dictionary
only shows the databases the caller may query.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from flask import Response, g, request
from flask_appbuilder import BaseView, expose
from flask_appbuilder.security.decorators import has_access, has_access_api

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_VIEW = "AIAgentAdmin"
SAFE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "b", "i", "strong", "em", "p", "br", "span", "div", "blockquote",
             "code", "pre", "hr", "ul", "ol", "li", "dd", "dt", "dl", "a", "table", "thead", "tbody", "tr", "th",
             "td", "del", "sup", "sub"}
SAFE_ATTRS = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}}


def _json(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, default=str, ensure_ascii=False), status=status, mimetype="application/json")


def render_markdown(text: str) -> str:
    """Answer Markdown -> safe HTML (tables and code blocks kept, anything else stripped)."""
    import markdown
    import nh3

    html = markdown.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"])
    return nh3.clean(html, tags=SAFE_TAGS, attributes=SAFE_ATTRS, link_rel="noopener noreferrer")


class NotFound(Exception):
    """An object that does not exist or is not the caller's: answered 404."""


def _not_found(_ex: Exception) -> Response:
    return _json({"error": "not found"}, 404)


def abort(code: int) -> None:
    raise NotFound()


def _sync_chunks(prefix: str) -> None:
    """The searchable pieces follow a change (words at once, vectors at the next indexing)."""
    try:
        from supagent.knowledge.index import embed_pending, sync

        out = sync((prefix,))
        if 0 < out.get("added", 0) + out.get("changed", 0) <= 64:
            embed_pending(limit=64)             # a few new pieces: their vectors at once
    except Exception:  # pylint: disable=broad-except
        from superset import db

        db.session.rollback()


def _is_admin() -> bool:
    from superset.extensions import security_manager

    try:
        return bool(security_manager.is_admin()) or bool(security_manager.can_access("can_write", ADMIN_VIEW))
    except Exception:  # pylint: disable=broad-except
        return False


def _nav(active: str) -> dict:
    from superset.extensions import security_manager

    return {"active": active, "user": g.user.username if getattr(g, "user", None) else "",
            "can_chat": security_manager.can_access("can_read", ChatView.class_permission_name),
            "can_dictionary": security_manager.can_access("can_read", KnowledgeView.class_permission_name),
            "is_admin": _is_admin()}


STALE_MINUTES = 35          # no progress for this long: the worker died (the Celery task's limit is 30)


def expire_stale(messages: list) -> bool:
    """Answers stuck in pending / running (worker restarted, task lost) become errors, and a
    stop that nobody honours becomes cancelled; True when one changed."""
    now = dt.datetime.utcnow()
    changed = False
    for m in messages:
        last = m.updated_at or m.created_at or now
        if m.status in ("pending", "running") and now - last > dt.timedelta(minutes=STALE_MINUTES):
            m.status, m.finished_at = "error", now
            m.content = (m.content or "") + ("\n\n" if m.content else "") + \
                f"(no progress for {STALE_MINUTES} minutes: the answer was lost, e.g. a worker restarted. Ask again.)"
            changed = True
        elif m.status == "cancelling" and now - last > dt.timedelta(minutes=2):
            m.status, m.finished_at, m.content = "cancelled", now, m.content or "(stopped)"
            changed = True
    return changed


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


class ChatView(BaseView):
    route_base = "/supagent"
    default_view = "index"
    class_permission_name = "AIAgent"
    method_permission_name = {"index": "read", "conversations": "read", "conversation": "read",
                              "delete_conversation": "write", "ask": "write", "message": "read",
                              "feedback": "write", "file": "read", "cancel": "write", "result_xlsx": "read",
                              "memory": "read", "add_memory": "write", "delete_memory": "write"}
    template_folder = os.path.join(HERE, "templates")

    @expose("/")
    @has_access
    def index(self) -> Any:
        return self.render_template("supagent/chat.html", nav=_nav("chat"))

    # ---- conversations of the caller only
    @staticmethod
    def _conversation(cid: int) -> Any:
        from superset import db

        from supagent.models import Conversation

        conv = db.session.get(Conversation, cid)
        if conv is None or conv.user_id != g.user.id:
            abort(404)
        return conv

    @staticmethod
    def _message_json(m: Any) -> dict:
        from superset import db

        from supagent.models import File

        files = []
        for f in m.files or []:
            item = dict(f)
            if f.get("id") and db.session.get(File, f["id"]) is not None:
                item["url"] = f"api/files/{f['id']}"
            files.append(item)
        final = m.status in ("done", "error", "cancelled")
        return {"id": m.id, "role": m.role, "status": m.status, "content": m.content or "",
                "html": render_markdown(m.content or "") if m.role == "assistant" else None,
                "steps": m.steps or [], "files": files, "feedback": m.feedback,
                "results": (m.results or []) if final else [],
                "created_at": m.created_at, "finished_at": m.finished_at}

    @expose("/api/conversations", methods=("GET",))
    @has_access_api
    def conversations(self) -> Response:
        from superset import db

        from supagent.models import Conversation

        rows = (db.session.query(Conversation).filter_by(user_id=g.user.id)
                .order_by(Conversation.updated_at.desc()).limit(100).all())
        return _json({"conversations": [{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows]})

    @expose("/api/conversations/<int:cid>", methods=("GET",))
    @has_access_api
    def conversation(self, cid: int) -> Response:
        from superset import db

        from supagent.models import Message

        conv = self._conversation(cid)
        msgs = db.session.query(Message).filter_by(conversation_id=conv.id).order_by(Message.id).all()
        if expire_stale(msgs):
            db.session.commit()
        return _json({"id": conv.id, "title": conv.title, "messages": [self._message_json(m) for m in msgs]})

    @expose("/api/conversations/<int:cid>", methods=("DELETE",))
    @has_access_api
    def delete_conversation(self, cid: int) -> Response:
        from superset import db

        from supagent.models import Example, File, Message

        conv = self._conversation(cid)
        ids = [m.id for m in db.session.query(Message.id).filter_by(conversation_id=conv.id)]
        if ids:
            db.session.query(File).filter(File.message_id.in_(ids)).delete(synchronize_session=False)
            db.session.query(Example).filter(Example.message_id.in_(ids)).delete(synchronize_session=False)
            db.session.query(Message).filter(Message.id.in_(ids)).delete(synchronize_session=False)
        db.session.delete(conv)
        db.session.commit()
        return _json({"deleted": cid})

    @expose("/api/ask", methods=("POST",))
    @has_access_api
    def ask(self) -> Response:
        from superset import db

        from supagent.models import Conversation, Message
        from supagent.tasks import dispatch_answer

        body = _body()
        question = str(body.get("question") or "").strip()
        if not question:
            return _json({"error": "empty question"}, 400)
        if len(question) > 8000:
            return _json({"error": "question too long (8000 characters at most)"}, 400)
        cid = body.get("conversation_id")
        if cid:
            conv = self._conversation(int(cid))
            busy = db.session.query(Message).filter(Message.conversation_id == conv.id,
                                                    Message.status.in_(("pending", "running", "cancelling"))).all()
            if expire_stale(busy):
                db.session.commit()
                busy = [m for m in busy if m.status in ("pending", "running", "cancelling")]
            if busy:
                return _json({"error": "the previous question of this conversation is still being answered"}, 409)
        else:
            conv = Conversation(user_id=g.user.id, title=question[:120])
            db.session.add(conv)
            db.session.flush()
        db.session.add(Message(conversation_id=conv.id, role="user", content=question, status="done"))
        answer = Message(conversation_id=conv.id, role="assistant", status="pending", steps=[])
        db.session.add(answer)
        conv.updated_at = dt.datetime.utcnow()
        db.session.commit()
        where = dispatch_answer(answer.id)
        return _json({"conversation_id": conv.id, "message_id": answer.id, "executor": where})

    @expose("/api/messages/<int:mid>", methods=("GET",))
    @has_access_api
    def message(self, mid: int) -> Response:
        from superset import db

        from supagent.models import Message

        m = db.session.get(Message, mid)
        if m is None:
            abort(404)
        self._conversation(m.conversation_id)
        if expire_stale([m]):
            db.session.commit()
        return _json(self._message_json(m))

    @expose("/api/messages/<int:mid>/cancel", methods=("POST",))
    @has_access_api
    def cancel(self, mid: int) -> Response:
        """Stop an answer: the agent stops before its next step."""
        from superset import db

        from supagent.models import Message

        m = db.session.get(Message, mid)
        if m is None:
            abort(404)
        self._conversation(m.conversation_id)
        if m.status == "pending":
            m.status, m.content, m.finished_at = "cancelled", "(stopped)", dt.datetime.utcnow()
        elif m.status == "running":
            m.status = "cancelling"
        db.session.commit()
        return _json({"status": m.status})

    @expose("/api/messages/<int:mid>/results/<int:n>.xlsx", methods=("GET",))
    @has_access_api
    def result_xlsx(self, mid: int, n: int) -> Response:
        """One query result of an answer as an Excel file (the rows the page shows)."""
        import tempfile

        from superset import db

        from supagent.models import Message
        from supagent.tools import _write_xlsx

        m = db.session.get(Message, mid)
        if m is None:
            abort(404)
        self._conversation(m.conversation_id)
        results = m.results or []
        if not 0 <= n < len(results):
            abort(404)
        res = results[n]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "result.xlsx")
            _write_xlsx(path, res.get("columns") or [], [tuple(r) for r in res.get("rows") or []],
                        {"generated": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "by": g.user.username,
                         "database": res.get("database") or "", "rows": len(res.get("rows") or []),
                         "truncated": "yes" if res.get("truncated") else "no", "query": res.get("sql") or ""})
            with open(path, "rb") as fh:
                data = fh.read()
        return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="result-{mid}-{n + 1}.xlsx"',
                                 "X-Content-Type-Options": "nosniff"})

    @staticmethod
    def _memory_json(m: Any, me: int) -> dict:
        return {"id": m.id, "scope": m.scope, "kind": m.kind, "text": m.text, "category": m.category,
                "status": m.status, "source": m.source, "mine": m.user_id == me, "created_at": m.created_at}

    @expose("/api/memory", methods=("GET",))
    @has_access_api
    def memory(self) -> Response:
        from superset import db

        from supagent.models import Memory

        me = g.user.id
        mine = (db.session.query(Memory).filter(Memory.scope == "user", Memory.user_id == me,
                                                Memory.status != "disabled").order_by(Memory.id.desc()).all())
        team = (db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "active")
                .order_by(Memory.id.desc()).limit(200).all())
        proposed = (db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "proposed",
                                                    Memory.user_id == me).all())
        return _json({"mine": [self._memory_json(m, me) for m in mine],
                      "team": [self._memory_json(m, me) for m in team],
                      "proposed": [self._memory_json(m, me) for m in proposed], "is_admin": _is_admin()})

    @expose("/api/memory", methods=("POST",))
    @has_access_api
    def add_memory(self) -> Response:
        from supagent.knowledge.memory import add

        body = _body()
        m = add(g.user.id, str(body.get("text") or ""), scope=str(body.get("scope") or "user"),
                kind=str(body.get("kind") or "preference"), category=body.get("category"), source="manual",
                approved_by=g.user.username if _is_admin() and body.get("scope") == "team" else None)
        if m is None:
            return _json({"error": "empty, or already remembered"}, 400)
        _sync_chunks("memory:")
        return _json({"memory": self._memory_json(m, g.user.id)})

    @expose("/api/memory/<int:mem_id>", methods=("DELETE",))
    @has_access_api
    def delete_memory(self, mem_id: int) -> Response:
        from superset import db

        from supagent.models import Memory

        m = db.session.get(Memory, mem_id)
        if m is None or (not _is_admin() and not (m.user_id == g.user.id and (m.scope == "user" or
                                                                              m.status == "proposed"))):
            abort(404)
        m.status = "disabled"
        db.session.commit()
        _sync_chunks("memory:")
        return _json({"disabled": mem_id})

    @expose("/api/messages/<int:mid>/feedback", methods=("POST",))
    @has_access_api
    def feedback(self, mid: int) -> Response:
        """+1 keeps the question and the SQL that answered it as an example for later questions."""
        from superset import db

        from supagent.models import Example, Message

        m = db.session.get(Message, mid)
        if m is None or m.role != "assistant":
            abort(404)
        self._conversation(m.conversation_id)
        value = int(_body().get("value") or 0)
        m.feedback = value if value in (-1, 1) else None
        db.session.query(Example).filter_by(message_id=m.id).delete(synchronize_session=False)
        kept = False
        if value == 1:
            sql = [s for s in (m.steps or []) if s.get("tool") == "execute_sql" and s.get("status") == "done"]
            question = (db.session.query(Message).filter(Message.conversation_id == m.conversation_id,
                                                         Message.id < m.id, Message.role == "user")
                        .order_by(Message.id.desc()).first())
            if sql and question is not None:
                req = (sql[-1].get("args") or {}).get("request") or sql[-1].get("args") or {}
                if req.get("sql"):
                    db.session.add(Example(question=question.content, sql=req.get("sql"),
                                           database_id=req.get("database_id"), message_id=m.id,
                                           tools=[s.get("tool") for s in m.steps or []]))
                    kept = True
        db.session.commit()
        from supagent.knowledge.experience import feedback as recipe_feedback

        recipes = recipe_feedback(m.id, value)
        _sync_chunks("recipe:")
        from supagent.tasks import dispatch_catalog, dispatch_memory

        if value == 1:
            dispatch_memory(m.id)                    # what is worth remembering from this exchange
        if recipes:
            dispatch_catalog()                       # a formula confirmed (or no longer certain)
        return _json({"feedback": m.feedback, "example_kept": kept, "recipes": recipes})

    @expose("/api/files/<int:fid>", methods=("GET",))
    @has_access_api
    def file(self, fid: int) -> Response:
        from superset import db

        from supagent.models import File, Message

        f = db.session.get(File, fid)
        if f is None:
            abort(404)
        m = db.session.get(Message, f.message_id)
        if m is None:
            abort(404)
        self._conversation(m.conversation_id)
        inline = (f.mime or "").startswith("image/") and not request.args.get("download")
        headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{f.name}\"",
                   "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
        return Response(f.data, mimetype=f.mime or "application/octet-stream", headers=headers)


def _visible_databases() -> set[int]:
    from supagent.security import visible_databases

    return visible_databases()


class _RecipesMixin:
    @expose("/api/recipes", methods=("GET",))
    @has_access_api
    def recipes(self) -> Response:
        from superset import db

        from supagent.models import Recipe

        visible, admin = _visible_databases(), _is_admin()
        q = db.session.query(Recipe).order_by(Recipe.last_used_at.desc())
        status = request.args.get("status")
        if status:
            q = q.filter(Recipe.status == status)
        out = []
        for r in q.limit(500):
            if not admin and (not r.database_id or r.database_id not in visible):
                continue                             # another database, or an unknown one
            out.append({"id": r.id, "question": r.question, "tool": r.tool, "database_id": r.database_id,
                        "target": r.target, "query": r.query, "seconds": r.seconds, "rows": r.rows,
                        "steps": r.steps, "status": r.status, "uses": r.uses, "created_at": r.created_at,
                        "last_used_at": r.last_used_at})
        return _json({"recipes": out, "is_admin": admin})

    @expose("/api/recipes/<int:rid>", methods=("POST", "DELETE"))
    @has_access_api
    def set_recipe(self, rid: int) -> Response:
        from superset import db

        from supagent.models import Recipe

        if not _is_admin():
            return _json({"error": "only admins may change the learned answers"}, 403)
        r = db.session.get(Recipe, rid)
        if r is None:
            abort(404)
        if request.method == "DELETE":
            db.session.delete(r)
            db.session.commit()
            return _json({"deleted": rid})
        status = str(_body().get("status") or "")
        if status not in ("auto", "confirmed", "rejected"):
            return _json({"error": "status: auto, confirmed or rejected"}, 400)
        r.status = status
        db.session.commit()
        return _json({"id": rid, "status": status})

    @expose("/api/timings", methods=("GET",))
    @has_access_api
    def timings(self) -> Response:
        from superset import db

        from supagent.models import QueryStat

        visible, admin = _visible_databases(), _is_admin()
        rows = (db.session.query(QueryStat).order_by(QueryStat.max_seconds.desc()).limit(300).all())
        return _json({"timings": [{"target": r.target, "database_id": r.database_id, "pattern": r.pattern,
                                   "calls": r.calls, "errors": r.errors, "avg_seconds": round((r.total_seconds or 0) /
                                                                                          max(r.calls or 1, 1), 2),
                                   "max_seconds": r.max_seconds, "last_error": r.last_error, "last_at": r.last_at}
                                  for r in rows if r.database_id in visible or (admin and not r.database_id)]})


class KnowledgeView(_RecipesMixin, BaseView):
    route_base = "/supagent/dictionary"
    default_view = "index"
    class_permission_name = "AIAgentDictionary"
    method_permission_name = {"index": "read", "summary": "read", "objects": "read", "obj": "read",
                              "changes": "read", "relations": "read", "edit": "write", "recipes": "read",
                              "set_recipe": "write", "timings": "read", "search": "read"}

    @expose("/api/search", methods=("GET",))
    @has_access_api
    def search(self) -> Response:
        from supagent.knowledge.search import search

        q = (request.args.get("q") or "").strip()
        kind = request.args.get("kind") or None
        if not q:
            return _json({"results": []})
        return _json({"results": search(q, k=min(int(request.args.get("k") or 12), 30),
                                        kinds=(kind,) if kind else None)})
    template_folder = os.path.join(HERE, "templates")

    @expose("/")
    @has_access
    def index(self) -> Any:
        return self.render_template("supagent/dictionary.html", nav=_nav("dictionary"))

    @staticmethod
    def _sources() -> dict[int, Any]:
        from supagent.knowledge.curated import sources_of_user

        return {s.id: s for s in sources_of_user()}

    @expose("/api/summary", methods=("GET",))
    @has_access_api
    def summary(self) -> Response:
        from sqlalchemy import func
        from superset import db

        from supagent.models import KObject, Relation, Run

        sources = self._sources()
        out = []
        for sid, s in sources.items():
            counts = dict(db.session.query(KObject.kind, func.count(KObject.id))
                          .filter(KObject.source_id == sid, KObject.gone_at.is_(None)).group_by(KObject.kind).all())
            described = (db.session.query(func.count(KObject.id))
                         .filter(KObject.source_id == sid, KObject.gone_at.is_(None),
                                 KObject.description.isnot(None), KObject.description != "").scalar())
            unverified = (db.session.query(func.count(KObject.id))
                          .filter(KObject.source_id == sid, KObject.description_source == "llm",
                                  KObject.verified.is_(False), KObject.gone_at.is_(None)).scalar())
            out.append({"id": sid, "database_id": s.database_id, "database": s.database_name, "backend": s.backend,
                        "last_learned_at": s.last_learned_at, "counts": counts, "described": described,
                        "unverified": unverified, "stats": s.stats or {}})
        ids = list(sources)
        rels = 0
        if ids:
            obj_ids = db.session.query(KObject.id).filter(KObject.source_id.in_(ids))
            rels = (db.session.query(func.count(Relation.id))
                    .filter(Relation.a_id.in_(obj_ids), Relation.relation != "family_part").scalar())
        runs = db.session.query(Run).order_by(Run.id.desc()).limit(5).all()
        return _json({"sources": out, "relations": rels, "is_admin": _is_admin(),
                      "runs": [{"id": r.id, "reason": r.reason, "status": r.status, "started_at": r.started_at,
                                "finished_at": r.finished_at} for r in runs]})

    @expose("/api/objects", methods=("GET",))
    @has_access_api
    def objects(self) -> Response:
        from superset import db

        from supagent.models import KObject

        sources = self._sources()
        q = db.session.query(KObject).filter(KObject.source_id.in_(list(sources) or [-1]))
        args = request.args
        if args.get("source"):
            q = q.filter(KObject.source_id == int(args["source"]))
        if args.get("kind"):
            q = q.filter(KObject.kind == args["kind"])
        if args.get("parent"):
            q = q.filter(KObject.parent == args["parent"])
        show = args.get("show", "")
        if show == "unverified":
            q = q.filter(KObject.description_source == "llm", KObject.verified.is_(False))
        elif show == "undescribed":
            q = q.filter((KObject.description.is_(None)) | (KObject.description == ""))
        elif show == "gone":
            q = q.filter(KObject.gone_at.isnot(None))
        if show != "gone":
            q = q.filter(KObject.gone_at.is_(None))
        text = (args.get("q") or "").strip()
        if text:
            like = f"%{text}%"
            q = q.filter(KObject.name.ilike(like) | KObject.description.ilike(like) | KObject.parent.ilike(like))
        total = q.count()
        page = max(0, int(args.get("page") or 0))
        size = min(200, max(10, int(args.get("size") or 50)))
        from sqlalchemy import case, func

        group = func.coalesce(func.nullif(KObject.parent, ""), KObject.name)     # an index, then its fields
        head = case((KObject.kind.in_(("index", "metric", "family")), 0), else_=1)
        rows = q.order_by(group, head, KObject.name).offset(page * size).limit(size).all()
        return _json({"total": total, "page": page, "size": size, "objects": [_obj_row(o, sources) for o in rows]})

    @expose("/api/objects/<int:oid>", methods=("GET",))
    @has_access_api
    def obj(self, oid: int) -> Response:
        from superset import db

        from supagent.knowledge.describe import _relation_text
        from supagent.models import Change, KObject, Relation

        sources = self._sources()
        o = db.session.get(KObject, oid)
        if o is None or o.source_id not in sources:
            abort(404)
        row = _obj_row(o, sources)
        row.update(stats=o.stats or {}, synonyms=o.synonyms or [], backend_help=o.backend_help,
                   first_seen=o.first_seen, last_seen=o.last_seen, gone_at=o.gone_at)
        rels = (db.session.query(Relation).filter((Relation.a_id == o.id) | (Relation.b_id == o.id)).all())
        ends = {r.a_id for r in rels} | {r.b_id for r in rels}
        objs = {x.id: x for x in db.session.query(KObject).filter(KObject.id.in_(ends))} if ends else {}
        row["relations"] = []
        for r in rels:
            a, b = objs.get(r.a_id), objs.get(r.b_id)
            if a is None or b is None or a.source_id not in sources or b.source_id not in sources:
                continue
            other = b if a.id == o.id else a
            row["relations"].append({"relation": r.relation, "origin": r.origin, "confidence": r.confidence,
                                     "evidence": r.evidence or {}, "text": _relation_text(r, a, b),
                                     "other": _obj_row(other, sources)})
        kids_kind = {"metric": "label", "index": "field"}.get(o.kind)
        if kids_kind:
            kids = (db.session.query(KObject).filter_by(source_id=o.source_id, kind=kids_kind, parent=o.name)
                    .order_by(KObject.name).limit(500).all())
            row["children"] = [_obj_row(k, sources) for k in kids]
        if o.kind == "label":
            others = (db.session.query(KObject.parent).filter(KObject.source_id == o.source_id, KObject.kind == "label",
                                                              KObject.name == o.name, KObject.id != o.id,
                                                              KObject.gone_at.is_(None)).limit(300).all())
            row["also_on"] = sorted(p for (p,) in others)
        changes = db.session.query(Change).filter_by(object_id=o.id).order_by(Change.id.desc()).limit(30).all()
        row["changes"] = [{"at": c.at, "change": c.change, "detail": c.detail or {}, "run": c.run_id} for c in changes]
        return _json(row)

    @expose("/api/objects/<int:oid>", methods=("POST",))
    @has_access_api
    def edit(self, oid: int) -> Response:
        """Admins: write or approve a description (it becomes curated: the learner never changes it)."""
        from superset import db

        from supagent.models import KObject

        if not _is_admin():
            return _json({"error": "only admins may change the dictionary"}, 403)
        o = db.session.get(KObject, oid)
        if o is None or o.source_id not in self._sources():
            abort(404)
        body = _body()
        if "description" in body:
            text = str(body.get("description") or "").strip()
            o.description = text or None
            o.description_source = "curated" if text else None
            o.verified = bool(text)
        if body.get("approve") and o.description:
            o.verified = True
        if "category" in body:
            o.category = str(body.get("category") or "").strip()[:64] or None
        if "unit" in body:
            o.unit = str(body.get("unit") or "").strip()[:64] or None
        if "synonyms" in body:
            syn = body.get("synonyms")
            o.synonyms = [s.strip() for s in (syn if isinstance(syn, list) else str(syn or "").split(","))
                          if str(s).strip()] or None
        db.session.commit()
        return _json(_obj_row(o, self._sources()))

    @expose("/api/changes", methods=("GET",))
    @has_access_api
    def changes(self) -> Response:
        from supagent.knowledge.describe import changes

        return _json({"changes": changes(int(request.args.get("days") or 7), limit=500)})

    @expose("/api/relations", methods=("GET",))
    @has_access_api
    def relations(self) -> Response:
        from superset import db

        from supagent.knowledge.describe import _relation_text
        from supagent.models import KObject, Relation

        sources = self._sources()
        ids = db.session.query(KObject.id).filter(KObject.source_id.in_(list(sources) or [-1]),
                                                  KObject.gone_at.is_(None))
        rels = (db.session.query(Relation).filter(Relation.a_id.in_(ids), Relation.b_id.in_(ids),
                                                  Relation.relation != "family_part")
                .order_by(Relation.origin, Relation.confidence.desc()).limit(1000).all())
        ends = {r.a_id for r in rels} | {r.b_id for r in rels}
        objs = {x.id: x for x in db.session.query(KObject).filter(KObject.id.in_(ends))} if ends else {}
        out = []
        for r in rels:
            a, b = objs.get(r.a_id), objs.get(r.b_id)
            if a is None or b is None:
                continue
            out.append({"relation": r.relation, "origin": r.origin, "confidence": r.confidence,
                        "evidence": r.evidence or {}, "text": _relation_text(r, a, b),
                        "a": _obj_row(a, sources), "b": _obj_row(b, sources)})
        return _json({"relations": out})


def _obj_row(o: Any, sources: dict[int, Any]) -> dict:
    st = o.stats or {}
    src = sources.get(o.source_id)
    return {"id": o.id, "kind": o.kind, "parent": o.parent or None, "name": o.name, "database": src.database_name
            if src else None, "source_id": o.source_id, "data_type": o.data_type, "metric_type": o.metric_type,
            "unit": o.unit, "description": o.description, "description_source": o.description_source,
            "verified": bool(o.verified), "category": o.category, "gone": o.gone_at is not None,
            "series": st.get("series"), "cardinality": st.get("cardinality"), "docs": st.get("docs")}


class AdminView(BaseView):
    route_base = "/supagent/admin"
    default_view = "index"
    class_permission_name = ADMIN_VIEW
    method_permission_name = {"index": "read", "get_settings": "read", "put_settings": "write", "test_llm": "write",
                              "learn": "write", "runs": "read", "put_catalog": "write", "status": "read",
                              "entries": "read", "create_entry": "write", "update_entry": "write",
                              "delete_entry": "write", "entry_history": "read", "restore_entry": "write",
                              "export_catalog": "read", "team_memory": "read", "set_memory": "write",
                              "docs": "read", "add_doc": "write", "refresh_doc": "write", "delete_doc": "write"}

    # ---- team memory
    @expose("/api/memory", methods=("GET",))
    @has_access_api
    def team_memory(self) -> Response:
        from superset import db
        from superset.extensions import security_manager

        from supagent.models import Memory

        rows = (db.session.query(Memory).filter(Memory.scope == "team").order_by(Memory.status.desc(),
                                                                                 Memory.id.desc()).limit(500).all())
        names: dict[int, str] = {}
        out = []
        for m in rows:
            if m.user_id not in names:
                u = security_manager.get_user_by_id(m.user_id) if m.user_id else None
                names[m.user_id] = u.username if u is not None else "?"
            out.append({"id": m.id, "text": m.text, "kind": m.kind, "category": m.category, "status": m.status,
                        "source": m.source, "by": names[m.user_id], "created_at": m.created_at,
                        "approved_by": m.approved_by})
        return _json({"memory": out})

    @expose("/api/memory/<int:mem_id>", methods=("POST",))
    @has_access_api
    def set_memory(self, mem_id: int) -> Response:
        from superset import db

        from supagent.models import Memory

        m = db.session.get(Memory, mem_id)
        if m is None:
            abort(404)
        body = _body()
        status = body.get("status")
        if status in ("active", "disabled", "proposed"):
            m.status = status
            if status == "active":
                m.approved_by = g.user.username
        if body.get("text"):
            m.text = str(body["text"]).strip()[:1000]
        if "category" in body:
            m.category = str(body.get("category") or "").strip() or None
        db.session.commit()
        _sync_chunks("memory:")
        return _json({"id": m.id, "status": m.status, "text": m.text})

    # ---- documents and sites
    @staticmethod
    def _doc_json(d: Any) -> dict:
        return {"id": d.id, "kind": d.kind, "title": d.title, "url": d.url, "category": d.category,
                "pages": d.pages or [], "chars": len(d.content or ""), "max_pages": d.max_pages,
                "refresh_days": d.refresh_days, "enabled": d.enabled, "status": d.status, "error": d.error,
                "fetched_at": d.fetched_at, "created_by": d.created_by}

    @expose("/api/docs", methods=("GET",))
    @has_access_api
    def docs(self) -> Response:
        from superset import db

        from supagent.models import Doc

        return _json({"docs": [self._doc_json(d) for d in db.session.query(Doc).order_by(Doc.id.desc())]})

    @expose("/api/docs", methods=("POST",))
    @has_access_api
    def add_doc(self) -> Response:
        from superset import db

        from supagent.knowledge.docs import DocError, check_url, html_to_text
        from supagent.models import Doc

        body = _body()
        kind = "url" if body.get("url") else "upload"
        d = Doc(kind=kind, title=(str(body.get("title") or "").strip() or None), category=body.get("category"),
                created_by=g.user.username, max_pages=max(1, min(int(body.get("max_pages") or 1), 200)),
                refresh_days=max(1, int(body.get("refresh_days") or 7)))
        if kind == "url":
            d.url = str(body["url"]).strip()
            try:
                check_url(d.url)
            except DocError as ex:
                return _json({"error": str(ex)}, 400)
        else:
            text = str(body.get("content") or "")
            if not text.strip():
                return _json({"error": "empty document"}, 400)
            if len(text) > 20_000_000:
                return _json({"error": "document too large (20 MB of text at most)"}, 400)
            if (body.get("name") or "").lower().endswith((".html", ".htm")) or text.lstrip()[:15].lower().startswith(
                    ("<!doctype html", "<html")):
                title, text, _links = html_to_text(text)
                d.title = d.title or title or body.get("name")
            d.title = d.title or body.get("name") or "document"
            d.content, d.status = text, "ok"
            d.content_hash = hashlib.sha256(text.encode()).hexdigest()[:40]
            d.fetched_at = dt.datetime.utcnow()
        db.session.add(d)
        db.session.commit()
        if kind == "url":
            from supagent.tasks import dispatch_doc

            dispatch_doc(d.id)
        else:
            _sync_chunks("doc:")
        return _json({"doc": self._doc_json(d)})

    @expose("/api/docs/<int:doc_id>/refresh", methods=("POST",))
    @has_access_api
    def refresh_doc(self, doc_id: int) -> Response:
        from superset import db

        from supagent.models import Doc
        from supagent.tasks import dispatch_doc

        d = db.session.get(Doc, doc_id)
        if d is None or d.kind != "url":
            abort(404)
        dispatch_doc(d.id)
        return _json({"refreshing": doc_id})

    @expose("/api/docs/<int:doc_id>", methods=("DELETE",))
    @has_access_api
    def delete_doc(self, doc_id: int) -> Response:
        from superset import db

        from supagent.models import Doc

        d = db.session.get(Doc, doc_id)
        if d is None:
            abort(404)
        db.session.delete(d)
        db.session.commit()
        _sync_chunks("doc:")
        return _json({"deleted": doc_id})
    template_folder = os.path.join(HERE, "templates")

    @expose("/")
    @has_access
    def index(self) -> Any:
        return self.render_template("supagent/admin.html", nav=_nav("admin"))

    @expose("/api/settings", methods=("GET",))
    @has_access_api
    def get_settings(self) -> Response:
        from supagent import settings

        return _json({"settings": settings.describe()})

    @expose("/api/settings", methods=("POST",))
    @has_access_api
    def put_settings(self) -> Response:
        from supagent import settings

        body = _body().get("settings") or {}
        errors, saved = {}, []
        for key, value in body.items():
            spec = settings.BY_KEY.get(key)
            if spec is None:
                errors[key] = "unknown setting"
                continue
            if spec.secret and value in (None, "") and not _body().get("clear_secrets"):
                continue                               # an empty password box keeps the secret
            try:
                settings.set_value(key, None if value == "" and spec.kind not in ("str",) else value,
                                   by=g.user.username)
                saved.append(key)
            except (ValueError, TypeError) as ex:
                errors[key] = str(ex)
        return _json({"saved": saved, "errors": errors, "settings": settings.describe()},
                     400 if errors and not saved else 200)

    @expose("/api/test-llm", methods=("POST",))
    @has_access_api
    def test_llm(self) -> Response:
        from supagent.llm import LLM

        try:
            return _json({"ok": True, **LLM().check()})
        except Exception as ex:  # pylint: disable=broad-except
            return _json({"ok": False, "error": f"{type(ex).__name__}: {str(ex)[:800]}"})

    @expose("/api/learn", methods=("POST",))
    @has_access_api
    def learn(self) -> Response:
        from supagent.knowledge.learner import running_run
        from supagent.tasks import dispatch_learning

        busy = running_run()
        if busy is not None:
            return _json({"error": f"learning run {busy.id} is still running"}, 409)
        databases = _body().get("databases") or None
        return _json({"started": dispatch_learning("manual", databases)})

    @expose("/api/runs", methods=("GET",))
    @has_access_api
    def runs(self) -> Response:
        from sqlalchemy import func
        from superset import db

        from supagent.models import Change, Run

        rows = db.session.query(Run).order_by(Run.id.desc()).limit(30).all()
        counts = dict(db.session.query(Change.run_id, func.count(Change.id))
                      .filter(Change.run_id.in_([r.id for r in rows] or [-1])).group_by(Change.run_id).all())
        return _json({"runs": [{"id": r.id, "reason": r.reason, "status": r.status, "started_at": r.started_at,
                                "finished_at": r.finished_at, "stats": r.stats or {}, "error": r.error,
                                "changes": counts.get(r.id, 0)} for r in rows]})

    # ---- the catalog, as separate entries
    @staticmethod
    def _entry_json(e: Any, content: bool = True) -> dict:
        from supagent.knowledge.catalog import AGENT

        out = {"id": e.id, "title": e.title, "classification": e.classification, "category": e.category,
               "fmt": e.fmt, "enabled": bool(e.enabled), "version": e.version, "updated_at": e.updated_at,
               "updated_by": e.updated_by, "created_by": e.created_by, "size": len(e.content or ""),
               "origin": e.origin, "evidence": e.evidence or None, "agent": e.updated_by == AGENT}
        if content:
            out["content"] = e.content or ""
        return out

    @expose("/api/entries", methods=("GET",))
    @has_access_api
    def entries(self) -> Response:
        from superset import db

        from supagent.knowledge.catalog import CLASSIFICATIONS, conflicts
        from supagent.models import Entry

        q = db.session.query(Entry).filter(Entry.deleted_at.is_(None))
        rows = q.order_by(Entry.classification, Entry.category, Entry.title).all()
        categories = sorted({e.category for e in rows if e.category})
        return _json({"entries": [self._entry_json(e) for e in rows], "classifications": CLASSIFICATIONS,
                      "categories": categories, **conflicts()})

    @expose("/api/entries", methods=("POST",))
    @has_access_api
    def create_entry(self) -> Response:
        return self._save_entry(None)

    @expose("/api/entries/<int:eid>", methods=("PUT",))
    @has_access_api
    def update_entry(self, eid: int) -> Response:
        return self._save_entry(eid)

    def _save_entry(self, eid: int | None) -> Response:
        from supagent.knowledge.catalog import CatalogError, save_entry
        from supagent.knowledge.curated import apply_catalog

        body = _body()
        try:
            e = save_entry(body, by=g.user.username, entry_id=eid, expected_version=body.get("version"))
        except CatalogError as ex:
            return _json({"error": str(ex)}, 409 if "changed meanwhile" in str(ex) else 400)
        applied = apply_catalog() if e.classification != "note" else {}
        _sync_chunks("entry:")
        return _json({"entry": self._entry_json(e), "applied": applied})

    @expose("/api/entries/<int:eid>", methods=("DELETE",))
    @has_access_api
    def delete_entry(self, eid: int) -> Response:
        from supagent.knowledge.catalog import CatalogError, delete_entry

        try:
            delete_entry(eid, by=g.user.username, expected_version=request.args.get("version", type=int))
        except CatalogError as ex:
            return _json({"error": str(ex)}, 409 if "changed meanwhile" in str(ex) else 404)
        _sync_chunks("entry:")
        return _json({"deleted": eid})

    @expose("/api/entries/<int:eid>/history", methods=("GET",))
    @has_access_api
    def entry_history(self, eid: int) -> Response:
        from superset import db

        from supagent.models import EntryVersion

        rows = (db.session.query(EntryVersion).filter_by(entry_id=eid).order_by(EntryVersion.version.desc())
                .limit(100).all())
        return _json({"versions": [{"version": v.version, "title": v.title, "classification": v.classification,
                                    "category": v.category, "enabled": v.enabled, "deleted": v.deleted,
                                    "changed_at": v.changed_at, "changed_by": v.changed_by,
                                    "content": v.content or ""} for v in rows]})

    @expose("/api/entries/<int:eid>/restore", methods=("POST",))
    @has_access_api
    def restore_entry(self, eid: int) -> Response:
        from supagent.knowledge.catalog import CatalogError, restore_entry
        from supagent.knowledge.curated import apply_catalog

        try:
            e = restore_entry(eid, int(_body().get("version") or 0), by=g.user.username)
        except CatalogError as ex:
            return _json({"error": str(ex)}, 404)
        return _json({"entry": self._entry_json(e), "applied": apply_catalog()})

    @expose("/api/catalog/export", methods=("GET",))
    @has_access_api
    def export_catalog(self) -> Response:
        from supagent.knowledge.catalog import export_catalog

        return Response(export_catalog(), mimetype="application/x-yaml",
                        headers={"Content-Disposition": 'attachment; filename="catalog.yaml"'})

    @expose("/api/catalog", methods=("POST",))
    @has_access_api
    def put_catalog(self) -> Response:
        """A whole catalog (YAML): split into entries (merge by title, or replace)."""
        from supagent.knowledge.catalog import CatalogError, import_catalog
        from supagent.knowledge.curated import apply_catalog

        body = _body()
        try:
            counts = import_catalog(str(body.get("content") or ""), by=g.user.username,
                                    mode="replace" if body.get("mode") == "replace" else "merge")
        except CatalogError as ex:
            return _json({"error": str(ex)}, 400)
        return _json({"imported": counts, "applied": apply_catalog()})

    @expose("/api/status", methods=("GET",))
    @has_access_api
    def status(self) -> Response:
        from superset import db
        from superset.extensions import celery_app

        from supagent import __version__
        from supagent.models import Meta
        from supagent.tasks import BEAT_KEY, workers_alive

        try:
            import fastmcp  # noqa: F401
            import superset.mcp_service  # noqa: F401

            mcp = True
        except Exception:  # pylint: disable=broad-except
            mcp = False
        from supagent.models import Run

        row = db.session.get(Meta, "schema_version")
        last = (db.session.query(Run).filter(Run.reason == "schedule").order_by(Run.id.desc()).first())
        return _json({"version": __version__, "schema_version": row.value if row else None,
                      "celery_workers": workers_alive(), "superset_mcp": mcp,
                      "daily_tick_scheduled": BEAT_KEY in (celery_app.conf.beat_schedule or {}),
                      "last_scheduled_run": last.started_at if last else None,
                      "last_scheduled_status": last.status if last else None})
