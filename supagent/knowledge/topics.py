"""The subjects of a chat (0.8). Each question belongs to one subject (Message.topic, its answer too): a question
that goes on from the previous one (it refers to it, completes it, answers its question back, asks the same for
another day or value, starts with "and", "what about"...) stays in its subject; one about other data starts a new
subject; one about the data of an earlier subject goes back to it. An answer is given the messages of its subject
only: from its start (the first exchange, what the follow-ups refer to), the exchanges in between shortened, the last
ones in full; never the other subjects of the chat.

Deciding, generically (nothing here is about a business domain): the words of the follow-ups, then what the
question is about compared with each subject: the named values (BILLING_API, srv-01...), the tables it most likely
needs (the resolver, without the LLM) and the tables each subject's answers read, the content words. When the words
cannot tell (one word in common, a question with a subject of its own), the LLM is asked in a short call, if there
is one. Not sure: the same subject, since more context only costs tokens while less context costs right answers.

agent.subjects = false: the last exchanges of the chat, as before 0.8.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

HISTORY_FULL = 3            # the last exchanges of the subject given in full (with the first one)
HISTORY_CHARS = 3000        # of one message given in full
MIDDLE_CHARS = 500          # of an answer in between (its first lines: the result, mostly)
MAX_HISTORY_CHARS = 24000   # the whole subject at most: the oldest exchanges in between go first
LEGACY_MESSAGES = 6         # messages of older versions (no subject): the last ones, as before
TOPIC_SECONDS = 30          # the LLM's decision at most (then: the same subject)

NEW_SUBJECT = re.compile(
    r"\b(new|another|different|other|unrelated|separate|next) (question|subject|topic|matter)\b|"
    r"\b(chang(e|ing)|switch(ing)?) (the )?(subject|topic)\b|\bunrelated\b|\bsomething else\b|"
    r"\b(nouvelle question|autre (sujet|question|chose)|changement de sujet|change(ons)? de sujet|rien [àa] voir)\b",
    re.I)
GOES_ON = re.compile(
    r"^\s*(and|also|plus|then|now|so|ok|okay|alright|what about|how about|same|again|instead|but|"
    r"et|aussi|alors|maintenant|pareil|idem|m[êe]me chose|ensuite|puis|mais|plut[ôo]t)\b", re.I)
BACK_TO = re.compile(
    r"\b(back to|return(ing)? to|go(ing)? back|earlier (question|subject|topic)|first (question|subject|topic)|"
    r"revenons|retour (à|au|aux)|reprenons|pour revenir)\b", re.I)
TIME_WORDS = (                         # the words of dates and periods: any subject has them (not "events", "market")
    "january february march april may june july august september october november december janvier fevrier mars "
    "avril mai juin juillet aout septembre octobre novembre decembre jan feb mar apr jun jul aug sep sept oct nov dec "
    "janv fev avr juil monday tuesday wednesday thursday friday saturday sunday lundi mardi mercredi jeudi vendredi "
    "samedi dimanche morning afternoon evening night nightly tonight week weekly weekend month monthly year yearly "
    "day daily today yesterday tomorrow hour hourly minute second hier demain aujourd hui semaine mois jour heure "
    "matin soir nuit annee quarter trimestre")


@dataclass
class Subject:
    id: int
    exchanges: list[tuple[Any, Any]] = field(default_factory=list)   # (question message, its answer or None)
    terms: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    values: set[str] = field(default_factory=set)

    @property
    def first(self) -> str:
        return str(self.exchanges[0][0].content or "") if self.exchanges else ""

    @property
    def last(self) -> str:
        return str(self.exchanges[-1][0].content or "") if self.exchanges else ""


def enabled() -> bool:
    from supagent import settings

    try:
        return bool(settings.get("agent.subjects"))
    except Exception:  # pylint: disable=broad-except
        return True


@functools.lru_cache(maxsize=1)
def _time_terms() -> frozenset:
    """The time words as the terms write them (stemmed, singular): whole words, not their first letters."""
    from supagent.knowledge.describe import stem

    words = TIME_WORDS.split()
    return frozenset(words) | frozenset(stem(w) for w in words) | frozenset(stem(w + "s") for w in words)


def _subject_terms(text: str) -> set[str]:
    """The content words of a text that can say what it is about (dates and periods are shared by any subject)."""
    from supagent.knowledge.resolve import terms

    return {t for t in terms(text or "") if t not in _time_terms()}


def _values(text: str) -> set[str]:
    from supagent.knowledge.resolve import VALUE_TOKEN

    return {v.upper() for v in VALUE_TOKEN.findall(text or "") if not re.fullmatch(r"\d+([-_]\d+)*", v)}


def _tables_of_results(results: Any) -> set[str]:
    """The tables (indices, metrics) the queries of an answer read."""
    from supagent.knowledge.experience import promql_pattern, sql_pattern

    out: set[str] = set()
    for r in results or []:
        q = str(r.get("sql") or "")
        if not q:
            continue
        try:
            _p, names = promql_pattern(q) if r.get("tool") == "promql_query" else sql_pattern(q)
        except Exception:  # pylint: disable=broad-except
            names = []
        out |= {str(n).lower() for n in names if n}
    return out


def _tables_of_steps(steps: Any) -> set[str]:
    """The tables an answer's tool calls named (its queries, the metric or index it described)."""
    from supagent.knowledge.experience import promql_pattern, sql_pattern

    out: set[str] = set()
    for s in steps or []:
        if s.get("status") not in (None, "done"):
            continue
        args = s.get("args") or {}
        req = args.get("request") if isinstance(args.get("request"), dict) else args
        q = req.get("sql") or req.get("expr") if isinstance(req, dict) else None
        if not q:
            continue
        try:
            _p, names = promql_pattern(q) if req.get("expr") else sql_pattern(q)
        except Exception:  # pylint: disable=broad-except
            names = []
        out |= {str(n).lower() for n in names if n}
    return out


