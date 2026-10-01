"""A second, independent computation of an answer's figures (0.7, test: agent.cross_check, off by default).

When the classic agent (the model writes the queries) answers with figures, the governed pipeline answers the same
question its own way: its plan, its queries built by code, without the classic agent (a question it would hand to
the classic agent gives no second opinion). The classic answer's headline figures (in bold, else the first ones)
are looked for among the second answer's figures and its queries' rows: all there, the two agree; one missing
while the second gave figures, they disagree, and the answer says so with the other figures, for the user to
judge. Agreement between independent ways of computing is the strongest outside signal that an answer is right
(consistency-based confidence for text-to-SQL; Entezari Maleki, Pourreza, Rafiei 2025), at the cost of the second
computation's time (agent.cross_check_seconds at most; later: no second opinion)."""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)
MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|janv|févr|mars|avr|mai|juin|juil|août|sept|déc)[a-zéû]*\.?"
YEAR = r"(?:19|20)\d{2}"
NOT_FIGURES = re.compile(                              # dates (with their year), times, codes and ids: not figures
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}(?:[ T]\d{{2}}:\d{{2}}(?::\d{{2}})?)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th|er)?\s+{MONTHS}(?:,?\s+{YEAR})?"
    rf"|\b{MONTHS}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:{YEAR}\b)?"
    rf"|\b(?:in|of|year)\s+{YEAR}\b|\b\d{{1,2}}[:h]\d{{2}}\b"
    r"|\b[A-Z][A-Z0-9_-]*\d[A-Z0-9_-]*\b|\b(?:id|#)\s*\d+\b", re.I)
MIN_FIGURE = 2.0              # 0 and 1 say too little to compare


NUMBER = re.compile(r"(?<![\w.])[-\u2212\u2013]?(?:\d{1,3}(?:[,\u202f\u00a0 ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?![\w])")


def _number(raw: str) -> float | None:
    """One reading of a number as written: 5,939 and 5 939 are 5939; 12,5 is 12.5; 1,234.5 is 1234.5."""
    raw = raw.replace("\u2212", "-").replace("\u2013", "-")
    m = re.fullmatch(r"(-?)(\d{1,3}(?:[,\u202f\u00a0 ]\d{3})+)(?:[.,](\d+))?", raw)
    if m:
        whole = re.sub(r"[,\u202f\u00a0 ]", "", m.group(2))
        text = f"{m.group(1)}{whole}" + (f".{m.group(3)}" if m.group(3) else "")
    else:
        text = raw.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def figures(text: str) -> list[float]:
    """The numbers of a text without its dates, times, years, codes and ids."""
    out = []
    for m in NUMBER.finditer(NOT_FIGURES.sub(" ", text or "")):
        v = _number(m.group(0).strip())
        if v is not None and abs(v) >= MIN_FIGURE:
            out.append(v)
    return out


def headline(text: str) -> list[float]:
    """The figures an answer stands on: its bold ones, else the first three of its first paragraph."""
    bold = [v for b in re.findall(r"\*\*([^*]+)\*\*", text or "") for v in figures(b)]
    if bold:
        return bold[:6]
    return figures((text or "").strip().split("\n\n")[0])[:3]


def close(x: float, y: float) -> bool:
    """The same figure, rounded or in thousands or millions."""
    x, y = abs(x), abs(y)
    for scale in (1.0, 1e3, 1e6, 1e-3, 1e-6, 100.0, 0.01):
        w = y * scale
        if abs(x - w) <= max(0.006 * max(x, w), 0.51 if x >= 100 else 0.0051 * max(1.0, x)):
            return True
    return False


def rows_of(trace: list[dict]) -> list[float]:
    """The figures of the queries an answer ran (their results)."""
    out: list[float] = []
    for t in trace:
        if t.get("status") == "done" and (t.get("called") or t.get("tool")) not in ("classic", "decide", "plan"):
            out += figures(str(t.get("result") or ""))
    return out


def verdict(answer: str, other_text: str, other_rows: list[float]) -> tuple[str, list[float]]:
    """(agree | disagree | undecided, the headline figures not found)."""
    head = headline(answer)
    pool = figures(other_text) + list(other_rows)
    if not head or not pool:
        return "undecided", []
    missing = [h for h in head if not any(close(h, p) for p in pool)]
    return ("agree" if not missing else "disagree"), missing


def wanted(answer: str, trace: list[dict]) -> bool:
    """A classic answer with figures from its own queries: worth a second computation."""
    from supagent import settings

    if not settings.get("agent.cross_check"):
        return False
    queried = any((t.get("called") or t.get("tool")) in ("execute_sql", "promql_query") and t.get("status") == "done"
                  for t in trace)
    return queried and bool(headline(answer))


def second_opinion(username: str, question: str, history: list[dict] | None, should_stop: Any = None) -> dict:
    """The governed pipeline's answer to the same question (no classic agent): {text, rows, way}."""
    from supagent import settings
    from supagent.governed.pipeline import GovernedAgent
    from supagent.llm import bounded

    agent = GovernedAgent(username, rich_results=True, should_stop=should_stop)
    agent.second_opinion = True
    try:
        with bounded(agent.llm, float(settings.get("agent.cross_check_seconds") or 180)):
            text, trace = agent.ask(question, history)
    finally:
        agent.close()
    return {"text": text or "", "rows": rows_of(trace), "way": getattr(agent, "way", "") or ""}


def note(missing: list[float], other: dict) -> str:
    """What the answer says when the second computation does not give its figures."""
    def fmt(v: float) -> str:
        return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    theirs = [v for v in headline(other["text"]) or figures(other["text"])[:4] if v not in missing][:4]
    said = ", ".join(fmt(v) for v in missing[:4])
    other_said = (", ".join(fmt(v) for v in theirs) if theirs else "other figures")
    return (f"\n\n(Check: a second, independent way of computing it (a checked plan, its queries built by code) "
            f"did not find {said}; it found {other_said}. Compare the two before relying on these figures.)")


def check(username: str, question: str, history: list[dict] | None, answer: str, trace: list[dict],
          should_stop: Any = None) -> tuple[str, dict | None]:
    """The answer, with a note when a second computation disagrees; and what was found (for the steps)."""
    if not wanted(answer, trace):
        return answer, None
    try:
        other = second_opinion(username, question, history, should_stop)
    except Exception as ex:  # pylint: disable=broad-except   (no second opinion: the answer as it was)
        from supagent.agent import Cancelled

        if isinstance(ex, Cancelled):
            raise
        log.info("supagent cross-check: no second opinion: %s", str(ex)[:200])
        return answer, {"verdict": "undecided", "why": f"{type(ex).__name__}: {str(ex)[:200]}"}
    if other["way"].startswith("classic") or not other["text"].strip():
        return answer, {"verdict": "undecided", "why": other["way"] or "no answer"}
    v, missing = verdict(answer, other["text"], other["rows"])
    found = {"verdict": v, "missing": missing, "headline": headline(answer), "other": other["text"][:600]}
    if v == "disagree":
        return answer + note(missing, other), found
    return answer, found
