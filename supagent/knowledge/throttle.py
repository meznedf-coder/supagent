"""Protection of the backends while learning (Mimir, OpenSearch): one request at a time,
at most learn.max_requests_per_minute, each with a timeout, and a breaker that stops the
database after learn.stop_after_errors failures in a row (HTTP 429, 5xx, timeouts,
refused connections). The requests made are counted for the run's statistics."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


class SourceStopped(Exception):
    """The breaker opened: this database is left alone until the next run."""


class Throttle:
    def __init__(self, per_minute: int, stop_after: int, name: str = "") -> None:
        self.interval = 60.0 / max(1, int(per_minute))
        self.stop_after = max(1, int(stop_after))
        self.name = name
        self.requests = 0
        self.errors = 0
        self.failures_in_row = 0
        self.last_error = ""
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        from supagent.knowledge.stopping import check

        check()                                   # an admin stopped the run: not one more request
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + self.interval

    def call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        if self.failures_in_row >= self.stop_after:
            raise SourceStopped(f"{self.name}: stopped after {self.failures_in_row} failures in a row "
                                f"({self.last_error})")
        self.wait()
        self.requests += 1
        try:
            out = fn(*args, **kwargs)
        except Exception as ex:  # pylint: disable=broad-except
            if _is_overload(ex):
                self.errors += 1
                self.failures_in_row += 1
                self.last_error = f"{type(ex).__name__}: {str(ex)[:200]}"
                log.warning("supagent learn: %s: %s (%s in a row)", self.name, self.last_error, self.failures_in_row)
                if self.failures_in_row >= self.stop_after:
                    raise SourceStopped(f"{self.name}: stopped after {self.failures_in_row} failures in a row "
                                        f"({self.last_error})") from ex
                time.sleep(min(30.0, 2.0 * self.failures_in_row))       # back off
            raise
        self.failures_in_row = 0
        return out

    def stats(self) -> dict[str, Any]:
        return {"requests": self.requests, "errors": self.errors,
                **({"last_error": self.last_error} if self.last_error else {})}


def _is_overload(ex: Exception) -> bool:
    """Failures that say the backend is busy or unreachable (not a bad query)."""
    text = f"{type(ex).__name__} {ex}".lower()
    status = getattr(ex, "status_code", None) or getattr(getattr(ex, "response", None), "status_code", None) \
        or getattr(ex, "status", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return any(s in text for s in ("429", "too many requests", "timeout", "timed out", "connection", "unavailable",
                                   "circuit_breaking", "503", "502", "504", "rejected_execution"))


class Proxy:
    """An object whose methods go through the throttle (a promagg client, an osagg transport)."""

    def __init__(self, target: Any, throttle: Throttle, methods: tuple[str, ...]) -> None:
        self._target = target
        self._throttle = throttle
        self._methods = methods

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if name in self._methods and callable(attr):
            return lambda *a, **k: self._throttle.call(attr, *a, **k)
        return attr