def subjects(messages: list[Any]) -> tuple[dict[int, Subject], int | None]:
    """The chat's subjects from its messages (oldest first) and the subject of the last exchange. Messages of an
    older version (no subject) make subject 0."""
    out: dict[int, Subject] = {}
    last: int | None = None
    pending: Any = None
    for m in messages:
        if m.role == "user":
            if pending is not None:                       # a question without an answer (stopped): kept alone
                _add(out, pending, None)
            pending = m
        elif m.role == "assistant" and pending is not None:
            last = _add(out, pending, m)
            pending = None
    if pending is not None:
        last = _add(out, pending, None)
    return out, last


def _add(out: dict[int, Subject], q: Any, a: Any) -> int:
    tid = int(q.topic if q.topic is not None else (a.topic if a is not None and a.topic is not None else 0))
    s = out.setdefault(tid, Subject(id=tid))
    s.exchanges.append((q, a))
    s.terms |= _subject_terms(q.content or "")
    s.values |= _values(q.content or "")
    if a is not None and a.status == "done":
        s.values |= set(list(_values(a.content or ""))[:40])
        s.tables |= _tables_of_results(a.results) | _tables_of_steps(a.steps)
    return tid


def _question_tables(question: str) -> set[str]:
    """The tables the question most likely needs (the resolver: the dictionary and what earlier answers used)."""
    try:
        from supagent.knowledge.resolve import resolve

        found = resolve(question, limit=4)
    except Exception:  # pylint: disable=broad-except
        from superset import db

        db.session.rollback()
        return set()
    best = max((c.get("score") or 0 for c in found), default=0)
    return {str(c.get("parent") or c.get("name") or "").lower() for c in found
            if (c.get("score") or 0) >= max(2.0, best * 0.6)} - {""}


# the decisions that a question goes on from the last exchange with nothing of its own (the agent reads it as a
# follow-up: the previous question's context, its conditions kept); "the same data" is not one (a new question)
CONTINUES_LAST = frozenset({"reply to the question back", "completes the previous question", "goes on",
                            "no subject of its own", "refers to the previous answer",
                            "another day of the previous question"})


@dataclass
class Decision:
    topic: int | None            # the subject it belongs to (None: a new one)
    how: str                     # why (logged; the tests read it)
    back: bool = False           # it goes back to an earlier subject


def overlap(q_terms: set[str], q_tables: set[str], q_values: set[str], s: Subject) -> float:
    return 3.0 * len(q_values & s.values) + 2.0 * len(q_tables & s.tables) + 1.0 * len(q_terms & s.terms)


