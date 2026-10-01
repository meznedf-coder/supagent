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
import logging
import os
import re
from typing import Any

from flask import Response, g, request
from flask_appbuilder import BaseView, expose
from flask_appbuilder.security.decorators import has_access, has_access_api

log = logging.getLogger(__name__)

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
    """The searchable pieces follow a change: in the background of this process (knowledge.apply), the save
    answering at once; without the background (tests, knowledge.apply_background off): now."""
    try:
        _apply("chunks", prefix=prefix)
    except Exception:  # pylint: disable=broad-except
        from superset import db

        db.session.rollback()
        log.warning("supagent: the search pieces of %s: not written", prefix, exc_info=True)


def _apply(kind: str, **kw: Any) -> dict:
    from supagent import settings
    from supagent.knowledge import apply

    if settings.get("knowledge.apply_background"):
        apply.later(kind, **kw)
        return {"applying": True}
    jobs = {"catalog": kw.get("before") if kind == "catalog" else None,
            "prefixes": {kw["prefix"]} if kind == "chunks" else set(),
            "objects": set(kw.get("ids") or []) if kind == "objects" else set()}
    return apply.run(jobs).get("catalog") or {}


def _embed_few(out: dict[str, int]) -> None:
    from supagent.knowledge.index import embed_few

    embed_few(out)


def _catalog_changed(before: dict) -> dict:
    """After a change of the catalog: the dictionary and the searchable pieces in step (knowledge.apply)."""
    try:
        return _apply("catalog", before=before)
    except Exception:  # pylint: disable=broad-except
        from superset import db

        db.session.rollback()
        log.warning("supagent: catalog applied to the dictionary: failed", exc_info=True)
        return {}


def _ref_titles(refs: Any) -> dict[str, str]:
    """What knowledge refs are, in words (entry:3 -> the entry's title), one query per kind of ref."""
    from superset import db

    from supagent.models import ContextPage, Doc, Entry, KObject, Memory, Recipe

    models = {"entry": Entry, "memory": Memory, "doc": Doc, "context": ContextPage, "recipe": Recipe,
              "object": KObject}
    out: dict[str, str] = {}
    wanted: dict[str, dict[int, list[str]]] = {}
    for ref in set(refs or ()):
        kind, _, rest = (ref or "").partition(":")
        if kind == "data":
            out[ref] = rest.split(":", 1)[1] if ":" in rest else rest
        elif kind == "family":
            out[ref] = (rest.split(":", 1)[1] if ":" in rest else rest) + "_* metrics"
        else:
            ident = rest.split("#", 1)[0]
            out[ref] = ref
            if kind in models and ident.isdigit():
                wanted.setdefault(kind, {}).setdefault(int(ident), []).append(ref)
    for kind, ids in wanted.items():
        model = models[kind]
        try:
            for o in db.session.query(model).filter(model.id.in_(list(ids))):
                title = str(getattr(o, "title", None) or getattr(o, "name", None) or getattr(o, "question", None)
                            or getattr(o, "text", "") or "")[:160]
                if kind == "object" and getattr(o, "parent", None):
                    title = f"{o.parent} › {title}"
                for ref in ids.get(o.id, []):
                    out[ref] = title or ref
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
    return out


def _like(word: str) -> str:
    """A word inside a LIKE pattern, its wildcards escaped (escape character: backslash)."""
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_admin() -> bool:
    from superset.extensions import security_manager

    try:
        return bool(security_manager.is_admin()) or bool(security_manager.can_access("can_write", ADMIN_VIEW))
    except Exception:  # pylint: disable=broad-except
        return False


