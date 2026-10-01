"""The governed agent (agent.pipeline = governed): the same tools, permissions, trace, stop and progress as
the classic agent, and another way to answer:

  decide  the knowledge the question needs (decider: candidates, gate, one LLM choice, pack)
  plan    one LLM call fills a typed plan (plan); no SQL
  check   code checks it (validate); errors go back once for a second plan
  run     code builds each query (compile) and runs it with execute_sql (the user's permissions)
  answer  the model writes from the figures sheet (compose); its numbers are checked; code adds how it was
          counted

Charts, dashboards, e-mails and exports (actions), open status questions, and a question no valid plan could
be written for go to the classic agent: coverage never drops (the trace says which way it went).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from supagent import settings
from supagent.agent import NUMBERS_NOTE, Agent, Cancelled

PLAN_SECONDS = 180      # one plan call at most (llm.timeout is for long answers): then the classic agent answers
from supagent.llm import add_usage

log = logging.getLogger(__name__)

CLASSIC_KINDS = ("action", "status", "other")
# saved charts and dashboards, files, e-mails, screenshots, "is it normal", "what is happening", "why": the classic
# agent (its tools), whatever the decider's model said
ROUTER_CLASSIC = ("charts", "observability", "incident")        # routes the classic agent's tools answer
CLASSIC_INTENTS = {"charts", "files", "images", "status", "usual", "investigation", "read_charts", "sqllab", "history"}
PLAN_TRIES = 2


class GovernedAgent(Agent):
    """Answers with a checked plan; the classic agent for what a plan cannot say."""

    route_id: int | None = None
    way: str = ""                           # governed | classic: <why>

    def ask(self, question: str, history: list[dict] | None = None) -> tuple[str, list[dict]]:
        from supagent.governed.graph import run

        self.usage = {}
        self.guard.saved = {}
        self.refused = set()
        prev_q, prev_a = _previous(history)
        from supagent.agent import asks_back

        self.replied = bool(prev_a) and asks_back(prev_a)     # the user answers a question back: their choice
        state: dict[str, Any] = {"question": question, "history": history or [], "prev_q": prev_q, "prev_a": prev_a,
                                 "user_id": _user_id(self.username), "trace": []}
        try:
            out = run(self, state)
        except Cancelled:
            raise
        except Exception as ex:  # pylint: disable=broad-except   (a step that fails: the classic agent answers)
            log.warning("supagent governed: a step failed, the classic agent answers", exc_info=True)
            from supagent import router

            router.left(getattr(self, "route_id", None))
            out = {"trace": state["trace"], **self._n_classic({**state, "say": True,
                                                              "why": f"a step failed ({type(ex).__name__})"})}
        return out.get("answer") or "", out["trace"]

    # the nodes (each returns what it adds to the state) and the routes between them (graph) ------------- #
    def _n_decide(self, st: dict[str, Any]) -> dict[str, Any]:
        from supagent.governed import decider

        trace = st["trace"]
        moa = self.route(st["question"], st["prev_q"])            # the router (MOA): once, shared with classic
        add_usage(self.usage, getattr(self, "_router_usage", None) or {})
        self._router_usage = {}
        if moa.route in ROUTER_CLASSIC:                            # tools only the classic agent has
            step = self._begin(trace, "route", {"question": st["question"]})
            self._end(step, trace, "done", json.dumps(moa.as_dict(), default=str))
            return {"why": f"the router: {moa.route}"}
        step = self._begin(trace, "decide", {"question": st["question"]})
        try:
            d = decider.decide(st["question"], previous=st["prev_q"], user_id=st["user_id"], llm=_Counted(self),
                               route_id=getattr(self, "route_id", None))
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent governed: the decider failed", exc_info=True)
            self._end(step, trace, "error", f"the decider failed: {type(ex).__name__}: {ex}")
            return {"why": "the decider failed"}
        self.route_id = d.route_id
        sel = d.selection
        self.given_refs = set(getattr(self, "given_refs", None) or ()) | set(sel.knowledge or ())
        self._end(step, trace, "done", json.dumps({
            "kind": sel.kind, "by": sel.by, "confidence": sel.confidence, "tables": sel.tables(),
            "knowledge": [k.title for k in d.pack.knowledge][:12], "ambiguous": sel.ambiguous,
            "missing": sel.missing, "seconds": d.gathered.seconds}, default=str))
        tools = [k.title for k in d.pack.knowledge if k.kind == "tool"]
        from supagent.agent import intents

        asked = intents(st["question"]) & CLASSIC_INTENTS       # what only the classic agent's tools do, by code
        why = f"a {sel.kind} question" if sel.kind in CLASSIC_KINDS else (
            f"a {sorted(asked)[0]} request" if asked else
            f"the MCP source {tools[0]}" if tools and not sel.tables() else "")
        out: dict[str, Any] = {"decision": d, "why": why}
        if not why and sel.kind == "explain" and d.pack.knowledge:     # the knowledge answers it: no plan needed
            from supagent.governed.plan import Plan

            out["plan"] = Plan(kind="explain", knowledge=[k.ref for k in d.pack.knowledge])
        return out

    def _n_plan(self, st: dict[str, Any], feedback: str = "") -> dict[str, Any]:
        d = st["decision"]
        plan, checked = self._plan(st["question"], d.pack, st["prev_q"], st["prev_a"], st["trace"], feedback=feedback)
        if plan is None:                                           # the classic agent answers: not an example
            from supagent import router

            router.left(getattr(self, "route_id", None))
        return {"plan": plan, "checked": checked,
                "why": "" if plan is not None else "no plan passed the checks", "say": plan is None}

    def _n_run(self, st: dict[str, Any]) -> dict[str, Any]:
        results, failed = self._run(st["plan"], st["decision"].pack, st["trace"])
        return {"results": results, "failed": failed}

    def _n_replan(self, st: dict[str, Any]) -> dict[str, Any]:
        out = self._n_plan(st, feedback="The plan above was run and these steps failed; write the plan again so "
                                        "that each step can run:\n" + "\n".join(st["failed"]))
        out["replanned"] = True
        if out["plan"] is not None and out["plan"].kind != "answer":
            out.update(plan=None, why="the plan's queries failed", say=True)
        return out

    def _n_answer(self, st: dict[str, Any]) -> dict[str, Any]:
        from supagent.governed import compose, gate

        d, plan, results = st["decision"], st["plan"], st["results"]
        figures = compose.sheet(plan, results, d.pack)
        answer = self._write(st["question"], figures, results, st["prev_q"], st["trace"])
        empty = [s for s, content in results.items() if not compose.result_rows(content)[1]]
        answer += compose.notes(st["checked"], empty, plan, d.pack) + compose.interpretation(plan, d.pack, st["checked"])
        gate.used(self.route_id, _read(plan, d.pack))
        self.way = "governed"
        return {"answer": answer}

    def _n_clarify(self, st: dict[str, Any]) -> dict[str, Any]:
        from supagent.governed import compose

        self.way = "governed"
        return {"answer": compose.clarify(st["plan"], st["decision"].pack)}

    def _n_cannot(self, st: dict[str, Any]) -> dict[str, Any]:
        from supagent.governed import compose

        self.way = "governed"
        return {"answer": compose.cannot(st["plan"], st["decision"].pack)}

    def _n_explain(self, st: dict[str, Any]) -> dict[str, Any]:
        self.way = "governed"
        return {"answer": self._explain(st["question"], st["plan"], st["decision"].pack, st["trace"])}

    def _n_classic(self, st: dict[str, Any]) -> dict[str, Any]:
        why = st.get("why") or "no plan"
        self.way = f"classic: {why}"
        trace = st["trace"]
        step = self._begin(trace, "classic", {"why": why})
        self._end(step, trace, "done", why)
        usage = dict(self.usage)
        answer, classic_trace = Agent.ask(self, st["question"], st["history"])
        add_usage(self.usage, usage)
        if st.get("say"):
            answer += f"\n\n_(Answered without a checked plan: {why}.)_"
        trace.extend(classic_trace)
        return {"answer": answer}

    # ------------------------------------------------------------------------------------------ #
    def _plan(self, question: str, pack: Any, prev_q: str, prev_a: str, trace: list[dict],
              feedback: str = "") -> tuple[Any, Any]:
        """Up to PLAN_TRIES LLM calls: a plan that passes the checks (fixed where code can), or (None, None)."""
        from supagent.governed.plan import PLAN_TOOL, parse_plan, plan_messages
        from supagent.governed.validate import check
        from supagent.knowledge.period import _now

        chat = "\n".join(x for x in (prev_q, prev_a) if x)
        now = _now()
        messages = plan_messages(question, pack.text(), f"{now:%A %Y-%m-%d %H:%M}", previous=chat[:1500],
                                 feedback=feedback)
        for attempt in range(PLAN_TRIES):
            self._check_stop()
            step = self._begin(trace, "plan", {"attempt": attempt + 1})
            try:
                from supagent.llm import bounded

                with bounded(self.llm, PLAN_SECONDS):             # a plan is short: no answer in time, classic
                    msg = self.llm.chat(messages, tools=[PLAN_TOOL])
                add_usage(self.usage, self.llm.last_usage)
            except Exception as ex:  # pylint: disable=broad-except
                self._end(step, trace, "error", f"{type(ex).__name__}: {ex}")
                from supagent.agent import BAD_TOOL_CALL

                if BAD_TOOL_CALL.search(str(ex)) and attempt + 1 < PLAN_TRIES:   # the server could not read it
                    messages.append({"role": "user", "content": "(Your last reply could not be read: call "
                                                                "submit_plan again with valid JSON arguments.)"})
                    continue
                return None, None
            args: Any = next(((tc.get("function") or {}).get("arguments") for tc in msg.get("tool_calls") or []
                              if (tc.get("function") or {}).get("name") == "submit_plan"), None)
            if args is None:
                args = msg.get("content") or ""
            plan, err = parse_plan(args)
            if plan is None:
                self._end(step, trace, "error", err)
                messages += [{"role": "assistant", "content": str(args)[:4000]},
                             {"role": "user", "content": f"(Check: {err}. Call submit_plan with a plan that fits.)"}]
                continue
            checked = check(plan, pack, question, chat=chat, today=now.date())
            self._end(step, trace, "done" if checked.ok else "error", json.dumps({
                "errors": checked.errors, "added": checked.added, "fixed": checked.fixed, "warnings": checked.warnings,
                "plan": plan.model_dump(exclude_none=True)}, default=str)[:6000])
            if checked.ok:
                return plan, checked
            messages += [{"role": "assistant", "content": json.dumps(plan.model_dump(), default=str)[:4000]},
                         {"role": "user", "content": "(Check before running: " + "; ".join(checked.errors[:10]) +
                          ". Call submit_plan again with these fixed; never keep a condition nobody gave.)"}]
        return None, None

    def _run(self, plan: Any, pack: Any, trace: list[dict]) -> tuple[dict[str, str], list[str]]:
        """Each step built by code and run with execute_sql; (step id -> result JSON, the steps that failed)."""
        from supagent.governed.compile import CompileError, compile_step

        results: dict[str, str] = {}
        found: dict[str, list[Any]] = {}
        failed: list[str] = []
        for s in plan.steps:
            self._check_stop()
            t = pack.table(s.table)
            try:
                sql = compile_step(s, t, found)
            except CompileError as ex:
                failed.append(f"{s.id}: {ex}")
                continue
            args = {"request": {"database_id": t.database_id, "sql": sql}}
            step = self._begin(trace, "execute_sql", args)
            t0 = time.time()
            called, content = "execute_sql", self._on_dataset(s, t, found, args)
            if content is None:                  # no dataset (or it failed): the query built by code, as execute_sql
                called, content = self._call("execute_sql", args)
            step.update(called=called, seconds=round(time.time() - t0, 1), full=content)
            res = _json(content)
            ok = isinstance(res, dict) and res.get("success") is not False and not res.get("error")
            self._end(step, trace, "done" if ok else "error", content[:4000])
            if not ok:
                failed.append(f"{s.id}: {str((res or {}).get('error') or content)[:600]}")
                continue
            results[s.id] = content
            for r in res.get("rows") or []:
                for col, v in (r or {}).items():
                    found.setdefault(f"{s.id}.{col}", [])
                    if v is not None and v not in found[f"{s.id}.{col}"]:
                        found[f"{s.id}.{col}"].append(v)
        return results, failed

    def _on_dataset(self, s: Any, t: Any, found: dict[str, list[Any]], args: dict) -> str | None:
        """The step through Superset's chart data API when its table has a dataset (governed.use_datasets):
        the dataset's permissions and row-level security apply. None: no dataset, or it failed (logged)."""
        if not settings.get("governed.use_datasets"):
            return None
        from supagent.governed.compile import dataset_for, run_on_dataset

        try:
            ds = dataset_for(t)
            if ds is None:
                return None
            out = run_on_dataset(s, t, ds, found)
        except Exception as ex:  # pylint: disable=broad-except
            from superset import db

            db.session.rollback()
            log.info("supagent governed: the dataset path failed for %s (%s): execute_sql", t.name, ex)
            return None
        args["request"]["sql"] = out.get("sql") or args["request"]["sql"]    # what really ran
        args["request"]["via"] = out.get("via")
        return json.dumps(out, default=str)

    def _write(self, question: str, figures: str, results: dict[str, str], prev_q: str, trace: list[dict]) -> str:
        """The model's sentences from the figures; numbers not in the results: sent back once, then marked."""
        from supagent.governed.compose import answer_messages, strip_answer

        messages = answer_messages(question, figures, prev_q)
        given = [{"role": "user", "content": question + "\n" + figures}] + [
            {"role": "tool", "content": c} for c in results.values()]
        answer = ""
        for attempt in range(2):
            self._check_stop()
            step = self._begin(trace, "answer", {"attempt": attempt + 1})
            try:
                msg = self.llm.chat(messages, tools=None, max_tokens=self._answer_tokens())
                add_usage(self.usage, self.llm.last_usage)
            except Exception as ex:  # pylint: disable=broad-except
                self._end(step, trace, "error", f"{type(ex).__name__}: {ex}")
                return figures
            answer = strip_answer(msg.get("content") or "")
            unknown = self._ungrounded(answer, given)
            self._end(step, trace, "done", json.dumps({"numbers not in the results": unknown}))
            if not unknown:
                return answer
            if attempt == 0:
                messages += [{"role": "assistant", "content": answer},
                             {"role": "user", "content": "(Check: these numbers are not in the figures sheet: "
                                                         + ", ".join(unknown[:12]) + ". Write the answer again with "
                                                         "the sheet's numbers only; never mention this check.)"}]
        return answer + NUMBERS_NOTE.format(numbers=", ".join(self._ungrounded(answer, given)[:12]))

    def _explain(self, question: str, plan: Any, pack: Any, trace: list[dict]) -> str:
        from supagent.governed.compose import explain_messages, strip_answer

        step = self._begin(trace, "explain", {"knowledge": plan.knowledge})
        try:
            msg = self.llm.chat(explain_messages(question, plan, pack), tools=None,
                                max_tokens=self._answer_tokens())
            add_usage(self.usage, self.llm.last_usage)
        except Exception as ex:  # pylint: disable=broad-except
            self._end(step, trace, "error", f"{type(ex).__name__}: {ex}")
            return "\n".join(f"- {k.title}: {k.text}" for k in (pack.item(r) for r in plan.knowledge) if k)
        self._end(step, trace, "done", "")
        from supagent.governed.compose import explained, named

        items = [k for k in (pack.item(r) for r in plan.knowledge) if k is not None]
        return named(explained(strip_answer(msg.get("content") or ""), items), pack)

    # ------------------------------------------------------------------------------------------ #
    def _begin(self, trace: list[dict], tool: str, args: dict) -> dict:
        step = {"tool": tool, "args": args, "status": "running", "started": time.time()}
        trace.append(step)
        self._report(trace)
        return step

    def _end(self, step: dict, trace: list[dict], status: str, result: str) -> None:
        step.update(status=status, result=result, seconds=step.get("seconds") or round(time.time() - step["started"], 1))
        self._report(trace)

    def after_saved(self, message_id: int) -> None:
        """The runner saved the answer: its route knows its message (Helpful then teaches the gate)."""
        from supagent.governed import gate

        if self.route_id:
            try:
                from superset import db

                from supagent.models import Route

                r = db.session.get(Route, self.route_id)
                if r is not None:
                    r.message_id = message_id
                    if getattr(self, "replied", False) and r.used:    # what the user chose, read: it teaches
                        r.signal = "clarified"
                    db.session.commit()
            except Exception:  # pylint: disable=broad-except
                gate.log.warning("supagent governed: route not linked", exc_info=True)