def _relative_day(text: str, cur: Subject) -> bool:
    """A short question with a day said relative to the subject's ("Compare with the day before.", "And the 23rd?",
    "that day"): the subject's previous question for that day (whatever tables its words make the resolver guess)."""
    from supagent.knowledge.period import BARE_DAY, DAY_AFTER, DAY_BEFORE, SAME_DAY, anchor_of

    if not any(rx.search(text) for rx in (BARE_DAY, DAY_BEFORE, DAY_AFTER, SAME_DAY)):
        return False
    from supagent.agent import now

    return anchor_of([str(q.content or "") for q, _a in cur.exchanges], now().date()) is not None


def decide(question: str, subs: dict[int, Subject], last: int | None, llm: Any = None) -> Decision:
    """The subject of a new question (see the module)."""
    from supagent.agent import FRAGMENT, FRAGMENT_WORDS, REPLY_WORDS, adds_to, asks_back, refers_back

    if not subs or last is None or last not in subs:
        return Decision(None, "first question")
    cur = subs[last]
    before = cur.last
    said = next((str(a.content or "") for _q, a in reversed(cur.exchanges) if a is not None and a.content), "")
    text = (question or "").strip()
    n_words = len(text.split())
    if said and asks_back(said) and not text.endswith("?") and n_words <= REPLY_WORDS:
        return Decision(last, "reply to the question back")
    explicit_new = bool(NEW_SUBJECT.search(text))
    back = bool(BACK_TO.search(text))
    if not explicit_new and not back:
        if FRAGMENT.match(text) and n_words <= FRAGMENT_WORDS:
            return Decision(last, "completes the previous question")
        if GOES_ON.match(text):
            return Decision(last, "goes on")
        if n_words <= FRAGMENT_WORDS + 4 and _relative_day(text, cur):
            return Decision(last, "another day of the previous question")
    q_terms, q_values = _subject_terms(text), _values(text)
    if not explicit_new and not back and not q_values and n_words <= FRAGMENT_WORDS + 4 and refers_back(text):
        return Decision(last, "refers to the previous answer")      # "What was its failure rate?": its answer
    q_tables = _question_tables(text)
    scored = sorted(((overlap(q_terms, q_tables, q_values, s), s.id) for s in subs.values()),
                    key=lambda x: (-x[0], -_recency(subs, x[1])))
    best_score, best = scored[0]
    last_score = next(sc for sc, sid in scored if sid == last)
    if explicit_new and not back:
        return Decision(None, "said a new subject")
    if back and best_score > 0:
        return Decision(best, "goes back (said)", back=best != last)
    if best_score >= 2.0:                           # its values, its tables or its words are a subject's
        if best != last and best_score >= last_score + 2.0:
            return Decision(best, "the data of an earlier subject", back=True)
        return Decision(last if last_score >= 2.0 else best, "the same data", back=last_score < 2.0 and best != last)
    own_data = bool(q_tables or q_values)
    if not own_data and len(q_terms) < 3:
        return Decision(last, "no subject of its own")
    pronoun = refers_back(text)
    unclear = (pronoun and best_score <= 0 and own_data) or best_score > 0 or (not own_data and best_score <= 0)
    if not unclear:
        asked = [str(q.content or "") for q, _a in cur.exchanges]
        if pronoun or (adds_to(text, before, asked) and n_words <= FRAGMENT_WORDS + 4 and not own_data):
            return Decision(last, "refers to the previous answer")
        return Decision(None, "other data")
    asked = ask_llm(text, subs, llm) if llm is not None else None
    if asked is not None:
        if asked == 0:
            return Decision(None, "the LLM: a new subject")
        if asked in subs:
            return Decision(asked, "the LLM: the same subject" if asked == last else "the LLM: an earlier subject",
                            back=asked != last)
    if pronoun or best_score > 0:
        return Decision(last, "not sure: the same subject")
    return Decision(None, "nothing in common")


def _recency(subs: dict[int, Subject], sid: int) -> int:
    s = subs[sid]
    return max((getattr(q, "id", 0) or 0) for q, _a in s.exchanges) if s.exchanges else 0


