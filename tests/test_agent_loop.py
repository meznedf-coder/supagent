"""The agent's loop with a scripted LLM: an identical call is not run twice, an empty LLM answer
is asked again and then falls back to the last result, an answer that announces a step without
taking it is sent back once, a claim that all is well while a check could not run is corrected,
and what the LLM calls took is counted. Also the LLM client (empty answers asked again, usage)
and the background LLM work that waits while answers are being computed."""

from __future__ import annotations

import itertools
import json
import types

import pytest

from test_stop import FakeAgent, _running

_IDS = itertools.count(1)


def call(name: str, args: dict | None = None) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{next(_IDS)}", "function": {"name": name, "arguments": json.dumps(args or {})}}]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


class ScriptedLLM:
    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.last_usage = None

    def chat(self, messages, tools=None, max_tokens=None):
        self.seen.append([dict(m) for m in messages])
        self.caps = [*getattr(self, "caps", []), max_tokens]
        reply = self.replies.pop(0)
        self.last_usage = {"calls": 1, "seconds": 2.0, "prompt_tokens": 1000, "completion_tokens": 20,
                           "cached_tokens": 800}
        if isinstance(reply, Exception):
            raise reply
        return reply


SQL_ROWS = json.dumps({"success": True, "columns": [{"name": "APP"}, {"name": "n"}],
                       "rows": [{"APP": "A", "n": 3}, {"APP": "B", "n": 2}], "row_count": 2})


def agent_with(monkeypatch, replies: list, results=None, rich: bool = True):
    from supagent.agent import Agent, ChartGuard

    a = object.__new__(Agent)
    a.username, a.on_step, a.should_stop, a.rich, a.max_steps = "alice", None, None, rich, 10
    a.llm = ScriptedLLM(replies)
    a.names = {"execute_sql", "check_health", "generate_chart", "list_charts", "promql_query"}
    a.local = {}
    a.superset = types.SimpleNamespace(schema=lambda name: {}, available=True, error=None)
    a.guard = ChartGuard(a)
    ran: list[tuple[str, dict]] = []

    def fake_call(self, name, args):
        ran.append((name, args))
        return name, (results(name, args) if results else SQL_ROWS)

    monkeypatch.setattr(Agent, "_system", lambda self, q, shown=None: "SYSTEM")
    monkeypatch.setattr(Agent, "_question_blocks", lambda self, q, shown=None: "")
    monkeypatch.setattr(Agent, "_specs_for", lambda self, q: [])
    monkeypatch.setattr(Agent, "_call", fake_call)
    return a, ran


def tool_messages(llm: ScriptedLLM) -> list[str]:
    return [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]


def test_an_identical_call_is_not_run_twice(ctx, monkeypatch):
    from supagent.agent import REPEAT_NOTE

    sql = {"request": {"database_id": 1, "sql": "SELECT APP, COUNT(*) n FROM jobs GROUP BY 1"}}
    a, ran = agent_with(monkeypatch, [call("execute_sql", sql), call("execute_sql", sql), say("A: 3, B: 2.")])
    answer, trace = a.ask("How many jobs per application?")
    assert answer.startswith("A: 3, B: 2.") and len(ran) == 1
    assert tool_messages(a.llm)[-1] == REPEAT_NOTE
    assert [t["status"] for t in trace] == ["done", "done"] and "full" in trace[0] and "full" not in trace[1]


def test_reading_again_after_a_save_is_not_a_repeat(ctx, monkeypatch):
    replies = [call("list_charts"), call("generate_chart", {"request": {"dataset_id": 1, "save_chart": True}}),
               call("list_charts"), say("Saved.")]
    a, ran = agent_with(monkeypatch, replies, results=lambda n, args: json.dumps({"success": True, "charts": []}))
    a.ask("Save a chart")
    assert [n for n, _ in ran] == ["list_charts", "generate_chart", "list_charts"]


def test_a_failed_call_is_still_refused_when_repeated(ctx, monkeypatch):
    sql = {"request": {"database_id": 1, "sql": "SELECT nope"}}
    a, ran = agent_with(monkeypatch, [call("execute_sql", sql), call("execute_sql", sql), say("It failed.")],
                        results=lambda n, args: json.dumps({"success": False, "error": "no column nope"}))
    a.ask("x")
    assert len(ran) == 1 and "already made exactly this call and it failed" in tool_messages(a.llm)[-1]


