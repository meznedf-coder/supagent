"""Generic questions: what a learned answer and a chat are named.

A question often carries the data of one case ("why is job 202609253742945492 failing?",
"the same for BILLING", "the @timestamp_date is a timestamp"). Stored as it is, it cannot be
recognised the next time, and it keeps data that is useless later. After each answer the
LLM writes (one short call, after the answer is shown):

  question   the question the final query answered, standalone (the earlier messages taken
             into account when the last one only continues or corrects them), short and
             generic: "Why did a given job fail?", "Failed jobs per application on a given day"
  title      two to six words for the chat ("Failed job details")

Without the LLM (an error), a plain version is made (values replaced by <id>, <date>, ...)
and marked not generic, so that the daily learning writes it again later. The daily
learning also rewrites the learned answers and chat names made before this existed, and
merges the learned answers that turn out to be the same.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from superset import db

log = logging.getLogger(__name__)
PROMPT = """You file the questions a data assistant answered, so that the team can find and reuse
the way each one was answered. Read the conversation and the final query, then write JSON:
- "question": the data question the final query answers, written as a generic template:
  * standalone: when the last message only continues, narrows or corrects an earlier one,
    write the complete question;
  * every value of this particular case is replaced by its kind: application, server or team
    names, ids, dates, days, periods, numbers and codes become "a given application", "a given
    server", "a given job", "a given day", "a given period"...;
  * keep the field, table and metric names that tell how it is answered;
  * at most 15 words.
- "title": two to six words, generic too (no names, ids, dates or numbers).
- "reusable": false only when no data question was answered (a greeting, thanks, a remark).
- "same_as": when learned questions already kept are listed, the number of the one that asks the
  same thing as your question (only the values of one case differ), else null. A question on
  another field, grouping or metric is not the same.
Examples:
  "how many jobs of BILLING failed on 23 September?" ->
  {"question": "How many jobs of a given application failed on a given day?", "title": "Failed jobs per application", "reusable": true, "same_as": null}
  "why is job 202609253742945492 failing?" ->
  {"question": "Why did a given job fail, with its error details?", "title": "Failed job details", "reusable": true, "same_as": null}
  earlier "failed jobs by application yesterday", last "and only on srv-emea-001?" ->
  {"question": "Failed jobs by application on a given day for a given server", "title": "Failed jobs by server", "reusable": true, "same_as": null}
Write in the language of the user's messages. Answer with the JSON only."""
MONTH = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|"
         r"mar|apr|jun|jul|aug|sept|sep|oct|nov|dec|janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|"
         r"septembre|octobre|novembre|d[ée]cembre)\.?")
DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
                   r"|\b\d{1,2}(?:st|nd|rd|th|er)?\s+(?:of\s+)?" + MONTH + r"(?:\s+\d{4})?(?!\w)"
                   r"|\b" + MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+\d{4})?", re.I)
