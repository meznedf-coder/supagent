"""Celery tasks, registered on Superset's own Celery app (its workers and beat load them with
the app): answering a question, learning, and the hourly tick that starts the daily learning
at learn.hour. Without a worker, questions are answered in a thread of the web server.

A question is answered once, by one process: the worker or web server that starts it first
takes it (runner.run_answer: pending -> running), the others leave it. In agent.executor = auto
a question goes to the workers while one is alive (supagent.workers: their heartbeats), and the
web server that received it answers it itself if no worker started it within TAKEOVER_S seconds
(every worker busy, or one stopped since its last heartbeat)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from celery.schedules import crontab
from superset.extensions import celery_app

log = logging.getLogger(__name__)
_PING: dict[str, Any] = {"at": 0.0, "ok": False}
BEAT_KEY = "supagent-learn-tick"
TAKEOVER_S = 15.0              # auto: a question no worker started in this time is answered here


@celery_app.task(name="supagent.answer", soft_time_limit=1800, time_limit=1900, ignore_result=True,
                 acks_late=True)          # a busy worker leaves the next questions to the other workers
def answer_task(message_id: int) -> None:
    from supagent.runner import run_answer

    if not run_answer(message_id):
        log.info("supagent: answer %s already taken (another worker, a web server) or stopped", message_id)


@celery_app.task(name="supagent.learn", soft_time_limit=4 * 3600, time_limit=4 * 3600 + 300)
def learn_task(reason: str = "manual", databases: list[str] | None = None) -> dict:
    from supagent.knowledge.learner import run_learning

    return run_learning(reason=reason, databases=databases)


@celery_app.task(name="supagent.context", soft_time_limit=2 * 3600, time_limit=2 * 3600 + 300)
def context_task(reason: str = "manual") -> dict:
    from superset import db

    from supagent.knowledge.context import build_context

    try:
        return build_context(reason=reason)
    finally:
        db.session.remove()


@celery_app.task(name="supagent.remember", ignore_result=True, soft_time_limit=600, time_limit=660)
def remember_task(message_id: int) -> None:
    from superset import db

    from supagent.knowledge.memory import learn_from_message
    from supagent.llm import background

    try:
        with background():
            learn_from_message(message_id)
    finally:
        db.session.remove()


@celery_app.task(name="supagent.helpful", ignore_result=True, soft_time_limit=600, time_limit=660)
def helpful_task(message_id: int) -> None:
    from superset import db

    try:
        learn_helpful(message_id)
    finally:
        db.session.remove()


def learn_helpful(message_id: int) -> None:
    """An answer marked Helpful -> a learned answer (the LLM writes its generic question), then
    the formulas that it may confirm (agent catalog)."""
    from supagent.knowledge.autocatalog import run as agent_catalog
    from supagent.knowledge.experience import learn_from_helpful
    from supagent.knowledge.index import embed_few, sync
    from supagent.llm import background

    with background():                     # the people waiting for an answer first
        if learn_from_helpful(message_id) is not None:
            embed_few(sync(("recipe:",)))    # found by the next similar question, by meaning too
            agent_catalog(llm_docs=False, parts=("formulas",))


@celery_app.task(name="supagent.catalog", ignore_result=True, soft_time_limit=600, time_limit=660)
def catalog_task() -> None:
    from superset import db

    from supagent.knowledge.autocatalog import run

    try:
        run(llm_docs=False, parts=("formulas",))
    finally:
        db.session.remove()


@celery_app.task(name="supagent.doc", ignore_result=True, soft_time_limit=1800, time_limit=1900)
def doc_task(doc_id: int) -> None:
    from superset import db

    from supagent.knowledge.docs import refresh
    from supagent.knowledge.index import index_knowledge
    from supagent.models import Doc

    from supagent.llm import background

    try:
        d = db.session.get(Doc, doc_id)
        if d is not None:
            with background():
                refresh(d)
                index_knowledge()
    finally:
        db.session.remove()


@celery_app.task(name="supagent.learn_tick", ignore_result=True)
def learn_tick() -> None:
    """Every hour: the daily learning when due, and old files removed."""
    from superset import db

    from supagent.knowledge.learner import due_today, run_learning
    from supagent.runner import purge_old_chats, purge_old_files
    from supagent.workers import purge

    try:
        purge_old_files()
        purge_old_chats()
        try:
            purge()                          # delivered queue messages, heartbeats of gone workers
        except Exception:  # pylint: disable=broad-except   (the learning goes on)
            log.exception("supagent: clean-up of the queue")
        try:
            from supagent import settings
            from supagent.knowledge.ranking import purge as purge_uses

            purge_uses()                     # the uses of the learned items older than a year and a month
            from supagent.knowledge.usage import purge as purge_calls

            purge_calls(int(settings.get("usage.keep_days") or 0))      # the LLM usage page's old calls
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            log.exception("supagent: clean-up of the LLM calls")
        if due_today():
            run_learning(reason="schedule")
        else:
            from supagent.knowledge.docs import refresh_due
            from supagent.knowledge.index import index_knowledge

            refresh_due()
            index_knowledge()
        try:                                     # the decider's gate from the answers people confirmed
            from supagent.governed.gate import learn

            learn()
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            log.exception("supagent: the decider's gate was not learned")
        try:                                     # the knowledge store: chats, routes, names, new vectors
            from supagent.knowledge.pgstore import maintain

            maintain()
        except Exception:  # pylint: disable=broad-except
            db.session.rollback()
            log.exception("supagent: the knowledge store was not kept in step")
        from supagent.knowledge.context import build_context, context_due

        if context_due():                    # the nightly Context, after the day's learning
            build_context(reason="schedule")
    finally:
        db.session.remove()


def add_beat_schedule() -> None:
    """Add the hourly tick to Superset's beat schedule (kept with what the config defines)."""
    schedule = dict(celery_app.conf.beat_schedule or {})
    schedule[BEAT_KEY] = {"task": "supagent.learn_tick", "schedule": crontab(minute=7)}
    celery_app.conf.beat_schedule = schedule