@pytest.mark.parametrize("rich", [True, False])
def test_an_empty_llm_answer_falls_back_to_the_last_result(ctx, monkeypatch, rich):
    from supagent.agent import EMPTY_PLAIN, EMPTY_RICH
    from supagent.llm import EmptyAnswer

    sql = {"request": {"database_id": 1, "sql": "SELECT APP, n FROM t"}}
    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), EmptyAnswer("empty")], rich=rich)
    answer, trace = a.ask("Jobs per application")
    if rich:
        assert answer == EMPTY_RICH and trace[0]["full"]              # the page shows the rows
    else:
        assert answer.startswith(EMPTY_PLAIN) and "| APP | n |" in answer and "| A | 3 |" in answer
    assert a.usage["calls"] == 2


def test_an_empty_llm_answer_without_any_result_still_fails(ctx, monkeypatch):
    from supagent.llm import EmptyAnswer

    a, _ran = agent_with(monkeypatch, [EmptyAnswer("empty")])
    with pytest.raises(EmptyAnswer):
        a.ask("Hello?")


@pytest.mark.parametrize("text", [
    "I found the metric. Let me run the query now.",
    "The index is batch-jobs. I will now query it for yesterday.",
    "J'ai trouvé la métrique. Je vais lancer la requête.",
    "Next, I'll create the chart in Superset:",
    "I'll do this in three steps:\n1. Update chart 277 to show 23 September\n2. Add it to the dashboard\n"
    "3. E-mail a screenshot of the dashboard",                          # a plan and nothing done (lab)
])
def test_an_announced_step_is_sent_back_once(ctx, monkeypatch, text):
    from supagent.agent import announces_action

    assert announces_action(text)
    first = {"request": {"database_id": 1, "sql": "SELECT DISTINCT APP FROM jobs"}}
    sql = {"request": {"database_id": 1, "sql": "SELECT COUNT(*) FROM jobs"}}
    a, ran = agent_with(monkeypatch, [call("execute_sql", first), say(text), call("execute_sql", sql),
                                      say("There are 5.")])
    answer, _trace = a.ask("How many?")
    assert answer.startswith("There are 5.") and len(ran) == 2
    nudges = [m["content"] for m in a.llm.seen[-1] if m["role"] == "user" and "announcing a step" in m["content"]]
    assert len(nudges) == 1 and a.usage["nudges"] == 1                  # counted (superset supagent stats)


@pytest.mark.parametrize("text", [
    "There are 5 failed jobs. Let me know if you want a chart.",
    "5 jobs failed. If you want, I can create a chart of them.",
    "5 jobs failed. Shall I create a chart?",
    "5 jobs failed. Si vous voulez, je vais créer un graphique.",
    "The query I ran counts the failed jobs: 5.",
    "Here is what I did:\n1. Updated chart 277 to 23 September\n2. Added it to the dashboard",
    "Two applications failed:\n- BILLING: 12\n- PAYROLL: 4",
])
def test_offers_and_plain_answers_are_not_announcements(text):
    from supagent.agent import announces_action

    assert announces_action(text) is None


def test_the_announcement_nudge_is_given_once_only(ctx, monkeypatch):
    sql = {"request": {"database_id": 1, "sql": "SELECT 1"}}
    a, _ran = agent_with(monkeypatch, [call("execute_sql", sql), say("Let me run the query."),
                                       say("Let me run the query.")])
    answer, _trace = a.ask("Which applications failed?")
    assert answer.startswith("Let me run the query.")                # not sent back twice


def _health(errors: dict) -> str:
    return json.dumps({"breaches": [], "breach_count": 0, "errors": errors, "note": "no breach"})


@pytest.mark.parametrize("answer, noted", [
    ("No breach was found between 02:00 and 06:00: the servers were healthy.", True),
    ("Aucun problème détecté sur les serveurs.", True),
    ("The CPU check could not run, so I cannot say whether the servers are healthy.", False),
    ("Memory was high on srv-2 from 03:00 to 04:10.", False),
])
def test_all_clear_while_a_check_could_not_run_is_corrected(ctx, monkeypatch, answer, noted):
    a, _ran = agent_with(monkeypatch, [call("check_health", {"start": "a", "end": "b"}), say(answer)],
                         results=lambda n, args: _health({"cpu_saturation": "timeout"}))
    out, _trace = a.ask("Were the servers saturated (srv-2 too)?")
    assert ("check cpu_saturation could not run" in out) is noted