IDS = re.compile(r"\b\d{4,}\b|\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I)
FILLERS = re.compile(r"^\s*(please|pls|hi|hello|bonjour|salut|can you|could you|would you|peux[- ]tu|pouvez[- ]vous|"
                     r"est[- ]ce que tu peux|give me|show me|tell me|donne[- ]moi|montre[- ]moi|dis[- ]moi)\b[\s,:]*",
                     re.I)


def plain(question: str) -> str:
    """The question without the values of one case (no LLM)."""
    q = " ".join((question or "").split())
    for _ in range(3):
        q = FILLERS.sub("", q)
    q = re.sub(r"'[^']{1,80}'|\"[^\"]{1,80}\"", "<value>", q)
    q = re.sub(r"\b\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b(19|20)\d{6}\b",
               "<date>", q)
    q = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", "<time>", q)
    q = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{5,}\b", "<id>", q, flags=re.I)
    q = re.sub(r"(=|:)\s*(?!<)[\w.\-]{3,}", r"\1 <value>", q)
    q = q.strip(" ,.;:")
    return (q[:1].upper() + q[1:])[:160] if q else (question or "")[:160]


def plain_title(question: str) -> str:
    words = plain(question).split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


def case_values(texts: list[str], query: str | None) -> tuple[set[str], set[str]]:
    """The values of this one case that the user wrote: (ids, long numbers and dates; the quoted
    values of the query written as they are: names, codes). The first ones never stay in a
    generic question; the others are sent back to the LLM once (a status such as KO may stay)."""
    said = " ".join(t for t in texts if t)
    hard = {m.group(0).strip() for m in IDS.finditer(said)} | {m.group(0).strip() for m in DATES.finditer(said)}
    literals = {(a or b).strip("%* ") for a, b in re.findall(r"'([^']{2,80})'|\"([^\"]{2,80})\"", query or "")}
    soft = {v for v in literals if len(v) >= 2 and re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", said)}
    return {v for v in hard if len(v) >= 2}, soft - hard


def leaks(text: str, values: set[str]) -> list[str]:
    return sorted(v for v in values if re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", text or "", re.I))


def _parse(content: str) -> dict[str, Any]:
    raw = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    a, b = raw.find("{"), raw.rfind("}")
    data = json.loads(raw[a:b + 1]) if 0 <= a < b else {}
    return data if isinstance(data, dict) else {}


def generalize(question: str, earlier: list[str], query: str | None, tool: str | None = None,
               llm: Any = None, kept: list[tuple[int, str]] | None = None) -> dict[str, Any]:
    """{"question", "title", "reusable", "generic", "same_as"} for an answered question.
    `kept`: (id, question) of learned answers that may ask the same thing; "same_as" is one of
    these ids or None. A value of this case left in the question or the title is sent back
    once; ids, numbers and dates still there are then replaced."""
    from supagent.llm import LLM

    lines = [f"Earlier user message: {e[:500]}" for e in (earlier or [])[-3:] if e]
    lines.append(f"Last user message: {(question or '')[:1500]}")
    if query:
        lines.append(f"Final query ({tool or 'query'}): {query[:1500]}")
    if kept:
        lines.append("Learned questions already kept:\n" + "\n".join(f"{i}: {q[:200]}" for i, q in kept))
    hard, soft = case_values([question] + list(earlier or []), query)
    try:
        client = llm or LLM()
        messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n".join(lines)}]
        data = _parse(client.chat(messages, max_tokens=300).get("content") or "")
        left = leaks(f"{data.get('question', '')} {data.get('title', '')}", hard | soft)
        if data and left:
            messages += [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                         {"role": "user", "content": "These values of this one case are still there: " + ", ".join(left)
                          + ". Replace each one by its kind (a given application, a given day, a given job...); "
                          "keep only a fixed code that defines what is counted (such as a status). Give the JSON "
                          "again."}]
            again = _parse(client.chat(messages, max_tokens=300).get("content") or "")
            data = {**data, **{k: v for k, v in again.items() if v is not None}} if again else data
        q = " ".join(str(data.get("question") or "").split())
        title = " ".join(str(data.get("title") or "").split()).strip(" .")
        for v in leaks(f"{q} {title}", hard):              # ids, numbers and dates never stay
            pattern = r"(?<![\w-])" + re.escape(v) + r"(?![\w-])"
            q = re.sub(r"\s+", " ", re.sub(pattern, "a given value", q, flags=re.I))
            title = re.sub(r"\s+", " ", re.sub(pattern, "", title, flags=re.I)).strip(" -,.")
        ids = {i for i, _q in kept or []}
        same = data.get("same_as")
        same = int(same) if isinstance(same, (int, str)) and str(same).strip().isdigit() else None
        if q:
            return {"question": q[:240], "title": (title or plain_title(q))[:80],
                    "reusable": data.get("reusable") is not False, "generic": True,
                    "same_as": same if same in ids else None}
        if data:                                   # no data question (a greeting...): still a name
            return {"question": plain(question), "title": (title or plain_title(question))[:80], "reusable": False,
                    "generic": True, "same_as": None}
    except Exception as ex:  # pylint: disable=broad-except   (named plainly; rewritten by the daily learning)
        log.warning("supagent: generic question not written: %s", ex)
    return {"question": plain(question), "title": plain_title(question), "reusable": True, "generic": False,
            "same_as": None}


# --------------------------------------------------------------------------- #
# the learned answers and chats made before (daily learning, `superset supagent tidy-learned`)
# --------------------------------------------------------------------------- #
def _similar(a: str, b: str) -> bool:
    from supagent.knowledge.experience import words

    wa, wb = words(a), words(b)
    return bool(wa and wb) and len(wa & wb) / max(len(wa), len(wb)) >= 0.6


def absorb(keep: Any, dup: Any) -> None:
    """Two learned answers that are the same question become `keep`: uses added up, the
    confirmations joined, the latest use kept, confirmed by an admin if either was; `dup` is
    deleted (not committed here)."""
    keep.uses = (keep.uses or 1) + (dup.uses or 1)
    keep.confirmations = sorted(set(keep.confirmations or []) | set(dup.confirmations or []))
    if dup.status == "confirmed":
        keep.status = "confirmed"
    if (dup.last_used_at and keep.last_used_at and dup.last_used_at > keep.last_used_at) or not keep.last_used_at:
        keep.last_used_at, keep.message_id = dup.last_used_at, dup.message_id
    if dup.generic and not keep.generic:
        keep.question, keep.words, keep.generic = dup.question, dup.words, True
    db.session.delete(dup)


def merge_duplicates() -> int:
    """Learned answers with the same query shape and a similar question become one."""
    from supagent.models import Recipe

    merged = 0
    from supagent.knowledge.experience import USED

    rows = db.session.query(Recipe).filter(Recipe.status.in_(USED)).order_by(Recipe.id).all()
    kept: list[Recipe] = []
    for r in rows:
        twin = next((k for k in kept if k.signature == r.signature and _similar(k.question, r.question)), None)
        if twin is None:
            kept.append(r)
            continue
        absorb(twin, r)
        merged += 1
    db.session.commit()
    return merged


def tidy_learned(llm: Any = None, limit: int = 50, deadline: float | None = None) -> dict[str, int]:
    """Rewrite the questions of the learned answers and the chat names that are not generic yet;
    a rewritten question that the LLM finds already kept joins it. Stops at `deadline` (the
    learning run's time limit): the next run goes on."""
    import time

    from supagent.knowledge.experience import USED, kept_questions, words
    from supagent.models import Conversation, Message, Recipe

    out = {"answers": 0, "chats": 0, "merged": 0}

    def late() -> bool:
        from supagent.knowledge.stopping import check

        check()                                    # an admin stopped the learning run
        if deadline is not None and time.time() > deadline:
            out["stopped"] = "time limit"
            return True
        return False

    for r in (db.session.query(Recipe).filter(Recipe.status.in_(USED),
                                              (Recipe.generic.is_(None)) | (Recipe.generic.is_(False)))
              .order_by(Recipe.id.desc()).limit(limit).all()):
        if late():
            break
        if db.session.get(Recipe, r.id) is None:
            continue                               # joined another one in this run
        kept = kept_questions(r.question or "", r.database_id, exclude=r.id, generic_only=True)
        g = generalize(r.question or "", [], r.query, r.tool, llm=llm, kept=kept)
        if not g["generic"]:
            break                                  # the LLM is not answering: next time
        r.question, r.words, r.generic = g["question"], " ".join(sorted(words(g["question"])))[:2000], True
        twin = db.session.get(Recipe, g["same_as"]) if g.get("same_as") else None
        if twin is not None and twin.status in USED and not (twin.status == r.status == "confirmed"
                                                             and twin.signature != r.signature):
            keep, dup = (r, twin) if r.status == "confirmed" and twin.status != "confirmed" else (twin, r)
            absorb(keep, dup)
            out["merged"] += 1
        db.session.commit()
        out["answers"] += 1
    for conv in db.session.query(Conversation).order_by(Conversation.id.desc()).limit(500).all():
        if out["chats"] >= limit or "stopped" in out or late():
            break
        first = (db.session.query(Message).filter(Message.conversation_id == conv.id, Message.role == "user")
                 .order_by(Message.id).first())
        if first is None or (conv.title or "") != (first.content or "")[:120]:
            continue                               # already named (or renamed)
        g = generalize(first.content or "", [], None, llm=llm)
        if not g["generic"]:
            break
        conv.title = g["title"][:255]
        db.session.commit()
        out["chats"] += 1
    out["merged"] += merge_duplicates()
    if out["answers"] or out["merged"]:
        from supagent.knowledge.index import sync

        sync(("recipe:",))
    return out