def _nav(active: str) -> dict:
    from superset.extensions import security_manager

    from supagent.theme import superset_theme

    return {"active": active, "user": g.user.username if getattr(g, "user", None) else "",
            "can_chat": security_manager.can_access("can_read", ChatView.class_permission_name),
            "can_dictionary": security_manager.can_access("can_read", KnowledgeView.class_permission_name),
            "is_admin": _is_admin(), "theme": superset_theme()}


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
                              "memory": "read", "add_memory": "write", "delete_memory": "write",
                              "search_chats": "read"}
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
                "steps": m.steps or [], "files": files, "feedback": m.feedback, "feedback_reason": m.feedback_reason,
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
        """Stop an answer, at once: the conversation takes the next question right away. A step
        already running (an LLM call, a query) ends on its own; its result is thrown away and
        the agent does nothing more."""
        from superset import db

        from supagent.models import Message

        m = db.session.get(Message, mid)
        if m is None:
            abort(404)
        self._conversation(m.conversation_id)
        now = dt.datetime.utcnow()
        # only an answer still being answered: one that has just finished keeps its answer
        (db.session.query(Message).filter(Message.id == mid, Message.status.in_(("pending", "running", "cancelling")))
         .update({"status": "cancelled", "content": "(stopped)", "finished_at": now, "updated_at": now},
                 synchronize_session=False))
        db.session.commit()
        status = db.session.query(Message.status).filter(Message.id == mid).scalar()
        return _json({"status": status})

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

    @expose("/api/chats/search", methods=("GET",))
    @has_access_api
    def search_chats(self) -> Response:
        """The user's own chats like ?q= (the knowledge store: by words and meaning; else the words in the
        messages), newest conversation first among the closest: [{conversation_id, message_id, title, question,
        snippet, at}]."""
        from sqlalchemy import func as sa_func
        from superset import db

        from supagent.models import Conversation, Message

        q = " ".join((request.args.get("q") or "").split())[:200]
        if len(q) < 2:
            return _json({"results": [], "by": None})
        me = g.user.id
        hits: list[dict[str, Any]] = []
        by = "words"
        try:
            from supagent.knowledge import pgstore

            if pgstore.active():
                from supagent import settings

                for h in pgstore.chats(q, me, k=30, min_cos=float(settings.get("search.chat_box_similarity") or 0)):
                    hits.append({"conversation_id": h.get("conversation_id"), "message_id": h.get("message_id"),
                                 "question": h.get("question") or "", "snippet": (h.get("answer") or "")[:220],
                                 "at": h.get("at")})
                by = "store"
        except Exception:  # pylint: disable=broad-except   (the words below)
            log.warning("supagent: chat search in the store", exc_info=True)
        if not hits:
            by = "words"
            words = [w for w in re.findall(r"\w{2,}", q.lower())][:6]
            rows = db.session.query(Message).join(Conversation, Conversation.id == Message.conversation_id).filter(
                Conversation.user_id == me)
            for w in words:
                rows = rows.filter(sa_func.lower(Message.content).like(f"%{_like(w)}%", escape="\\"))
            for m in rows.order_by(Message.id.desc()).limit(60):
                text_ = m.content or ""
                at = text_.lower().find(words[0]) if words else 0
                start = max(0, at - 80)
                hits.append({"conversation_id": m.conversation_id, "message_id": m.id,
                             "question": text_[:200] if m.role == "user" else "",
                             "snippet": ("\u2026" if start else "") + text_[start:start + 220].replace("\n", " "),
                             "at": m.created_at.isoformat(timespec="minutes") if m.created_at else None})
        titles = {c.id: c.title for c in db.session.query(Conversation).filter(
            Conversation.user_id == me, Conversation.id.in_({h["conversation_id"] for h in hits if h["conversation_id"]}
                                                          or {-1}))}
        seen: set[int] = set()
        out = []
        for h in hits:                           # one per conversation (its closest), only conversations still there
            cid = h["conversation_id"]
            if cid not in titles or cid in seen:
                continue
            seen.add(cid)
            out.append({**h, "title": titles[cid] or f"Conversation {cid}"})
        return _json({"results": out[:20], "by": by})

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
        db.session.delete(m)                     # removed (Disable keeps a team one without using it)
        db.session.commit()
        _sync_chunks("memory:")
        return _json({"deleted": mem_id})

    @expose("/api/messages/<int:mid>/feedback", methods=("POST",))
    @has_access_api
    def feedback(self, mid: int) -> Response:
        """Helpful (+1) makes a learned answer of it (in the background: the LLM writes its generic
        question), for an admin to confirm or reject; Not helpful (-1) or 0 takes it back. With Not
        helpful, `reason` (what was wrong) is kept for the admins (superset supagent gaps) and what it
        says about the data is proposed to the memory (a team point waits for an admin)."""
        from superset import db

        from supagent.models import Example, Message

        m = db.session.get(Message, mid)
        if m is None or m.role != "assistant":
            abort(404)
        self._conversation(m.conversation_id)
        body = _body()
        value = int(body.get("value") or 0)
        reason = " ".join(str(body.get("reason") or "").split())[:1000]
        if reason and value == -1 and m.feedback == -1:          # the reason of a Not helpful given before
            m.feedback_reason = reason
            db.session.commit()
            from supagent.tasks import dispatch_memory

            dispatch_memory(m.id)                    # what it says about the data: to the memory
            return _json({"feedback": m.feedback, "feedback_reason": m.feedback_reason, "example_kept": False,
                          "recipes": 0})
        m.feedback = value if value in (-1, 1) else None
        m.feedback_reason = (reason or None) if value == -1 else None
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
        from supagent.governed.gate import confirm

        confirm(m.id, {1: "helpful", -1: "not_helpful"}.get(value))   # the decider's route of it teaches (Helpful)
        from supagent.knowledge.experience import feedback as recipe_feedback
        from supagent.tasks import dispatch_catalog, dispatch_helpful, dispatch_memory

        recipes = 0
        if value == 1:
            dispatch_helpful(m.id)                   # a learned answer, for an admin to confirm or reject
            dispatch_memory(m.id)                    # what is worth remembering from this exchange
        else:
            recipes = recipe_feedback(m.id, value)   # its Helpful taken back
            if recipes:
                _sync_chunks("recipe:")
                dispatch_catalog()                   # a formula no longer certain
            if value == -1:
                from supagent.knowledge.experience import forget_associations

                forget_associations(m.id)            # nor where its data was
                if m.feedback_reason:
                    dispatch_memory(m.id)
        return _json({"feedback": m.feedback, "feedback_reason": m.feedback_reason, "example_kept": kept,
                      "recipes": recipes})

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
        from supagent.textsafe import content_disposition

        headers = {"Content-Disposition": content_disposition("inline" if inline else "attachment", f.name or "file"),
                   "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
        return Response(f.data, mimetype=f.mime or "application/octet-stream", headers=headers)


def _page_args(default_size: int = 50) -> tuple[int, int]:
    """The page (from 0) and its size asked by a list of the pages."""
    try:
        page = max(0, int(request.args.get("page") or 0))
        size = min(200, max(5, int(request.args.get("size") or default_size)))
    except ValueError:
        page, size = 0, default_size
    return page, size


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
        else:
            q = q.filter(Recipe.status != "auto")    # saved by themselves by 0.2.1 and before
        from supagent.knowledge.ranking import adjust, demoted, reasons, usefulness

        rows = [r for r in q.limit(500) if admin or (r.database_id and r.database_id in visible)]
        use = usefulness([f"recipe:{r.id}" for r in rows])      # what the discussions said of them
        out = []
        for r in rows:
            u = use.get(f"recipe:{r.id}")
            out.append({"id": r.id, "question": r.question, "tool": r.tool, "database_id": r.database_id,
                        "target": r.target, "query": r.query, "seconds": r.seconds, "rows": r.rows,
                        "steps": r.steps, "status": r.status, "uses": r.uses, "created_at": r.created_at,
                        "helpful": len(r.confirmations or []), "last_used_at": r.last_used_at,
                        "use": u, "why": reasons(u), "rank": round(adjust(u), 3), "demoted": demoted(u)})
        if request.args.get("sort", "useful") == "useful":  # the most useful first, then the least tried
            out.sort(key=lambda x: (x["status"] == "rejected", x["demoted"], -x["rank"], -(x["use"] or {}).get("given", 0)))
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
            _sync_chunks("recipe:")
            return _json({"deleted": rid})
        status = str(_body().get("status") or "")
        if status not in ("helpful", "confirmed", "rejected"):
            return _json({"error": "status: helpful, confirmed or rejected"}, 400)
        r.status = status
        db.session.commit()
        if r.message_id:                             # the decider's route of the answer: confirmed or not
            from supagent.governed.gate import confirm

            confirm(r.message_id, {"confirmed": "confirmed", "rejected": "not_helpful"}.get(status, "helpful"))
        _sync_chunks("recipe:")
        from supagent.tasks import dispatch_catalog

        dispatch_catalog()                           # the formulas it supports (agent catalog)
        return _json({"id": rid, "status": status})

    @expose("/api/timings", methods=("GET",))
    @has_access_api
    def timings(self) -> Response:
        from sqlalchemy import false, or_
        from superset import db

        from supagent.models import QueryStat

        visible, admin = _visible_databases(), _is_admin()
        page, size = _page_args()
        q = db.session.query(QueryStat).filter(or_(QueryStat.database_id.in_(list(visible) or [-1]),
                                                   QueryStat.database_id.is_(None) if admin else false()))
        rows = q.order_by(QueryStat.max_seconds.desc(), QueryStat.id).offset(page * size).limit(size).all()
        return _json({"timings": [{"target": r.target, "database_id": r.database_id, "pattern": r.pattern,
                                   "calls": r.calls, "errors": r.errors, "avg_seconds": round((r.total_seconds or 0) /
                                                                                          max(r.calls or 1, 1), 2),
                                   "max_seconds": r.max_seconds, "last_error": r.last_error, "last_at": r.last_at,
                                   "last_query": r.last_query if admin else None} for r in rows],
                      "total": q.count(), "page": page, "size": size, "is_admin": admin})


class KnowledgeView(_RecipesMixin, BaseView):
    route_base = "/supagent/dictionary"
    default_view = "index"
    class_permission_name = "AIAgentDictionary"
    method_permission_name = {"index": "read", "summary": "read", "objects": "read", "obj": "read",
                              "changes": "read", "relations": "read", "edit": "write", "recipes": "read",
                              "set_recipe": "write", "timings": "read", "search": "read", "set_relation": "write",
                              "knowledge": "read", "agent_knowledge": "read", "context": "read",
                              "context_page": "read", "context_edit": "write", "context_build": "write"}

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
                    .filter(Relation.a_id.in_(obj_ids), Relation.relation != "family_part",
                            Relation.rejected_at.is_(None)).scalar())
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
        rels = (db.session.query(Relation).filter((Relation.a_id == o.id) | (Relation.b_id == o.id))
                .filter(Relation.rejected_at.is_(None)).all())
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
        from supagent.knowledge.freshness import touch

        touch()                                  # every server: the next answer uses it
        db.session.commit()
        try:                                     # and the agent's search finds it in a moment
            _apply("objects", ids=[o.id])
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            log.warning("supagent: search pieces of %s: not written", o.name, exc_info=True)
        return _json(_obj_row(o, self._sources()))

    @expose("/api/changes", methods=("GET",))
    @has_access_api
    def changes(self) -> Response:
        from supagent.knowledge.describe import changes

        page, size = _page_args()
        total: dict[str, int] = {}
        rows = changes(int(request.args.get("days") or 7), limit=size, offset=page * size, total=total)
        return _json({"changes": rows, "total": total.get("total", 0), "page": page, "size": size})

    @expose("/api/knowledge", methods=("GET",))
    @has_access_api
    def knowledge(self) -> Response:
        """What the team gave the agent, read only (the settings page changes it): the catalog
        entries, the documents and sites, the team memory (approved). A formula the agent learned
        on a database is shown only to the users who may query that database."""
        from superset import db

        from supagent.knowledge.catalog import AGENT
        from supagent.models import Doc, Entry, Memory

        dbs = _visible_databases()
        entries = []
        for e in (db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True))
                  .order_by(Entry.classification, Entry.category, Entry.title)):
            database_id = (e.evidence or {}).get("database_id")
            if database_id is not None and int(database_id) not in dbs:
                continue
            entries.append({"id": e.id, "title": e.title, "classification": e.classification, "category": e.category,
                            "fmt": e.fmt, "content": e.content or "", "updated_at": e.updated_at,
                            "by": "the agent" if e.updated_by == AGENT else (e.updated_by or e.created_by or "")})
        docs = [{"id": d.id, "title": d.title or d.url or f"document {d.id}", "kind": d.kind, "url": d.url,
                 "category": d.category, "status": d.status, "pages": len(d.pages or []) or None,
                 "chars": len(d.content or ""), "excerpt": (d.content or "")[:1500], "fetched_at": d.fetched_at}
                for d in db.session.query(Doc).filter(Doc.enabled.is_(True)).order_by(Doc.title)]
        team = [{"id": m.id, "kind": m.kind, "text": m.text, "category": m.category, "created_at": m.created_at}
                for m in (db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "active")
                          .order_by(Memory.created_at.desc()))]
        return _json({"entries": entries, "docs": docs, "team_memory": team})

    @expose("/api/agent_knowledge", methods=("GET",))
    @has_access_api
    def agent_knowledge(self) -> Response:
        """What the agent learned by itself, on the databases this user may query: its catalog
        entries (with their evidence), where the data is from the answers (a word of the questions
        and the metric or index that answered them), the AI descriptions still to verify, the
        relations it measured."""
        from sqlalchemy import func
        from superset import db

        from supagent.knowledge.catalog import AGENT
        from supagent.models import Association, Entry, KObject, Relation, Source

        dbs = _visible_databases()
        names = {s.id: s for s in db.session.query(Source).filter(Source.database_id.in_(dbs or [-1]))}
        entries = []
        for e in db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.updated_by == AGENT) \
                .order_by(Entry.updated_at.desc()):
            database_id = (e.evidence or {}).get("database_id")
            if database_id is not None and int(database_id) not in dbs:
                continue
            entries.append({"id": e.id, "title": e.title, "classification": e.classification, "category": e.category,
                            "content": e.content or "", "origin": e.origin, "evidence": e.evidence or {},
                            "enabled": bool(e.enabled), "updated_at": e.updated_at})
        from supagent.knowledge.ranking import adjust, reasons, usefulness

        use = usefulness([f"entry:{e['id']}" for e in entries])
        for e in entries:
            u = use.get(f"entry:{e['id']}")
            e.update(use=u, why=reasons(u), rank=round(adjust(u), 3))
        entries.sort(key=lambda e: (-e["rank"], -(e["use"] or {}).get("given", 0)))
        by_db = {s.database_id: s.database_name for s in names.values()}
        words = [{"word": a.word, "database": by_db.get(a.database_id, a.database_id), "kind": a.kind,
                  "parent": a.parent, "name": a.name, "uses": a.uses, "updated_at": a.updated_at}
                 for a in (db.session.query(Association).filter(Association.database_id.in_(dbs or [-1]))
                           .order_by(Association.uses.desc(), Association.updated_at.desc()).limit(300))]
        unverified = (db.session.query(KObject.source_id, func.count(KObject.id))
                      .filter(KObject.source_id.in_(list(names) or [-1]), KObject.description_source == "llm",
                              KObject.verified.is_(False), KObject.gone_at.is_(None))
                      .group_by(KObject.source_id).all())
        measured = (db.session.query(func.count(Relation.id)).join(KObject, KObject.id == Relation.a_id)
                    .filter(Relation.origin == "learned", Relation.rejected_at.is_(None),
                            KObject.source_id.in_(list(names) or [-1])).scalar())
        return _json({"entries": entries, "associations": words,
                      "to_verify": [{"source_id": sid, "database": names[sid].database_name, "count": n}
                                    for sid, n in unverified if sid in names],
                      "relations_measured": int(measured or 0)})

    @expose("/api/context", methods=("GET",))
    @has_access_api
    def context(self) -> Response:
        """The Context pages this user may read (each database of a page is one they may query), and
        the last build."""
        from superset import db

        from supagent import settings
        from supagent.knowledge.context import visible_pages
        from supagent.models import Run

        pages = [{"id": p.id, "section": p.section, "slug": p.slug, "title": p.title, "kind": p.kind,
                  "author": p.author or "agent", "ai": p.kind == "summary" and (p.author or "agent") == "agent",
                  "version": p.version, "updated_at": p.updated_at} for p in visible_pages()]
        last = db.session.query(Run).filter(Run.kind == "context").order_by(Run.id.desc()).first()
        return _json({"pages": pages, "enabled": bool(settings.get("context.enabled")),
                      "hour": settings.get("context.hour"),
                      "last_build": {"id": last.id, "status": last.status, "started_at": last.started_at,
                                     "finished_at": last.finished_at} if last else None})

    @expose("/api/context/<int:pid>", methods=("GET",))
    @has_access_api
    def context_page(self, pid: int) -> Response:
        from supagent.knowledge.context import visible_pages

        p = next((x for x in visible_pages() if x.id == pid), None)
        if p is None:
            abort(404)
        return _json({"id": p.id, "section": p.section, "title": p.title, "kind": p.kind, "author": p.author or "agent",
                      "ai": p.kind == "summary" and (p.author or "agent") == "agent", "content": p.content or "",
                      "html": render_markdown(p.content or ""), "sources": p.sources or [], "version": p.version,
                      "updated_at": p.updated_at, "llm_calls": p.llm_calls, "tokens": p.tokens})

    @expose("/api/context/<int:pid>", methods=("POST",))
    @has_access_api
    def context_edit(self, pid: int) -> Response:
        """Admins: correct a page (it is then theirs: the agent never writes over it), or give it
        back to the agent ({"reset": true}: written again at the next build)."""
        import datetime as dt

        from superset import db

        from supagent.knowledge.context import visible_pages
        from supagent.models import ContextPage

        if next((x for x in visible_pages() if x.id == pid), None) is None:
            abort(404)
        p = db.session.get(ContextPage, pid)
        body = _body()
        if body.get("reset"):
            p.author, p.input_hash = "agent", None
        else:
            text = str(body.get("content") or "").strip()
            if not text:
                return _json({"error": "empty page"}, 400)
            p.content, p.author = text, g.user.username
            p.version, p.updated_at = (p.version or 0) + 1, dt.datetime.utcnow()
        db.session.commit()
        _sync_chunks("context:")
        return _json({"id": p.id, "author": p.author, "version": p.version})

    @expose("/api/context/build", methods=("POST",))
    @has_access_api
    def context_build(self) -> Response:
        """Admins: build the Context now (a run of kind "context" in the runs list)."""
        from supagent.knowledge.learner import running_run
        from supagent.tasks import dispatch_context

        busy = running_run()
        if busy is not None:
            name = "a context build" if busy.kind == "context" else "a learning run"
            return _json({"error": f"{name} is running (run {busy.id}): the Context is built after it"}, 409)
        return _json({"started": dispatch_context("manual")})

    @expose("/api/relations", methods=("GET",))
    @has_access_api
    def relations(self) -> Response:
        from superset import db

        from supagent.knowledge.describe import _relation_text
        from supagent.models import KObject, Relation

        sources, admin = self._sources(), _is_admin()
        ids = db.session.query(KObject.id).filter(KObject.source_id.in_(list(sources) or [-1]),
                                                  KObject.gone_at.is_(None))
        q = db.session.query(Relation).filter(Relation.a_id.in_(ids), Relation.b_id.in_(ids),
                                              Relation.relation != "family_part")
        if not admin:
            q = q.filter(Relation.rejected_at.is_(None))
        page, size = _page_args()
        total = q.count()
        rels = q.order_by(Relation.origin, Relation.confidence.desc(), Relation.id).offset(page * size).limit(size).all()
        ends = {r.a_id for r in rels} | {r.b_id for r in rels}
        objs = {x.id: x for x in db.session.query(KObject).filter(KObject.id.in_(ends))} if ends else {}
        out = []
        for r in rels:
            a, b = objs.get(r.a_id), objs.get(r.b_id)
            if a is None or b is None:
                continue
            out.append({"id": r.id, "relation": r.relation, "origin": r.origin, "confidence": r.confidence,
                        "evidence": r.evidence or {}, "text": _relation_text(r, a, b),
                        "rejected": r.rejected_at is not None, "rejected_by": r.rejected_by,
                        "a": _obj_row(a, sources), "b": _obj_row(b, sources)})
        return _json({"relations": out, "total": total, "page": page, "size": size, "is_admin": admin})

    @expose("/api/relations/<int:rid>", methods=("POST",))
    @has_access_api
    def set_relation(self, rid: int) -> Response:
        """An admin marks a measured relation wrong (never measured again, never shown to the
        agent) or restores it. A relation from the catalog changes with its catalog entry."""
        import datetime as _dt

        from superset import db

        from supagent.models import Relation

        if not _is_admin():
            return _json({"error": "only admins may change the relations"}, 403)
        r = db.session.get(Relation, rid)
        if r is None:
            abort(404)
        if r.origin == "curated":
            return _json({"error": "this relation comes from the catalog: change or delete its catalog entry"}, 400)
        wrong = bool(_body().get("rejected"))
        r.rejected_at = _dt.datetime.utcnow() if wrong else None
        r.rejected_by = (g.user.username if wrong else None)
        db.session.commit()
        return _json({"id": rid, "rejected": wrong})


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
                              "learn": "write", "stop_learning": "write", "runs": "read", "put_catalog": "write", "status": "read",
                              "entries": "read", "create_entry": "write", "update_entry": "write",
                              "delete_entry": "write", "entry_history": "read", "restore_entry": "write",
                              "export_catalog": "read", "team_memory": "read", "set_memory": "write",
                              "docs": "read", "add_doc": "write", "refresh_doc": "write", "delete_doc": "write",
                              "usage": "read", "usage_data": "read", "review": "read", "facets": "read",
                              "set_facet": "write", "set_tag": "write", "set_link": "write", "set_route": "write",
                              "apply_status": "read"}

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

    @expose("/usage")
    @has_access
    def usage(self) -> Any:
        """The LLM usage page (admins): tokens, calls, context sizes, per person, per task, over time."""
        return self.render_template("supagent/usage.html", nav=_nav("usage"))

    @expose("/api/usage", methods=("GET",))
    @has_access_api
    def usage_data(self) -> Response:
        """?start=&end= (ISO, UTC; default the last 24 hours) &grain=hour|day &tz= (the browser's offset in
        minutes, for the buckets)."""
        from supagent.knowledge.usage import report

        def when(name: str, default: dt.datetime) -> dt.datetime:
            text = str(request.args.get(name) or "").strip().replace("Z", "")
            try:
                return dt.datetime.fromisoformat(text) if text else default
            except ValueError:
                return default

        end = when("end", dt.datetime.utcnow())
        start = when("start", end - dt.timedelta(days=1))
        if start >= end:
            start = end - dt.timedelta(days=1)
        grain = request.args.get("grain") or ("day" if end - start > dt.timedelta(days=3) else "hour")
        tz = request.args.get("tz", type=int) or 0
        return _json(report(start, end, grain, tz_offset_minutes=max(-900, min(900, tz))))

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
            from supagent.llm import llm_task

            with llm_task("test", user_id=g.user.id):
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
            what = "is stopping: try again in a moment" if busy.status == "stopping" else "is still running"
            name = "context build" if busy.kind == "context" else "learning run"
            return _json({"error": f"{name} {busy.id} {what}", "running": busy.id}, 409)
        databases = _body().get("databases") or None
        return _json({"started": dispatch_learning("manual", databases)})

    @expose("/api/learn/stop", methods=("POST",))
    @has_access_api
    def stop_learning(self) -> Response:
        """Stop the running learning run, at once: it keeps what it learned, and a new run can
        start right away (the request or LLM call in progress ends in the background)."""
        from supagent.knowledge.stopping import request_stop

        run_id = request_stop()
        if run_id is None:
            return _json({"error": "no learning run is running"}, 409)
        return _json({"stopped": run_id})

    @expose("/api/runs", methods=("GET",))
    @has_access_api
    def runs(self) -> Response:
        from sqlalchemy import func
        from superset import db

        from supagent.models import Change, Run

        from supagent.models import KObject

        page, size = _page_args(15)
        total = db.session.query(Run).count()
        rows = db.session.query(Run).order_by(Run.id.desc()).offset(page * size).limit(size).all()
        counts = dict(db.session.query(Change.run_id, func.count(Change.id))
                      .filter(Change.run_id.in_([r.id for r in rows] or [-1])).group_by(Change.run_id).all())
        progress = {}
        for r in rows:                                   # a run in progress: what it found so far
            if r.status in ("running", "stopping") and r.started_at is not None:
                progress[r.id] = {"new_objects": db.session.query(KObject)
                                  .filter(KObject.first_seen >= r.started_at).count()}
        return _json({"runs": [{"id": r.id, "kind": r.kind, "reason": r.reason, "status": r.status,
                                "started_at": r.started_at, "finished_at": r.finished_at, "stats": r.stats or {},
                                "error": r.error, "changes": counts.get(r.id, 0), "progress": progress.get(r.id)}
                               for r in rows], "total": total, "page": page, "size": size,
                      "running": db.session.query(Run.id).filter(Run.status.in_(("running", "stopping")))
                      .order_by(Run.id.desc()).limit(1).scalar()})

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
        from supagent.knowledge.curated import catalog_texts

        body = _body()
        before = catalog_texts()
        try:
            e = save_entry(body, by=g.user.username, entry_id=eid, expected_version=body.get("version"))
        except CatalogError as ex:
            return _json({"error": str(ex)}, 409 if "changed meanwhile" in str(ex) else 400)
        return _json({"entry": self._entry_json(e), "applied": _catalog_changed(before)})

    @expose("/api/entries/<int:eid>", methods=("DELETE",))
    @has_access_api
    def delete_entry(self, eid: int) -> Response:
        from supagent.knowledge.catalog import CatalogError, delete_entry
        from supagent.knowledge.curated import catalog_texts

        before = catalog_texts()
        try:
            delete_entry(eid, by=g.user.username, expected_version=request.args.get("version", type=int))
        except CatalogError as ex:
            return _json({"error": str(ex)}, 409 if "changed meanwhile" in str(ex) else 404)
        _catalog_changed(before)
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
        from supagent.knowledge.curated import catalog_texts

        before = catalog_texts()
        try:
            e = restore_entry(eid, int(_body().get("version") or 0), by=g.user.username)
        except CatalogError as ex:
            return _json({"error": str(ex)}, 404)
        return _json({"entry": self._entry_json(e), "applied": _catalog_changed(before)})

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
        from supagent.knowledge.curated import catalog_texts

        body = _body()
        before = catalog_texts()
        try:
            counts = import_catalog(str(body.get("content") or ""), by=g.user.username,
                                    mode="replace" if body.get("mode") == "replace" else "merge")
        except CatalogError as ex:
            return _json({"error": str(ex)}, 400)
        return _json({"imported": counts, "applied": _catalog_changed(before)})

    # ---- the review (the Data dictionary's first tab): what waits for an admin, in one place. Every action on an
    # item takes it out: approved, corrected, rejected or removed.
    @expose("/api/review", methods=("GET",))
    @has_access_api
    def review(self) -> Response:
        from sqlalchemy import func, or_

        from superset import db

        from supagent.governed.gate import CONFIRMED
        from supagent.models import Facet, KObject, Link, Memory, Recipe, Route, Tag

        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
        mem_q = db.session.query(Memory).filter(Memory.scope == "team", Memory.status == "proposed")
        memories = [{"id": m.id, "text": m.text, "kind": m.kind, "category": m.category, "source": m.source,
                     "created_at": m.created_at} for m in mem_q.order_by(Memory.id.desc()).limit(limit)]
        rec_q = db.session.query(Recipe).filter(Recipe.status == "helpful")
        recipes = [{"id": r.id, "question": r.question, "tool": r.tool, "target": r.target,
                    "query": (r.query or "")[:1500], "uses": r.uses, "created_at": r.created_at}
                   for r in rec_q.order_by(Recipe.id.desc()).limit(limit)]
        desc_q = db.session.query(KObject).filter(KObject.gone_at.is_(None), KObject.description.isnot(None),
                                                  KObject.description_source == "llm", KObject.verified.isnot(True))
        from supagent.knowledge.curated import sources_of_user

        dbs = {x.id: x.database_name for x in sources_of_user()}
        described = [{"id": o.id, "name": o.name, "parent": o.parent, "kind": o.kind, "description": o.description,
                      "source_id": o.source_id, "database": dbs.get(o.source_id)}
                     for o in desc_q.order_by(KObject.kind, KObject.name).limit(limit)]
        val_q = db.session.query(Facet).filter(Facet.status == "proposed")
        vals = val_q.order_by(Facet.facet, Facet.value).limit(limit).all()
        n_items = dict(db.session.query(Tag.facet_id, func.count(Tag.id)).filter(
            Tag.facet_id.in_([f.id for f in vals] or [-1])).group_by(Tag.facet_id).all())
        values = [{"id": f.id, "facet": f.facet, "value": f.value, "description": f.description,
                   "items": n_items.get(f.id, 0)} for f in vals]
        tag_q = (db.session.query(Tag, Facet).join(Facet, Facet.id == Tag.facet_id)
                 .filter(Tag.status == "proposed", Facet.status == "approved"))
        tag_rows = tag_q.order_by(Tag.confidence.desc(), Tag.id.desc()).limit(limit).all()
        link_q = db.session.query(Link).filter(Link.status == "proposed")
        link_rows = link_q.order_by(Link.confidence.desc(), Link.id.desc()).limit(limit).all()
        route_q = db.session.query(Route).filter(Route.moa.isnot(None), Route.moa != "other",
                                                 Route.moa_followed.is_(True), Route.signal.in_(CONFIRMED),
                                                 or_(Route.moa_by.is_(None), Route.moa_by != "admin"))
        route_rows = route_q.order_by(Route.id.desc()).limit(limit).all()
        titles = _ref_titles([t.ref for t, _f in tag_rows] + [x.a_ref for x in link_rows] + [x.b_ref for x in link_rows])
        tags = [{"id": t.id, "ref": t.ref, "title": titles.get(t.ref, t.ref), "facet": f.facet, "value": f.value,
                 "confidence": t.confidence} for t, f in tag_rows]
        links = [{"id": x.id, "a": x.a_ref, "a_title": titles.get(x.a_ref, x.a_ref), "b": x.b_ref,
                  "b_title": titles.get(x.b_ref, x.b_ref), "kind": x.kind, "confidence": x.confidence}
                 for x in link_rows]
        routes = [{"id": r.id, "question": r.question, "route": r.moa, "by": r.moa_by, "signal": r.signal,
                   "at": r.signal_at} for r in route_rows]
        counts = {"memory": mem_q.count(), "recipes": rec_q.count(), "descriptions": desc_q.count(),
                  "values": val_q.count(), "tags": tag_q.count(), "links": link_q.count(), "routes": route_q.count()}
        return _json({"memory": memories, "recipes": recipes, "descriptions": described, "values": values,
                      "tags": tags, "links": links, "routes": routes, "counts": counts,
                      "waiting": sum(counts[k] for k in ("memory", "recipes", "values", "tags", "links"))})

    @expose("/api/facets", methods=("GET",))
    @has_access_api
    def facets(self) -> Response:
        """The categories (?facet=, ?status=, ?q=), each value with its number of items and a few of them."""
        from sqlalchemy import func

        from superset import db

        from supagent.models import Facet, Tag

        q = db.session.query(Facet).filter(Facet.status != "rejected")
        if request.args.get("facet"):
            q = q.filter(Facet.facet == request.args["facet"])
        if request.args.get("status"):
            q = q.filter(Facet.status == request.args["status"])
        if request.args.get("q"):
            q = q.filter(Facet.value.ilike(f"%{request.args['q'][:100]}%"))
        rows = q.order_by(Facet.facet, Facet.value).limit(1000).all()
        ids = [f.id for f in rows] or [-1]
        n_items = dict(db.session.query(Tag.facet_id, func.count(Tag.id)).filter(
            Tag.facet_id.in_(ids), Tag.status == "approved").group_by(Tag.facet_id).all())
        rn = func.row_number().over(partition_by=Tag.facet_id, order_by=Tag.id).label("rn")
        sub = (db.session.query(Tag.facet_id.label("fid"), Tag.ref.label("ref"), rn)
               .filter(Tag.facet_id.in_(ids), Tag.status == "approved").subquery())
        some: dict[int, list[str]] = {}
        for fid, ref in db.session.query(sub.c.fid, sub.c.ref).filter(sub.c.rn <= 5):
            some.setdefault(fid, []).append(ref)
        titles = _ref_titles([r for refs in some.values() for r in refs])
        out = [{"id": f.id, "facet": f.facet, "value": f.value, "status": f.status, "source": f.source,
                "description": f.description, "synonyms": f.synonyms or [], "items": n_items.get(f.id, 0),
                "examples": [titles.get(r, r) for r in some.get(f.id, [])]} for f in rows]
        order = {"aspect": 0, "subject": 1, "application": 2, "component": 3}
        out.sort(key=lambda x: (order.get(x["facet"], 9), x["status"] != "proposed", -x["items"], x["value"].lower()))
        return _json({"facets": out})

    @expose("/api/facets/<int:fid>", methods=("POST",))
    @has_access_api
    def set_facet(self, fid: int) -> Response:
        """Approve, reject, rename, describe a value, or merge it into another (its items move there)."""
        import datetime as dt

        from superset import db

        from supagent.knowledge.facets import CONFIDENT
        from supagent.knowledge.freshness import touch
        from supagent.models import Facet, Tag

        f = db.session.get(Facet, fid)
        if f is None:
            abort(404)
        body = _body()
        if body.get("merge_into"):
            to = db.session.get(Facet, int(body["merge_into"]))
            if to is None or to.facet != f.facet or to.id == f.id:
                return _json({"error": "merge into another value of the same category"}, 400)
            for t in db.session.query(Tag).filter(Tag.facet_id == f.id):
                if db.session.query(Tag).filter(Tag.ref == t.ref, Tag.facet_id == to.id).first() is None:
                    t.facet_id = to.id
                else:
                    db.session.delete(t)
            to.synonyms = sorted(set((to.synonyms or []) + [f.value]))
            db.session.delete(f)
            db.session.commit()
            touch()
            db.session.commit()
            return _json({"merged_into": to.id})
        if body.get("status") in ("approved", "proposed", "rejected"):
            f.status = body["status"]
            if f.status == "approved":        # its confident tags are used at once
                for t in db.session.query(Tag).filter(Tag.facet_id == f.id, Tag.status == "proposed"):
                    if (t.confidence or 0) >= CONFIDENT:
                        t.status = "approved"
        if str(body.get("value") or "").strip():
            f.value = str(body["value"]).strip()[:128]
        if "description" in body:
            f.description = str(body.get("description") or "").strip() or None
        if "synonyms" in body:
            syn = body.get("synonyms")
            f.synonyms = [x.strip() for x in (syn if isinstance(syn, list) else str(syn or "").split(",")) if x.strip()]
        f.reviewed_by, f.reviewed_at = g.user.username, dt.datetime.utcnow()
        db.session.commit()
        touch()
        db.session.commit()
        return _json({"id": f.id, "status": f.status, "value": f.value})

    @expose("/api/tags/<int:tid>", methods=("POST",))
    @has_access_api
    def set_tag(self, tid: int) -> Response:
        from superset import db

        from supagent.knowledge.freshness import touch
        from supagent.models import Tag

        t = db.session.get(Tag, tid)
        if t is None:
            abort(404)
        if _body().get("status") in ("approved", "rejected"):
            t.status, t.reviewed_by = _body()["status"], g.user.username
            db.session.commit()
            touch()
            db.session.commit()
        return _json({"id": t.id, "status": t.status})

    @expose("/api/links/<int:lid>", methods=("POST",))
    @has_access_api
    def set_link(self, lid: int) -> Response:
        from superset import db

        from supagent.models import Link

        x = db.session.get(Link, lid)
        if x is None:
            abort(404)
        if _body().get("status") in ("approved", "rejected"):
            x.status, x.reviewed_by = _body()["status"], g.user.username
            db.session.commit()
        return _json({"id": x.id, "status": x.status})

    @expose("/api/routes/<int:rid>", methods=("POST",))
    @has_access_api
    def set_route(self, rid: int) -> Response:
        """A learned route: an admin keeps or corrects it ({"route": ...}: then it decides alone for the same
        question) or removes it ({"remove": true}: it no longer teaches)."""
        import datetime as dt

        from superset import db

        from supagent.governed.gate import CONFIRMED
        from supagent.models import Route
        from supagent.router import ROUTES

        r = db.session.get(Route, rid)
        if r is None:
            abort(404)
        body = _body()
        if body.get("remove"):
            r.moa_followed = False
        elif body.get("route") in ROUTES:              # right as it is, or corrected: an admin's example now
            r.moa, r.moa_by, r.moa_followed = body["route"], "admin", True
            if r.signal not in CONFIRMED:                # the answer's own signal is kept (the route was judged)
                r.signal, r.signal_at = "confirmed", dt.datetime.utcnow()
        else:
            return _json({"error": "route or remove"}, 400)
        db.session.commit()
        return _json({"id": r.id, "route": r.moa, "followed": r.moa_followed, "signal": r.signal})

    @expose("/api/apply", methods=("GET",))
    @has_access_api
    def apply_status(self) -> Response:
        from supagent.knowledge.apply import status

        return _json(status())

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
        from supagent import settings
        from supagent.workers import live_workers

        return _json({"version": __version__, "schema_version": row.value if row else None,
                      "celery_workers": workers_alive(), "workers": len(live_workers(fresh=True)),
                      "executor": settings.get("agent.executor"), "superset_mcp": mcp,
                      "daily_tick_scheduled": BEAT_KEY in (celery_app.conf.beat_schedule or {}),
                      "last_scheduled_run": last.started_at if last else None,
                      "last_scheduled_status": last.status if last else None})