def test_no_correction_when_everything_ran_or_a_failure_was_fixed():
    from supagent.agent import honesty_note

    ok = [{"tool": "check_health", "status": "done", "result": _health({})}]
    fixed = [{"tool": "execute_sql", "status": "error", "result": "{}"}, {"tool": "execute_sql", "status": "done",
                                                                           "result": SQL_ROWS}]
    failed = fixed[:1]
    assert honesty_note("No errors were found.", ok) == "" and honesty_note("No errors were found.", fixed) == ""
    assert "execute_sql could not run" in honesty_note("No errors were found.", failed)


def test_the_llm_time_and_tokens_of_an_answer_are_counted(ctx, monkeypatch):
    a, _ran = agent_with(monkeypatch, [call("execute_sql", {"sql": "SELECT 1"}), say("1.")])
    a.ask("x")
    assert a.usage == {"calls": 2, "seconds": 4.0, "prompt_tokens": 2000, "completion_tokens": 40,
                       "cached_tokens": 1600}


def test_what_an_answer_took_is_recorded(app, monkeypatch):
    from superset.extensions import db

    from supagent import runner
    from supagent.knowledge import experience, generic
    from supagent.models import Usage

    class Counted(FakeAgent):
        stop = False

        def ask(self, question, history=None):
            self.usage = {"calls": 3, "seconds": 12.5, "prompt_tokens": 9000, "completion_tokens": 300,
                          "cached_tokens": 6000}
            return super().ask(question, history)

    with app.app_context():
        _cid, mid = _running()
        monkeypatch.setattr("supagent.agent.Agent", type("Agent", (Counted,), {"message_id": mid}))
        monkeypatch.setattr(generic, "generalize", lambda *a, **k: {})
        monkeypatch.setattr(experience, "learn_from_answer", lambda *a, **k: None)
        runner.run_answer(mid)
        u = db.session.get(Usage, mid)
        assert (u.llm_calls, u.llm_seconds, u.prompt_tokens, u.cached_tokens) == (3, 12.5, 9000, 6000)
        assert (u.tool_calls, u.tool_seconds, u.failed_calls) == (1, 0.2, 0) and u.seconds >= 0


class _Response:
    def __init__(self, body: dict) -> None:
        self.body, self.text = body, json.dumps(body)

    def json(self):
        return self.body


def _client(monkeypatch, bodies: list[dict]):
    from supagent import llm as L

    client = L.LLM(L.LLMConfig(base_url="http://llm.invalid/v1", model="m"))
    sent = []
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: sent.append(kw) or _Response(bodies.pop(0)))
    return client, sent


def _reply(content: str = "", tool_calls=None, usage=None, timings=None) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content, **({"tool_calls": tool_calls}
                                                                                  if tool_calls else {})}}],
            "usage": usage or {"prompt_tokens": 500, "completion_tokens": 5}, **({"timings": timings} if timings else {})}


def test_an_empty_llm_answer_is_asked_again(monkeypatch):
    from supagent import llm as L

    client, sent = _client(monkeypatch, [_reply(""), _reply("<think>hmm</think> "), _reply("Hello")])
    assert client.chat([{"role": "user", "content": "hi"}])["content"] == "Hello" and len(sent) == 3
    assert client.last_usage["calls"] == 3
    client, sent = _client(monkeypatch, [_reply("")] * (L.EMPTY_RETRIES + 1))
    with pytest.raises(L.EmptyAnswer):
        client.chat([{"role": "user", "content": "hi"}])


def test_the_prompt_cache_share_is_read_from_llama_cpp_and_openai_answers(monkeypatch):
    client, _sent = _client(monkeypatch, [
        _reply("a", usage={"prompt_tokens": 5000, "completion_tokens": 9}, timings={"prompt_n": 1200}),
        _reply("b", usage={"prompt_tokens": 4000, "completion_tokens": 3,
                           "prompt_tokens_details": {"cached_tokens": 3500}})])
    client.chat([{"role": "user", "content": "x"}])
    assert (client.last_usage["prompt_tokens"], client.last_usage["cached_tokens"]) == (5000, 3800)
    client.chat([{"role": "user", "content": "y"}])
    assert client.last_usage["cached_tokens"] == 3500


def test_background_llm_calls_wait_for_the_answers(monkeypatch):
    from supagent import llm as L, priority

    waited = []
    monkeypatch.setattr(priority, "wait_for_answers", lambda: waited.append(1) or 0.0)
    client, _sent = _client(monkeypatch, [_reply("a"), _reply("b")])
    client.chat([{"role": "user", "content": "x"}])
    with L.background():
        client.chat([{"role": "user", "content": "y"}])
    assert waited == [1]