def workers_alive() -> bool:
    """A Celery worker reads the answers' queue: its heartbeat (any broker), or, with a broker
    that carries broadcasts (Redis, RabbitMQ), a worker that answered a ping in the last minute."""
    from supagent.workers import can_broadcast, live_workers

    if live_workers():
        return True
    if not can_broadcast():                  # a database queue: no ping
        return False
    if time.time() - _PING["at"] < 60:
        return _PING["ok"]
    found: list[bool] = []

    def ping() -> None:
        try:
            found.append(bool(celery_app.control.ping(timeout=1.0)))
        except Exception as ex:  # pylint: disable=broad-except
            log.info("supagent: no Celery worker (%s)", ex)

    t = threading.Thread(target=ping, name="supagent-ping", daemon=True)
    t.start()
    t.join(3.0)                  # an unreachable broker must not hold the question
    ok = bool(found and found[0])
    _PING.update(at=time.time(), ok=ok)
    return ok


def _queue() -> dict[str, str]:
    from supagent import settings

    queue = (settings.get("agent.celery_queue") or "").strip()
    return {"queue": queue} if queue else {}


def _in_thread(fn: Any, *args: Any, background: bool = False, delay: float = 0.0) -> None:
    from flask import current_app

    app = current_app._get_current_object()

    def run() -> None:
        from supagent.llm import background as llm_background

        with app.app_context():
            if background:                 # its LLM calls wait while answers are computed
                with llm_background():
                    fn(*args)
            else:
                fn(*args)

    t = threading.Timer(delay, run) if delay else threading.Thread(target=run)
    t.name, t.daemon = f"supagent-{fn.__name__}", True
    t.start()


def dispatch_answer(message_id: int) -> str:
    """Start answering; returns where: celery or thread."""
    from supagent import settings
    from supagent.runner import run_answer

    mode = settings.get("agent.executor")
    if mode == "celery" or (mode == "auto" and workers_alive()):
        from superset import db

        from supagent.models import Message

        try:
            res = answer_task.apply_async(args=[message_id], **_queue())
        except Exception as ex:  # pylint: disable=broad-except  (broker down: answer here)
            log.warning("supagent: Celery refused the question (%s), answering in the web server", ex)
            _PING.update(at=time.time(), ok=False)
        else:
            msg = db.session.get(Message, message_id)
            if msg is not None:
                msg.task_id = res.id
                db.session.commit()
            if mode == "auto":                # no worker started it in time: this web server answers
                _in_thread(_take_over, message_id, delay=TAKEOVER_S)
            return "celery"
    _in_thread(run_answer, message_id)
    return "thread"


def _take_over(message_id: int) -> None:
    from supagent.runner import run_answer

    run_answer(message_id, taking_over=True)


def dispatch_learning(reason: str = "manual", databases: list[str] | None = None) -> str:
    from supagent import settings
    from supagent.knowledge.learner import run_learning

    mode = settings.get("agent.executor")
    if mode == "celery" or (mode == "auto" and workers_alive()):
        try:
            learn_task.apply_async(args=[reason, databases], **_queue())
            return "celery"
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent: Celery refused the learning run (%s), running it in the web server", ex)
    _in_thread(lambda: run_learning(reason=reason, databases=databases))
    return "thread"


def dispatch_context(reason: str = "manual") -> str:
    from supagent import settings
    from supagent.knowledge.context import build_context

    mode = settings.get("agent.executor")
    if mode == "celery" or (mode == "auto" and workers_alive()):
        try:
            context_task.apply_async(args=[reason], **_queue())
            return "celery"
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent: Celery refused the context build (%s), running it in the web server", ex)
    _in_thread(lambda: build_context(reason=reason))
    return "thread"


def _dispatch(task: Any, fn: Any, *args: Any) -> str:
    from supagent import settings

    mode = settings.get("agent.executor")
    if mode == "celery" or (mode == "auto" and workers_alive()):
        try:
            task.apply_async(args=list(args), **_queue())
            return "celery"
        except Exception as ex:  # pylint: disable=broad-except
            log.warning("supagent: Celery refused %s (%s), running it here", task.name, ex)
    _in_thread(fn, *args, background=True)
    return "thread"


def dispatch_memory(message_id: int) -> str:
    from supagent.knowledge.memory import learn_from_message

    return _dispatch(remember_task, learn_from_message, message_id)


def dispatch_helpful(message_id: int) -> str:
    return _dispatch(helpful_task, learn_helpful, message_id)


def dispatch_catalog() -> str:
    """The formulas of the confirmed answers, after a Helpful / Not helpful (no LLM)."""
    def run() -> None:
        from supagent.knowledge.autocatalog import run as agent_catalog

        agent_catalog(llm_docs=False, parts=("formulas",))

    return _dispatch(catalog_task, run)


def dispatch_doc(doc_id: int) -> str:
    def run(i: int) -> None:
        from superset import db

        from supagent.knowledge.docs import refresh
        from supagent.knowledge.index import index_knowledge
        from supagent.models import Doc

        d = db.session.get(Doc, i)
        if d is not None:
            refresh(d)
            index_knowledge()

    return _dispatch(doc_task, run, doc_id)
