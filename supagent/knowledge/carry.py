"""A message that continues the previous question keeps its conditions (the classic pipeline).

"when I say <APP> you should know the filter is APPLICATION=<APP> ... and show me now only <APP>", after an
answer counted with STATUS_INFO = 'KO' and POSITION_LABEL = 'D': the new queries added the application and
dropped the label ("the label rule does not apply because the request did not mention a specific label").
The previous answer's conditions on a table this answer queries again, that no query of this answer has
(nor groups by), and that the message does not change (it names their column or value) or remove ("all",
"without"...), are sent back once, then said under the answer. Time windows are not carried (a follow-up
may move them); a window written with now() is no condition here anyway (sql_conditions reads literals).
"""

from __future__ import annotations

import re
from typing import Any

REFINES = re.compile(r"\b(only|just|instead|also|same|again|except|as well|"
                     r"seulement|uniquement|aussi|m[êe]me|encore|sauf|plut[ôo]t)\b", re.I)
REMOVES = re.compile(r"\b(all|any|every|without|remove|drop|ignore|regardless|whatever|no longer|not only|"
                     r"tous|toutes|sans|enl[eè]ve|retire|ignore|quel que soit)\b", re.I)
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}")


def continues(question: str, follow: bool) -> bool:
    """The message goes on from the previous question (it refers to it, completes it, or refines it)."""
    return follow or bool(REFINES.search(question or ""))


def dropped(question: str, previous: list[str], current: list[str]) -> list[Any]:
    """The previous answer's conditions this answer's queries lost (see the module)."""
    from supagent.knowledge.conditions import sql_conditions
    from supagent.knowledge.rulecheck import _tables, grouped_by

    if not previous or not current or REMOVES.search(question or ""):
        return []
    tables = set().union(*(_tables(q) for q in current))
    used = {c.column.lower() for q in current for c in sql_conditions(q)}
    used |= {g.lower() for q in current for g in grouped_by(q)}
    asked = (question or "").lower()
    out: dict[str, Any] = {}
    for q in previous:
        if not _tables(q) & tables:
            continue                                   # another table: another question
        for c in sql_conditions(q):
            col = c.column.lower()
            if col in used or col in out or "(" in col:
                continue                               # kept, or an aggregate's (HAVING)
            if any(isinstance(v, str) and DATED.match(v) for v in c.values):
                continue                               # a time window: a follow-up may move it
            words = [w for w in re.split(r"[^a-z0-9]+", col) if len(w) >= 4]
            if any(re.search(rf"\b{re.escape(w)}", asked) for w in words) or \
                    any(str(v).lower() in asked for v in c.values if len(str(v)) >= 2):
                continue                               # the message names it: it changes it
            out[col] = c
    return list(out.values())
