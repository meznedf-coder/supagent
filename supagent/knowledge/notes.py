"""Notes (0.7): what a user writes down in a few seconds (what a meeting decided, a fact the team must not lose),
for the team or for themselves, from the chat (the Notes button, or "/note ..." in the question box: the LLM is
not asked) or the Data dictionary. The agent finds them like the documents and always says whose note it is and
of which day: a note is not verified. It is never a team rule, never the source of a query's condition (the
conditions check leaves note lines out), and an admin may make a catalog entry of it (then verified). Personal
notes are found only in their author's answers and lists."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterator

from superset import db

from supagent.models import Note

MAX_CHARS = 50_000            # a meeting's minutes fit; a document belongs in Documents and sites
TITLE_CHARS = 120
MAX_TAGS = 10
MAX_VERSIONS = 20             # a note's states before its last changes: one can be put back
SAME_SECONDS = 600            # the same text by the same author again within 10 minutes: the same note
KIND = "teamnote"             # the pieces' kind ("guide" is the catalog's, named "note" before 0.8)
COMMAND = re.compile(r"^\s*/(note|mynote)\b[ \t]*:?\s*", re.I)


class NoteError(ValueError):
    pass


def command(text: str) -> dict[str, str] | None:
    """"/note <text>" (for the team) or "/mynote <text>" (for me) typed in the question box: the note to save,
    else None."""
    m = COMMAND.match(text or "")
    if m is None:
        return None
    return {"scope": "user" if m.group(1).lower() == "mynote" else "team", "text": (text or "")[m.end():].strip()}


WRITE = re.compile(             # "Note for the team: ...", "Make a personal note for me: ...", "Add a note: ..."
    r"^\W*(?:please\s+|can you\s+|could you\s+)?(?:"
    r"(?:make|add|take|write|save|create|keep)\s+(?:a\s+|this\s+|the\s+)?(?P<kind>personal\s+|private\s+|team\s+)?"
    r"note(?:\s+(?:for|to)\s+(?P<who1>the\s+team|everyone|us|me|myself))?"
    r"|note\s+(?:for|to)\s+(?P<who2>the\s+team|everyone|us|me|myself)"
    r"|(?:prends|ajoute|cr[ée]e|[ée]cris)\s+une\s+note(?P<kind_fr>\s+personnelle|\s+priv[ée]e)?"
    r"(?:\s+pour\s+(?P<who3>l'[ée]quipe|moi))?|note\s+pour\s+(?P<who4>l'[ée]quipe|moi))"
    r"\s*:\s*(?P<text>\S.*)$", re.I | re.S)


def asked_to_write(text: str) -> dict[str, str] | None:
    """A message that asks in words to write a note, its text after a colon: {scope, text}, else None. A bare
    "Note: ..." is not one (it may give context to a question)."""
    m = WRITE.match(text or "")
    if m is None:
        return None
    who = " ".join(x for x in (m.group("who1"), m.group("who2"), m.group("who3"), m.group("who4")) if x).lower()
    kind = " ".join(x for x in (m.group("kind"), m.group("kind_fr")) if x).lower()
    personal = bool(re.search(r"personal|private|personnelle|priv", kind)) or bool(re.search(r"\b(me|myself|moi)\b", who))
    return {"scope": "user" if personal else "team", "text": m.group("text").strip()}


def _title_of(text: str) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if line:
            return line[:TITLE_CHARS] + ("…" if len(line) > TITLE_CHARS else "")
    return "Note"


def _tags(tags: Any) -> list[str]:
    raw = tags.split(",") if isinstance(tags, str) else list(tags or [])
    out: list[str] = []
    for t in raw:
        t = " ".join(str(t).lower().split()).strip("#")[:40]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _day(value: Any) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as ex:
        raise NoteError("the day: YYYY-MM-DD") from ex


def _scope(scope: Any) -> str:
    scope = str(scope or "team")
    if scope not in ("team", "user"):
        raise NoteError("for: team or user")
    return scope


def add(user_id: int, text: str, title: str | None = None, scope: str = "team", tags: Any = None,
        meeting_on: Any = None, source: str = "page", by: str | None = None) -> Note:
    text = (text or "").strip()
    if not text:
        raise NoteError("the note is empty")
    if len(text) > MAX_CHARS:
        raise NoteError(f"{len(text):,} characters: a note holds {MAX_CHARS:,} at most (a longer text: Data "
                        "dictionary -> Documents and sites)")
    scope = _scope(scope)
    since = dt.datetime.utcnow() - dt.timedelta(seconds=SAME_SECONDS)
    same = (db.session.query(Note).filter(Note.user_id == user_id, Note.text == text, Note.created_at >= since)
            .order_by(Note.id.desc()).first())
    if same is not None:                                   # a double click, a "/note" sent twice
        return same
    n = Note(user_id=user_id, scope=scope, text=text, title=(title or "").strip()[:300] or _title_of(text),
             tags=_tags(tags), meeting_on=_day(meeting_on), source=source[:16], updated_by=by)
    db.session.add(n)
    db.session.commit()
    return n


def visible(user_id: int | None) -> Any:
    """The notes this user may read: the team's and their own."""
    from sqlalchemy import and_, or_

    return db.session.query(Note).filter(or_(Note.scope == "team", and_(Note.scope == "user",
                                                                        Note.user_id == (user_id or -1))))