class _Counted:
    """The agent's LLM for the decider's call, its usage counted in the answer's."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None) -> dict:
        msg = self.agent.llm.chat(messages, tools=tools, max_tokens=max_tokens)
        add_usage(self.agent.usage, self.agent.llm.last_usage)
        return msg


def _previous(history: list[dict] | None) -> tuple[str, str]:
    q = next((h.get("content") or "" for h in reversed(history or []) if h.get("role") == "user"), "")
    a = next((h.get("content") or "" for h in reversed(history or []) if h.get("role") == "assistant"), "")
    return q[:1000], re.sub(r"\s+", " ", a)[:1200]


def _user_id(username: str) -> int | None:
    try:
        from superset.extensions import security_manager

        u = security_manager.find_user(username=username)
        return u.id if u else None
    except Exception:  # pylint: disable=broad-except
        return None


def _json(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def _read(plan: Any, pack: Any) -> list[str]:
    return list(dict.fromkeys(pack.table(s.table).subject for s in plan.steps if pack.table(s.table)))


def make_agent(username: str, **kw: Any) -> Agent:
    """The agent of the installation's pipeline (agent.pipeline); classic when the governed one cannot load."""
    from supagent import agent as classic                 # looked up now: the classic agent as it is at this time

    if (settings.get("agent.pipeline") or "classic") == "governed":
        try:
            return GovernedAgent(username, **kw)
        except Exception:  # pylint: disable=broad-except
            log.warning("supagent: the governed pipeline could not start, classic used", exc_info=True)
    return classic.Agent(username, **kw)
