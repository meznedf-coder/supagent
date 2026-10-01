"""Preferences, rules and facts learned from the chats, for a user or for the team.

Learned only on explicit signals (never from every chat: a wrong rule would change everyone's
answers, and the LLM is shared): an answer marked Helpful, or a question that says "always",
"from now on", "remember", "by default", "toujours", "désormais", "retiens", "par défaut"...
The LLM extracts the durable points of that exchange; a user can also add or delete their own.

Personal memories are used at once, in their author's answers only. Team memories are used
after an admin's approval (memory.team_approval, default) or at once when approval is off.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from superset import db

from supagent import settings
from supagent.models import Conversation, Memory, Message

log = logging.getLogger(__name__)
SIGNALS = re.compile(r"\b(always|never|from now on|remember|by default|in future|keep in mind|i prefer|we prefer|"
                     r"memory|memori[sz]e|note (?:it|that)|when i say|you should know|"
                     r"toujours|jamais|d[ée]sormais|dor[ée]navant|retiens|souviens|par d[ée]faut|je pr[ée]f[èe]re|"
                     r"nous pr[ée]f[ée]rons|à l'avenir|m[ée]moire|m[ée]moris\w*|quand je dis|tu dois savoir)\b", re.I)
PROMPT = """You keep the memory of a data assistant used by a team. From the exchange below, list
only DURABLE points worth remembering for later questions: how this user or the team wants
answers (format, units, sorting, time zone, what 'yesterday' or 'today' means, environments to
exclude...), business rules and definitions stated by the user, facts about the data the user
asserted. Do not list what the assistant found in the data (results change), nor one-off
requests: an instruction for this question only ("compute it as X", "for 23 September", "as a
table") is not durable unless the user says it applies from now on, always, or to everyone.
Answer with a JSON list only (empty list when nothing is durable):
[{"text": "...", "scope": "personal" or "team", "kind": "preference" or "rule" or "fact",
  "category": "a short topic"}]
scope "team" only when the user says it applies to everyone or states a business rule.
When the assistant asked the user what a word meant and the user answered, the answer is a
definition worth keeping (team, fact, when it says what a business word or a field means). When
the user marked the answer Not helpful and said why, keep what the reason says about the data or
how to compute it (a rule or a definition: team), not a complaint."""


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9àâçéèêëîïôûùüÿœ]+", (text or "").lower()))


def _similar(a: str, b: str) -> bool:
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / max(len(wa), len(wb)) >= 0.8


def add(user_id: int, text: str, scope: str = "user", kind: str = "preference", category: str | None = None,
        source: str = "manual", message_id: int | None = None, approved_by: str | None = None) -> Memory | None:
    """A memory (None when an equivalent one exists). Never a second copy: a person writing again a
    memory that was disabled brings it back (for the team: to approve again); one learned from a chat
    that someone disabled stays disabled (it was refused)."""
    text = (text or "").strip()
    if not text:
        return None
    scope = "team" if scope in ("team", "everyone") else "user"
    q = db.session.query(Memory)
    q = q.filter(Memory.scope == "team") if scope == "team" else q.filter(Memory.scope == "user",
                                                                           Memory.user_id == user_id)
    same = [m for m in q.limit(5000) if _similar(m.text, text)]
    status = "active"
    if scope == "team" and settings.get("memory.team_approval") and not approved_by:
        status = "proposed"
    if same:
        kept = next((m for m in same if m.status != "disabled"), None)
        if kept is not None or source != "manual":
            return None
        m = same[0]                                     # written again by a person: back, not a copy
        m.status, m.text, m.approved_by = status, text[:1000], approved_by
        m.kind = kind if kind in ("preference", "rule", "fact") else m.kind
        db.session.commit()
        return m
    m = Memory(scope=scope, user_id=user_id, kind=kind if kind in ("preference", "rule", "fact") else "preference",
               text=text[:1000], category=(category or None), status=status, source=source, message_id=message_id,
               approved_by=approved_by)
    db.session.add(m)
    db.session.commit()
    return m


QUESTION_START = re.compile(
    r"^\s*(what|which|who|whom|whose|when|where|why|how|is|are|do|does|did|can|could|would|will|should|show|give|"
    r"list|find|get|tell|display|draw|plot|compare|count|calculate|compute|explain|send|export|create|make|save|"
    r"need|i need|i want|i would|please|quel|quelle|quels|quelles|qui|quand|o[uù]|pourquoi|comment|combien|"
    r"est-ce|peux|pouvez|peut|donne|montre|affiche|liste|trouve|calcule|compare|envoie|exporte|cr[ée]e|fais|"
    r"j'ai besoin|je veux|je voudrais)\b", re.I)
DECLARATIVE = re.compile(r"\b(is|are|means|mean|equals|stands for|refers to|corresponds to|contains|holds|represents|"
                         r"est|sont|signifie|veut dire|correspond|contient|d[ée]signe|repr[ée]sente)\b|=", re.I)


def is_statement(text: str) -> bool:
    """A message that tells something (the field X means Y, KO = failed), not a question: what it
    says about the data is worth keeping (the LLM extracts it; team facts wait for an admin)."""
    t = (text or "").strip()
    if not t or "?" in t or len(t) > 1500 or QUESTION_START.search(t):
        return False
    return bool(DECLARATIVE.search(t))


def worth_learning(question: str) -> bool:
    return bool(SIGNALS.search(question or "")) or is_statement(question)


def learn_from_message(message_id: int, llm: Any = None) -> list[int]:
    """The durable points of the exchange that ends with this answer."""
    from supagent.llm import LLM

    if not settings.get("memory.enabled"):
        return []
    answer = db.session.get(Message, message_id)
    if answer is None or answer.role != "assistant":
        return []
    conv = db.session.get(Conversation, answer.conversation_id)
    question = (db.session.query(Message).filter(Message.conversation_id == answer.conversation_id,
                                                 Message.id < message_id, Message.role == "user")
                .order_by(Message.id.desc()).first())
    if conv is None or question is None:
        return []
    exchange = f"USER: {question.content[:3000]}\nASSISTANT: {(answer.content or '')[:3000]}"
    asked = asked_back(question)
    if asked is not None:                              # the user answered a question of the assistant
        first, back = asked
        exchange = (f"USER: {first.content[:2000]}\nASSISTANT (asking what the user meant): {back.content[:1500]}\n"
                    + exchange)
    if answer.feedback == -1 and answer.feedback_reason:
        exchange += f"\nUSER (marked this answer Not helpful): {answer.feedback_reason[:1000]}"
    from supagent.llm import llm_task

    try:
        with llm_task("memory", user_id=conv.user_id, message_id=message_id):
            msg = (llm or LLM()).chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": exchange}],
                                      max_tokens=600)
    except Exception as ex:  # pylint: disable=broad-except
        log.warning("supagent memory: %s", ex)
        return []
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
    start, end = text.find("["), text.rfind("]")
    try:
        items = json.loads(text[start:end + 1]) if 0 <= start < end else []
    except ValueError:
        items = []
    made = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("text") or "").strip():
            continue
        m = add(conv.user_id, str(it["text"]), scope="team" if it.get("scope") == "team" else "user",
                kind=str(it.get("kind") or "preference"), category=it.get("category"), source="chat",
                message_id=message_id)
        if m is not None:
            made.append(m.id)
    if made:
        from supagent.knowledge.index import embed_few, sync

        embed_few(sync(("memory:",)))
    return made


def asked_back(question: Message) -> tuple[Message, Message] | None:
    """(the user's question before, the assistant's question back) when `question` answers what
    the assistant asked (rule 6 of the agent)."""
    from supagent.agent import asks_back

    back = (db.session.query(Message).filter(Message.conversation_id == question.conversation_id,
                                             Message.id < question.id).order_by(Message.id.desc()).first())
    if back is None or back.role != "assistant" or not asks_back(back.content or ""):
        return None
    first = (db.session.query(Message).filter(Message.conversation_id == question.conversation_id,
                                              Message.id < back.id, Message.role == "user")
             .order_by(Message.id.desc()).first())
    return (first, back) if first is not None else None


def memories_for(user_id: int | None, limit: int = 15) -> list[Memory]:
    """The user's personal memories and the team's active ones (for the prompt)."""
    q = db.session.query(Memory).filter(Memory.status == "active")
    mine = q.filter(Memory.scope == "user", Memory.user_id == user_id).order_by(Memory.id.desc()).limit(limit).all() \
        if user_id is not None else []
    team = q.filter(Memory.scope == "team").order_by(Memory.id.desc()).limit(limit).all()
    return mine + team


KIND_ORDER = {"rule": 0, "preference": 1, "fact": 2}
ENTRY_CHARS = 400


def prompt_block(user_id: int | None, shown: set[str] | None = None) -> str:
    """The memories given with every question, within memory.prompt_chars: rules, then
    preferences, then facts (the user's own first, the newest first). Facts left out are still
    found by the knowledge search when a question is about them (`shown` gets the refs of the
    ones given, not given twice)."""
    items = memories_for(user_id)
    if not items:
        return ""
    from supagent.knowledge.resolve import gone_names, mentions_gone

    budget = int(settings.get("memory.prompt_chars") or 0) or 10 ** 9
    gone = gone_names()
    lines = ["\n\nWhat this user and the team asked to remember (follow it unless the question says otherwise):"]
    used = 0
    for m in sorted(items, key=lambda m: KIND_ORDER.get(m.kind, 3)):     # stable: mine, then the team's
        if mentions_gone(m.text, gone):                # about a metric or index that no longer exists
            continue
        who = "team" if m.scope == "team" else "this user"
        text = " ".join((m.text or "").split())
        line = f"- ({who}, {m.kind}) {text[:ENTRY_CHARS]}{'...' if len(text) > ENTRY_CHARS else ''}"
        if used + len(line) > budget:
            continue
        lines.append(line)
        used += len(line)
        if shown is not None:
            shown.add(f"memory:{m.id}")
    return "\n".join(lines) if len(lines) > 1 else ""


STATUS_RANK = {"active": 0, "catalog": 1, "proposed": 2, "disabled": 3}


def merge_duplicates() -> int:
    """Copies of a memory (the same words, the same scope and, for a personal one, the same user): one
    stays (active first, then the newest), the others are deleted. Returns how many were deleted."""
    groups: dict[tuple, list[Memory]] = {}
    for m in db.session.query(Memory):
        key = (m.scope, m.user_id if m.scope == "user" else None, _norm(m.text))
        groups.setdefault(key, []).append(m)
    gone = 0
    for same in groups.values():
        if len(same) < 2:
            continue
        same.sort(key=lambda m: (STATUS_RANK.get(m.status or "", 4), -(m.id or 0)))
        for m in same[1:]:
            db.session.delete(m)
            gone += 1
    if gone:
        db.session.commit()
    return gone