def can_change(n: Note, user_id: int | None, admin: bool) -> bool:
    """Its author, or an admin (a team note only: a personal note stays its author's)."""
    return n.user_id == user_id or (admin and n.scope == "team")


def _state(n: Note) -> dict[str, Any]:
    return {"text": n.text, "title": n.title, "tags": list(n.tags or []),
            "meeting_on": n.meeting_on.isoformat() if n.meeting_on else None, "scope": n.scope,
            "at": (n.updated_at or n.created_at or dt.datetime.utcnow()).isoformat(timespec="seconds"),
            "by": n.updated_by}


def update(n: Note, values: dict[str, Any], by: str) -> Note:
    """The note changed; its state before is kept (versions, the last MAX_VERSIONS)."""
    before = _state(n)
    if "text" in values:
        text = str(values.get("text") or "").strip()
        if not text:
            raise NoteError("the note is empty")
        if len(text) > MAX_CHARS:
            raise NoteError(f"a note holds {MAX_CHARS:,} characters at most")
        n.text = text
    if "title" in values:
        n.title = str(values.get("title") or "").strip()[:300] or _title_of(n.text)
    if "tags" in values:
        n.tags = _tags(values.get("tags"))
    if "meeting_on" in values:
        n.meeting_on = _day(values.get("meeting_on"))
    if "scope" in values:
        n.scope = _scope(values.get("scope"))
    now = _state(n)
    if all(now[k] == before[k] for k in ("text", "title", "tags", "meeting_on", "scope")):
        return n                                           # nothing changed: no version
    n.versions = (list(n.versions or []) + [before])[-MAX_VERSIONS:]
    n.updated_by = by
    n.updated_at = dt.datetime.utcnow()
    db.session.commit()
    return n


def undo(n: Note, by: str) -> Note:
    """The note as it was before its last change (that version is taken back from its list)."""
    versions = list(n.versions or [])
    if not versions:
        raise NoteError("this note has no earlier version")
    last = versions.pop()
    n.text, n.title, n.tags, n.scope = last["text"], last["title"], list(last.get("tags") or []), _scope(last["scope"])
    n.meeting_on = _day(last.get("meeting_on"))
    n.versions = versions
    n.updated_by = by
    n.updated_at = dt.datetime.utcnow()
    db.session.commit()
    return n


YES = re.compile(r"^\W*(yes|yep|yeah|y|ok|okay|sure|confirm(ed)?|go ahead|do it|delete it|remove it|please do|correct|"
                 r"oui|d'accord|ok d'accord|vas-y|allez-y|confirm[ée]?|supprime[- ]la|c'est bon)\b", re.I)
ASKS_DELETE = re.compile(r"\b(delete|deleting|remove|removing|erase|supprim\w*|effac\w*)\b", re.I)


def delete_refusal(question: str, history: list[dict] | None, args: dict[str, Any]) -> str | None:
    """A note is deleted by the agent only after the user's yes: the answer before asked to delete this note
    (its title or number), and the user's message says yes. Else why not (the note is not deleted)."""
    if not args.get("confirmed"):
        return None                                        # without confirmed: the note is only shown
    try:
        note_id = int(args.get("note_id"))
    except (TypeError, ValueError):
        return None
    before = next((str(h.get("content") or "") for h in reversed(history or []) if h.get("role") == "assistant"), "")
    n = db.session.get(Note, note_id)
    named = n is not None and ((n.title or "").lower()[:40] in before.lower() or
                               re.search(rf"(?<![\w]){note_id}(?![\w])", before) is not None)
    if YES.match(question or "") and ASKS_DELETE.search(before) and named:
        return None
    return ("tool error (not run: confirmation): a note is deleted only after the user said yes to deleting it: "
            "show them the note (its title, author and day), ask whether to delete it, and stop; when their next "
            "message says yes, call delete_note with confirmed=true.")


def remove(n: Note) -> None:
    db.session.delete(n)
    db.session.commit()


