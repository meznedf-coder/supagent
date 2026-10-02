"""The router (MOA): the kind of work a question needs, read by the LLM from the question's meaning, from the
knowledge the question touches and from the routes people confirmed before. The routes are the same for every
deployment; nothing here is about a business domain: what is specific to a deployment comes from its own
knowledge and its own confirmed examples.

  functional      the business meaning or the business figures of the data
  technical       how the systems work: applications, components, jobs, data flows, configuration, what a
                  technical field or metric means
  incident        what went wrong and why over a past period: a failure, a delay, an error spike, a slowdown
  charts          Superset charts and dashboards themselves: find, show, explain, build or change one
  observability   detect issues: is everything normal, anomalies, health, alerts, compared with usual
  infrastructure  servers and services: CPU, memory, disk, network, latency, errors, availability

A route chooses the knowledge given first, the tools offered and a short instruction; the way the answer runs
(classic or governed) stays the deployment's. Not sure: the normal way, as before. The LLM scores every kind
(0-100); the confidence is how far the best is ahead of the next (a model's own "high" says little).

Learning, reliably: an answer people confirmed (Helpful, an admin's confirmation, the reply to a question back)
whose execution followed its route becomes an example. Examples of similar questions are shown to the LLM (how
this team names things); examples of the same question (close enough) are not shown: they only vote, and decide
only when two or more agree, or when an admin set the route itself (Data dictionary, To review: kept or corrected;
an admin confirming the answer judged the answer, not its route). One wrong example never decides alone.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from superset import db

from supagent import settings

log = logging.getLogger(__name__)

ROUTES = ("functional", "technical", "incident", "charts", "observability", "infrastructure")
OTHER = "other"
DEFINITIONS = {
    "functional": "the business meaning or the business figures of the data: what a business term means; amounts, "
                  "counts, rates, rankings or comparisons of business entities and processes",
    "technical": "how the systems work: applications, components, jobs and their dependencies, data flows, "
                 "configuration; what a technical field or metric means (an explanation, not figures)",
    "incident": "what went wrong and why over a past period: explain a failure, a delay, an error spike or a "
                "slowdown, its cause and its impact, across several sources",
    "charts": "Superset charts and dashboards themselves: find, show, open, explain, build or change a chart or a "
              "dashboard. Asking for figures is not charts, even when a chart shows them; asking whether what a "
              "dashboard shows is normal is observability",
    "observability": "detect issues: whether something is normal, healthy or unusual compared with its usual "
                     "levels; anomalies, alerts; is everything all right",
    "infrastructure": "figures or state of servers, hosts, services and their runtime: CPU, memory, disk, network, "
                      "heap, processes killed, latency or response time, timeouts, request errors, availability",
}
# what a route adds to the classic agent: tools (by intent), a short instruction, the knowledge given first
ROUTE_INTENTS = {"charts": {"charts", "read_charts", "sqllab"}, "observability": {"status", "usual"},
                 "incident": {"investigation", "status", "usual", "read_charts"}, "infrastructure": {"status", "usual"}}
ROUTE_NOTES = {
    "incident": "(This question asks what happened and why. Investigate: find what failed, was late or changed, "
                "when and where (the runs and their errors, the servers or services they ran on), follow what "
                "depends on what (the knowledge says it), check the resources of those servers or services at that "
                "time and compare with usual; answer with a short timeline and the cause, each point with the "
                "figure or the record that shows it.)",
    "observability": "(This question asks whether something is abnormal: check the health and the usual levels of "
                     "the period asked, then say what is out of the usual with its figures, or that nothing is.)",
    "infrastructure": "(This question is about servers or services: use their metrics or logs for the period asked "
                      "and name the server or service each figure is about.)",
    "technical": "(This question asks how the systems work: answer from the knowledge (documents, Context, catalog "
                 "notes) and name the source; query the data only if it asks for figures.)",
    "functional": "(This question is about the business meaning or figures of the data: apply the glossary's "
                  "definitions and the team's rules.)",
    "charts": "(This question is about Superset charts or dashboards: find the one it names or asks for with the "
              "chart tools; figures come from the data as usual.)",
}
# knowledge kinds given first for a route (found that many places higher in the search: lower = negative)
ROUTE_KINDS = {"technical": {"context": -4, "doc": -3, "guide": -3}, "functional": {"glossary": -3, "rule": -2},
               "incident": {"guide": -2, "doc": -2, "context": -2}, "infrastructure": {"metric": -2, "guide": -1}}
CONFIDENCE = ("low", "medium", "high")
STRONG = 0.8            # a confirmed example this close (cosine; words: 0.55) is the same question: it only votes
STRONG_WORDS = 0.55
EXAMPLES = 6
ROUTE_SECONDS = 60      # the router's call at most (then: the normal way)
FITS = 50               # the best kind fits less than this (of 100): not sure
MARGIN = (15, 30)       # the best kind's score ahead of the next by this much: medium, high

ROUTE_TOOL = {"type": "function", "function": {
    "name": "route_question",
    "description": "Say which kind of work the question needs.",
    "parameters": {"type": "object", "properties": {
        "scores": {"type": "object", "description": "how well each kind of work fits the question, from 0 (not at "
                                                      "all) to 100 (exactly)",
                   "properties": {r: {"type": "integer", "minimum": 0, "maximum": 100} for r in ROUTES}},
        "route": {"type": "string", "enum": list(ROUTES) + [OTHER], "description": "the kind that fits best"},
        "why": {"type": "string", "description": "a few words"}},
        "required": ["scores", "route"]}}}


@dataclass
class Decision:
    route: str = OTHER                   # the route used (OTHER: the normal way)
    llm: str = OTHER                     # what the LLM said
    second: str = ""
    confidence: str = "low"
    by: str = "fallback"                 # llm | examples | admin example | fallback | off
    why: str = ""
    examples: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.route in ROUTES

    def as_dict(self) -> dict[str, Any]:
        return {"route": self.route, "llm": self.llm, "second": self.second, "confidence": self.confidence,
                "by": self.by, "why": self.why[:200], "scores": self.scores, "examples": [
                    {k: e.get(k) for k in ("route", "similarity", "admin")} for e in self.examples[:EXAMPLES]]}


def enabled() -> bool:
    return bool(settings.get("agent.router"))


# --------------------------------------------------------------------------------------------- #
# the examples: confirmed routes of similar questions
# --------------------------------------------------------------------------------------------- #
def examples(question: str, user_id: int | None = None, k: int = EXAMPLES) -> list[dict[str, Any]]:
    """The routes people confirmed for the questions most like this one (the store: by meaning; else by words):
    [{question, route, similarity, admin}], closest first."""
    out: list[dict[str, Any]] = []
    try:
        from supagent.knowledge import pgstore

        if pgstore.active():
            out = pgstore.route_examples(question, k=k)
    except Exception:  # pylint: disable=broad-except
        log.warning("supagent router: examples from the store", exc_info=True)
    if out:
        return out
    return _examples_by_words(question, k)


def _examples_by_words(question: str, k: int) -> list[dict[str, Any]]:
    from supagent.governed.gate import CONFIRMED
    from supagent.knowledge.resolve import terms
    from supagent.models import Route

    asked = set(terms(question))
    if not asked:
        return []
    scored = []
    try:
        for r in (db.session.query(Route).filter(Route.signal.in_(CONFIRMED), Route.moa.isnot(None),
                                                 Route.moa_followed.is_(True))
                  .order_by(Route.id.desc()).limit(3000)):
            words = set((r.terms or "").split())
            if not words:
                continue
            sim = len(asked & words) / len(asked | words)
            if sim > 0.2:
                scored.append({"question": (r.question or "")[:240], "route": r.moa, "similarity": round(sim, 3),
                               "admin": r.moa_by == "admin", "words": True})
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        log.warning("supagent router: examples by words", exc_info=True)
    return sorted(scored, key=lambda x: -x["similarity"])[:k]


# --------------------------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------------------------- #
ROUTER_SYSTEM = ("You route a question about the data of this platform to the kind of work it needs. The kinds:\n" +
                 "\n".join(f"- {r}: {d}" for r, d in DEFINITIONS.items()) +
                 f"\n- {OTHER}: none of these, or a greeting\n"
                 "Read what the person wants to get, not only the words. The knowledge found for the question says "
                 "what the platform has (a chart or a dashboard listed there does not make it a charts question). "
                 "The team's questions routed before show how this team names things; they are similar questions, "
                 "not this one: decide from the kinds above. Score how well each kind fits (0 to 100; two kinds may "
                 "both fit), then give the best one as route. Call route_question once.")


def messages(question: str, previous: str, found: list[dict[str, Any]], shown: list[dict[str, Any]]) -> list[dict]:
    lines = []
    if found:
        lines.append("Knowledge found for the question (kind: title):")
        lines += [f"- {f['kind']}: {str(f['title'])[:110]}" + (f" [{f['facets']}]" if f.get("facets") else "")
                  for f in found[:8]]
    if shown:
        lines.append("Similar questions of the team and the kind of work each needed:")
        lines += [f"- {e['route']}: {e['question'][:200]}" for e in shown[:EXAMPLES]]
    if previous:
        lines.append(f"(The chat before: {previous[:500]})")
    lines.append(f"Question: {question}")
    return [{"role": "system", "content": ROUTER_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse(msg: dict[str, Any]) -> tuple[str, str, str, str, dict[str, int]]:
    """(route, second, confidence, why, scores). With scores, the route is the best scored kind and the confidence
    how far ahead of the next it is (a model's own "high" says little); without, the model's words."""
    args: Any = None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "route_question":
            args = (tc.get("function") or {}).get("arguments")
            break
    if args is None:                              # a model that wrote the JSON as text
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
        m = re.search(r"\{.*\}", text, re.S)
        args = m.group(0) if m else "{}"
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    route = str(args.get("route") or OTHER).strip().lower()
    route = route if route in ROUTES else OTHER
    second = str(args.get("second") or "").strip().lower()
    second = second if second in ROUTES else ""
    conf = str(args.get("confidence") or "low").strip().lower()
    conf = conf if conf in CONFIDENCE else "low"
    why = str(args.get("why") or "")[:300]
    raw = args.get("scores") if isinstance(args.get("scores"), dict) else {}
    scores: dict[str, int] = {}
    for r in ROUTES:
        try:
            scores[r] = max(0, min(100, int(float(raw.get(r, 0)))))
        except (TypeError, ValueError):
            scores[r] = 0
    if not any(scores.values()):
        return route, second, conf, why, {}
    ranked = sorted(ROUTES, key=lambda r: -scores[r])
    best, nxt = ranked[0], ranked[1]
    margin = scores[best] - scores[nxt]
    if route not in (best, OTHER) and scores[route] < scores[best]:
        conf = "low"                              # its route is not its best scored kind: it hesitates
    elif scores[best] < FITS or margin < MARGIN[0]:
        conf = "low"
    else:
        conf = "high" if margin >= MARGIN[1] else "medium"
    return (best if route != OTHER else OTHER), nxt if scores[nxt] >= FITS else "", conf, why, scores


def decide(question: str, previous: str = "", user_id: int | None = None, llm: Any = None,
           shown: list[dict[str, Any]] | None = None, found: list[dict[str, Any]] | None = None) -> Decision:
    """The route of a question. `shown` (confirmed examples) and `found` (knowledge) are looked up when not
    given (the lab's evaluation gives them)."""
    if not enabled():
        return Decision(by="off")
    if shown is None:
        shown = examples(f"{previous}\n{question}" if previous else question, user_id)
    if found is None:
        found = _found(question)
    d = Decision(examples=list(shown or []))
    strong = [e for e in d.examples if e.get("similarity", 0) >= (STRONG_WORDS if e.get("words") else STRONG)]
    similar = [e for e in d.examples if e not in strong]      # the LLM sees these; the same questions only vote
    if llm is not None:
        try:
            from supagent.llm import bounded

            with bounded(llm, ROUTE_SECONDS):                  # before every answer: quick, or the normal way
                msg = llm.chat(messages(question, previous, found, similar), tools=[ROUTE_TOOL], max_tokens=400)
            d.llm, d.second, d.confidence, d.why, d.scores = parse(msg)
        except Exception as ex:  # pylint: disable=broad-except   (the normal way)
            log.warning("supagent router: the LLM did not route: %s", str(ex)[:200])
    admin = next((e for e in strong if e.get("admin")), None)
    votes = Counter(e["route"] for e in strong if e.get("route") in ROUTES)
    top, n = votes.most_common(1)[0] if votes else (None, 0)
    floor = CONFIDENCE.index(settings.get("router.min_confidence") or "medium")
    if admin is not None and admin["route"] in ROUTES:
        d.route, d.by = admin["route"], "admin example"
    elif top and n >= 2 and len(votes) == 1 and top != d.llm:
        d.route, d.by = top, "examples"               # several close confirmed questions agree, against the LLM
    elif d.llm in ROUTES and CONFIDENCE.index(d.confidence) >= floor:
        d.route, d.by = d.llm, "llm"
    else:
        d.route, d.by = OTHER, "fallback"
    return d


def _found(question: str) -> list[dict[str, Any]]:
    """The knowledge that looks relevant (kind, title, the item's categories when classified)."""
    try:
        from supagent.knowledge.search import search

        pieces = search(question, k=8, rerank=False)          # quick: the router is before every answer
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
        return []
    out = []
    try:
        from supagent.knowledge.facets import facets_of

        tags = facets_of([p["ref"] for p in pieces])
    except Exception:  # pylint: disable=broad-except
        tags = {}
    for p in pieces:
        out.append({"kind": p["kind"], "title": p["title"], "ref": p["ref"],
                    "facets": ", ".join(tags.get(p["ref"], []))})
    return out


# --------------------------------------------------------------------------------------------- #
# recording: the decision on the answer's route row (the learning reads it)
# --------------------------------------------------------------------------------------------- #
def record(route_id: int | None, question: str, d: Decision, user_id: int | None = None) -> int | None:
    """The decision on the route row of this answer (made here when the decider made none); its id."""
    from supagent.knowledge.resolve import terms
    from supagent.models import Route

    try:
        r = db.session.get(Route, route_id) if route_id else None
        if r is None:
            r = Route(question=(question or "")[:2000], terms=" ".join(terms(question))[:2000], shown=[], chosen=[],
                      used=[], user_id=user_id)
            db.session.add(r)
        r.moa, r.moa_by, r.moa_confidence = d.route, d.by, d.confidence
        r.moa_followed = d.active                     # set to False when the execution leaves the route
        r.moa_detail = d.as_dict()
        db.session.commit()
        return r.id
    except Exception:  # pylint: disable=broad-except   (an answer never fails for its route)
        db.session.rollback()
        log.warning("supagent router: not recorded", exc_info=True)
        return None


def left(route_id: int | None) -> None:
    """The execution did not follow the route (a fallback): it never becomes an example."""
    if not route_id:
        return
    from supagent.models import Route

    try:
        r = db.session.get(Route, route_id)
        if r is not None:
            r.moa_followed = False
            db.session.commit()
    except Exception:  # pylint: disable=broad-except
        db.session.rollback()