def test_only_answers_being_computed_are_waited_for(app):
    import datetime as dt
    import time

    from superset.extensions import db

    from supagent import priority
    from supagent.models import Message

    with app.app_context():
        _cid, mid = _running()
        assert priority.answers_running() == 0                        # queued (pending): not waited for
        m = db.session.get(Message, mid)
        m.status = "running"
        db.session.commit()
        assert priority.answers_running() == 1
        t0 = time.time()
        assert priority.wait_for_answers(max_wait=0.3, poll=0.1) >= 0.3 and time.time() - t0 < 2
        m.updated_at = dt.datetime.utcnow() - dt.timedelta(seconds=priority.STALE_S + 60)
        db.session.commit()
        assert priority.answers_running() == 0                        # no progress for long: a dead worker
        m.status = "done"
        db.session.commit()


def test_the_llm_profile_says_what_the_server_does(monkeypatch):
    """superset supagent test-llm --profile: thinking, tool calls and the prompt cache."""
    tool_call = [{"id": "1", "function": {"name": "ticket_status", "arguments": "{\"number\": \"4711\"}"}}]
    bodies = [_reply("OK"), _reply("<think>x</think>OK", usage={"prompt_tokens": 20, "completion_tokens": 300}),
              _reply("", tool_calls=tool_call),
              _reply("t7", usage={"prompt_tokens": 3000, "completion_tokens": 1}, timings={"prompt_n": 3000}),
              _reply("c12", usage={"prompt_tokens": 3000, "completion_tokens": 1}, timings={"prompt_n": 12})]
    client, sent = _client(monkeypatch, bodies)
    out = client.profile()
    assert out["thinking"]["answered"] and out["tool_calls"]["works"] and out["prompt_cache"]["works"]
    assert out["prompt_cache"]["second"]["cached_tokens"] == 2988 and len(sent) == 5


def test_an_unreadable_tool_call_is_sent_back_not_an_error(ctx, monkeypatch):
    from supagent.llm import LLMError

    bad = LLMError('LLM x/chat/completions: HTTP 500: {"error":{"message":"Failed to parse tool call arguments as '
                   'JSON: parse error at line 1, column 53381"}}')
    a, ran = agent_with(monkeypatch, [call("execute_sql", {"sql": "SELECT 1"}), bad, say("Done: 2 charts saved.")])
    answer, _trace = a.ask("Make me a dashboard")
    assert answer.startswith("Done: 2 charts saved.") and len(ran) == 1
    assert "could not be read" in a.llm.seen[-1][-1]["content"]
    b, _ran = agent_with(monkeypatch, [call("execute_sql", {"sql": "SELECT 1"}), bad, bad, bad,
                                       say("One query ran; the charts are still to do.")])
    answer, _trace = b.ask("Make me a dashboard")
    assert answer.startswith("One query ran; the charts are still to do.")          # what was done, not an error


def test_a_nested_aggregate_on_metrics_gets_the_way_to_write_it():
    from supagent.tools_superset import NESTED_AGG

    assert NESTED_AGG.search("PushdownError: AVG(100 * SUM(rate) FILTER(WHERE mode <> 'idle') / SUM(rate)): cannot")
    assert not NESTED_AGG.search("PushdownError: WINDOW functions are not supported")


def test_a_call_written_as_text_after_the_last_call_is_not_the_answer(ctx, monkeypatch):
    calls = [call("execute_sql", {"request": {"database_id": 1, "sql": f"SELECT {i}"}}) for i in range(10)]
    a, _ran = agent_with(monkeypatch, calls + [say('<tool_call>\n<function=execute_sql>\n<parameter=request>\n'
                                                   '{"database_id": 1}\n</parameter>\n</function>\n</tool_call>')])
    answer, _trace = a.ask("Which applications failed?")
    assert "<tool_call>" not in answer and "<function=" not in answer
    assert answer.endswith("(Stopped: the tool calls of one answer were used up; ask a narrower question.)")


def test_two_calls_before_the_end_the_model_is_told_to_answer(ctx, monkeypatch):
    """An investigation that used its calls up wrote nothing (the retail lab's X2): two calls before the end, the
    model is told to answer from what it found."""
    from supagent.agent import LAST_CALLS

    sql = [{"request": {"database_id": 1, "sql": f"SELECT {i} AS n"}} for i in range(9)]
    a, ran = agent_with(monkeypatch, [call("execute_sql", q) for q in sql] + [say("Found: 8 checks, nothing more.")])
    answer, _trace = a.ask("Why did the jobs fail?")
    told = [m for m in a.llm.seen[-1] if m["role"] == "user" and m["content"] == LAST_CALLS]
    assert len(told) == 1 and len(ran) == 9
