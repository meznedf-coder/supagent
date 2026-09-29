"""The AI descriptions written during a learning run, not after it: a thread next to the learner
describes what nobody described yet (after the catalog, which wins) while the databases are read.
The learner spends most of its time waiting for the databases (read one request at a time, at
learn.max_requests_per_minute): the LLM works meanwhile. A database of thousands of metrics that
takes two hours to learn has its descriptions filled in as its metrics are learned, instead of
two hours later.

The thread stops when the learner has read every database (the rest is described at the end of
the run, after the relations and the catalog), at the data time limit, when an admin stops the
run, or on an LLM error. Its LLM calls wait while answers are being computed, like every
background LLM call."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)
IDLE_S = 10.0                    # nothing to describe yet: look again after this long
CATALOG_S = 120.0                # the catalog is applied again at most this often (big catalogs, long runs)


class Describer(threading.Thread):
    def __init__(self, app: Any, run_id: int, deadline: float, llm: Any = None) -> None:
        super().__init__(name=f"supagent-describe-{run_id}", daemon=True)
        self.app, self.run_id, self.deadline, self.llm = app, run_id, deadline, llm
        self.done = threading.Event()               # the learner read every database
        self.out: dict[str, Any] = {"written": 0, "requests": 0, "by_source": {}}

    def _add(self, res: dict[str, Any]) -> None:
        self.out["written"] += res.get("written", 0)
        self.out["requests"] += res.get("requests", 0)
        for sid, n in (res.get("by_source") or {}).items():
            self.out["by_source"][sid] = self.out["by_source"].get(sid, 0) + n

    def run(self) -> None:
        from superset import db

        from supagent.knowledge.curated import apply_catalog
        from supagent.knowledge.enrich import enrich
        from supagent.knowledge.stopping import LearningStopped, watching
        from supagent.llm import background

        with self.app.app_context():
            try:
                with background(), watching(self.run_id):
                    applied = 0.0
                    while not self.done.is_set() and time.time() < self.deadline:
                        if time.time() - applied >= CATALOG_S:
                            apply_catalog()                 # people's texts first: never described twice
                            applied = time.time()
                        res = enrich(None, self.deadline, llm=self.llm, pause=self.done)
                        self._add(res)
                        if res.get("error"):
                            self.out["error"] = res["error"]
                            break
                        if not res.get("requests"):         # nothing new yet: the learner is reading
                            self.done.wait(IDLE_S)
            except LearningStopped:
                self.out["stopped"] = "stopped by an admin"
            except Exception as ex:  # pylint: disable=broad-except   (the learning goes on without it)
                log.exception("supagent learn: descriptions during the run")
                self.out["error"] = f"{type(ex).__name__}: {str(ex)[:300]}"
            finally:
                db.session.remove()

    def finish(self, timeout: float, watch: bool = False) -> dict[str, Any]:
        """Stop after the LLM request in progress; what it wrote. watch: an admin's Stop ends the
        wait at once (LearningStopped in the learner; the LLM request ends in the background)."""
        from supagent.knowledge.stopping import check

        self.done.set()
        end = time.time() + timeout
        while self.is_alive() and time.time() < end:
            self.join(min(1.0, max(0.0, end - time.time())))
            if watch:
                check()
        if self.is_alive():
            self.out["still_running"] = True
        return self.out