def listing(user_id: int | None, q: str = "", offset: int = 0, limit: int = 20, mine: bool = False,
            tag: str | None = None, since: Any = None, until: Any = None) -> tuple[list[Note], int]:
    """A page of the notes this user may read, every word of `q` in the title, the text or the tags, of the days
    `since`-`until` (the day a note is about, else the day it was written): pinned first, then the newest."""
    from sqlalchemy import String, cast, func, or_

    query = visible(user_id)
    day = func.coalesce(Note.meeting_on, func.date(Note.created_at))
    if since:
        query = query.filter(day >= _day(since))
    if until:
        query = query.filter(day <= _day(until))
    if mine:
        query = query.filter(Note.user_id == (user_id or -1))
    for w in re.findall(r"\w+", (q or "").lower())[:8]:
        like = f"%{w}%"
        query = query.filter(or_(func.lower(Note.title).like(like), func.lower(Note.text).like(like),
                                 func.lower(cast(Note.tags, String)).like(like)))
    if tag:
        query = query.filter(func.lower(cast(Note.tags, String)).like(f"%\"{tag.lower()}\"%"))
    total = query.count()
    rows = (query.order_by(Note.pinned.desc(), day.desc(), Note.id.desc())
            .offset(max(0, int(offset))).limit(max(1, min(100, int(limit)))).all())
    return rows, total


def authors(ids: set[int]) -> dict[int, str]:
    """{user id: "First Last"} (else the username)."""
    from superset.extensions import security_manager as sm

    if not ids:
        return {}
    user = sm.user_model
    return {u.id: (" ".join(x for x in (u.first_name, u.last_name) if x) or u.username)
            for u in db.session.query(user).filter(user.id.in_(list(ids)))}


def day_of(n: Note) -> dt.date:
    return n.meeting_on or (n.created_at or dt.datetime.utcnow()).date()


def to_json(n: Note, me: int | None, admin: bool, names: dict[int, str]) -> dict[str, Any]:
    return {"id": n.id, "scope": n.scope, "title": n.title, "text": n.text, "tags": n.tags or [],
            "meeting_on": n.meeting_on.isoformat() if n.meeting_on else None, "day": day_of(n).isoformat(),
            "pinned": bool(n.pinned), "author": names.get(n.user_id, "?"), "mine": n.user_id == me,
            "can_change": can_change(n, me, admin), "entry_id": n.entry_id, "source": n.source,
            "versions": len(n.versions or []),
            "created_at": n.created_at, "updated_at": n.updated_at, "updated_by": n.updated_by}


def catalog_notes(q: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """The catalog's notes (an admin's, verified) whose title or text has every word of `q`: read in the same
    place as the team's notes."""
    from sqlalchemy import func

    from supagent.models import Entry

    query = db.session.query(Entry).filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True),
                                           Entry.classification == "guide")
    for w in re.findall(r"\w+", (q or "").lower())[:8]:
        query = query.filter((func.lower(Entry.title).like(f"%{w}%")) | (func.lower(Entry.content).like(f"%{w}%")))
    return [{"id": e.id, "title": e.title, "text": (e.content or "")[:4000], "category": e.category,
             "updated_at": e.updated_at, "updated_by": e.updated_by}
            for e in query.order_by(Entry.updated_at.desc()).limit(max(1, min(50, limit)))]


def promote(n: Note, by: str) -> Any:
    """A team note made a catalog entry (classification guide) by an admin: verified from then on."""
    from supagent.knowledge.catalog import save_entry

    if n.scope != "team":
        raise NoteError("only a team note can become a catalog entry")
    author = authors({n.user_id}).get(n.user_id, "?")
    e = save_entry({"title": n.title or _title_of(n.text), "classification": "guide", "category": "Team notes",
                    "content": f"{n.text}\n\n(From the note of {author} of {day_of(n).isoformat()}.)"}, by,
                   entry_id=n.entry_id if n.entry_id else None)
    n.entry_id = e.id
    n.updated_by = by
    db.session.commit()
    return e


def pieces() -> Iterator[dict[str, Any]]:
    """The searchable pieces of the notes (a long one in parts), each titled with its author and day."""
    from supagent.knowledge.index import split_text

    rows = db.session.query(Note).order_by(Note.id).all()
    names = authors({n.user_id for n in rows if n.user_id})
    for n in rows:
        whose = "Team note" if n.scope == "team" else "Personal note"
        title = (f"{whose} by {names.get(n.user_id, '?')} of {day_of(n).isoformat()} (not verified): "
                 f"{n.title or _title_of(n.text)}" + (f" [{', '.join(n.tags or [])}]" if n.tags else ""))
        for i, part in enumerate(split_text(n.text or "")):
            yield {"ref": f"note:{n.id}#{i}", "kind": KIND, "scope": n.scope,
                   "user_id": n.user_id if n.scope == "user" else None, "title": title[:500], "text": part}