SYSTEM = ("You read a chat about data. Say whether the new question continues one of the chat's subjects (it asks "
          "more about the same thing, refers to its results, or asks the same for another period or value) or starts "
          "a new subject (other data, another matter). Answer with the number of the subject it continues, or 0 for "
          "a new subject: one number only.")


def ask_llm(question: str, subs: dict[int, Subject], llm: Any) -> int | None:
    """The LLM's reading (a short call): the number of the subject the question continues, 0 for a new one, None
    when it did not answer."""
    from supagent.llm import bounded

    lines = []
    for sid in sorted(subs, key=lambda x: _recency(subs, x)):
        s = subs[sid]
        qs = [str(q.content or "") for q, _a in s.exchanges]
        shown = qs if len(qs) <= 4 else qs[:1] + ["…"] + qs[-2:]
        lines.append(f"Subject {sid}: " + " | ".join(" ".join(x.split())[:200] for x in shown))
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(lines) + f"\nNew question: {' '.join(question.split())[:600]}\n"
                                        "The subject it continues (0: a new subject):"}]
    try:
        with bounded(llm, TOPIC_SECONDS):
            msg = llm.chat(msgs, max_tokens=12)
    except Exception as ex:  # pylint: disable=broad-except   (the same subject)
        log.info("supagent subjects: the LLM did not decide: %s", str(ex)[:200])
        return None
    text = re.sub(r"<think>.*?</think>", "", str((msg or {}).get("content") or ""), flags=re.S)
    m = re.search(r"\d+", text)
    if not m:
        return None
    n = int(m.group(0))
    return n if n == 0 or n in subs else None


def history(s: Subject | None, before_id: int | None = None) -> list[dict[str, Any]]:
    """The messages of a subject given with the new question (see the module): [{"role", "content", "message"}]."""
    if s is None:
        return []
    ex = [(q, a) for q, a in s.exchanges if (before_id is None or q.id < before_id)]
    done = [(q, a) for q, a in ex if a is not None and a.status == "done" and (a.content or "")]
    if not done:
        return []
    if s.id == 0:                                 # messages of an older version: the last ones, as before
        flat = [m for pair in done for m in pair][-LEGACY_MESSAGES:]
        return [{"role": m.role, "content": str(m.content or "")[:HISTORY_CHARS], "message": m} for m in flat]
    keep_full = {0} | set(range(max(1, len(done) - HISTORY_FULL), len(done)))
    out: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for i, (q, a) in enumerate(done):
        full = i in keep_full
        answer = str(a.content or "")
        if not full and len(answer) > MIDDLE_CHARS:
            answer = answer[:MIDDLE_CHARS].rsplit(" ", 1)[0] + " …"
        out.append((i, {"role": "user", "content": str(q.content or "")[:HISTORY_CHARS], "message": q},
                    {"role": "assistant", "content": answer[:HISTORY_CHARS], "message": a}))
    total = sum(len(x["content"]) + len(y["content"]) for _i, x, y in out)
    while total > MAX_HISTORY_CHARS:               # the oldest exchanges in between go first
        middle = [k for k, (i, _x, _y) in enumerate(out) if i not in keep_full]
        if not middle:
            break
        _i, x, y = out.pop(middle[0])
        total -= len(x["content"]) + len(y["content"])
    return [m for _i, x, y in out for m in (x, y)]


def assign(messages: list[Any], question_msg: Any, answer_msg: Any, llm: Any = None) -> tuple[Decision,
                                                                                               list[dict[str, Any]]]:
    """The subject of a new question (its message and its answer's get it) and the history given with it, from the
    chat's earlier messages (oldest first, the question excluded)."""
    subs, last = subjects(messages)
    if not enabled():
        flat = [m for m in messages if m.status == "done" and m.content][-LEGACY_MESSAGES:]
        return Decision(last, "off"), [{"role": m.role, "content": str(m.content or "")[:HISTORY_CHARS], "message": m}
                                       for m in flat]
    d = decide(str(question_msg.content or ""), subs, last, llm)
    if d.topic is None:
        d.topic = max([sid for sid in subs] + [0]) + 1
    question_msg.topic = answer_msg.topic = d.topic
    return d, history(subs.get(d.topic), before_id=question_msg.id)


def describe(d: Decision) -> str:
    return json.dumps({"topic": d.topic, "how": d.how, "back": d.back})
