"""Is everything the team put in the knowledge given to the agent? A check an admin runs
(`superset supagent check-knowledge`, the admin page) and the tests use as their oracle:

  pieces        what the agent's search would write again (out of step with what it comes from)
  vectors       pieces not yet found by meaning (the embedding model is set)
  catalog       entries ignored (invalid YAML), conflicts, names the dictionary does not have
                (not learned yet, or misspelled: their descriptions reach nothing)
  rules         given in the instructions in full, or only found by the search
  waiting       team memories and learned answers waiting for an admin, documents that failed
  search        the knowledge search switched off (documents, Context, glossary and notes then
                reach the agent only through its tools)

Each problem comes with what to do."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from superset import db

from supagent import settings


def audit() -> dict[str, Any]:
    from supagent.knowledge import embeddings as E
    from supagent.knowledge.catalog import conflicts
    from supagent.knowledge.catalog import rules as catalog_rules
    from supagent.knowledge.curated import catalog_texts
    from supagent.knowledge.index import sync
    from supagent.models import Chunk, ContextPage, Doc, Entry, KObject, Memory, Recipe

    problems: list[str] = []
    out: dict[str, Any] = {"problems": problems}

    pending = sync(dry_run=True)
    out["pieces"] = {"total": db.session.query(Chunk).count(), "out_of_step": pending["added"] + pending["changed"]
                     + pending["removed"], "examples": pending.get("examples") or []}
    if out["pieces"]["out_of_step"]:
        problems.append(f"{out['pieces']['out_of_step']} searchable pieces are out of step with what they come from "
                        "(written at the next indexing, hourly; now: superset supagent index)")
    by_kind = dict(db.session.query(Chunk.kind, func.count()).group_by(Chunk.kind).all())
    out["pieces"]["by_kind"] = by_kind

    if E.enabled():
        model = E.model()
        missing = (db.session.query(Chunk).filter((Chunk.vector.is_(None)) | (Chunk.embed_model != model)).count())
        out["vectors"] = {"missing": missing, "model": model}
        if missing:
            problems.append(f"{missing} pieces have no vector of {model} yet: found by words only until the next "
                            "indexing (hourly; now: superset supagent index)")
    else:
        out["vectors"] = {"missing": None, "model": None}

    found = conflicts()
    out["catalog"] = {"errors": found["errors"], "conflicts": found["conflicts"]}
    for e in found["errors"]:
        problems.append(f"catalog entry {e['entry']!r} is ignored: {e['error']} (fix it in the Catalog tab)")
    for c in found["conflicts"]:
        problems.append(f"catalog: {c['kind']} {c['name']!r} is defined by {' and '.join(map(repr, c['entries']))}; "
                        f"{c['kept']!r} is used")
    known = {(k, p or "", n) for k, p, n in db.session.query(KObject.kind, KObject.parent, KObject.name)
             .filter(KObject.gone_at.is_(None))}
    unknown = sorted(f"{kind} {name}" + (f" of {parent}" if parent else "")
                     for (kind, parent, name) in catalog_texts() if (kind, parent or "", name) not in known)
    out["catalog"]["unknown_names"] = unknown
    if unknown and known:
        problems.append(f"the catalog describes {len(unknown)} names the dictionary does not have (not learned yet, "
                        f"or misspelled: their descriptions reach nothing): {', '.join(unknown[:8])}"
                        + (" ..." if len(unknown) > 8 else ""))

    from supagent.agent import RULE_CHARS, RULES_GIVEN

    team_rules = catalog_rules()
    long_rules = [r["title"] for r in team_rules[:RULES_GIVEN] if len(r["text"]) > RULE_CHARS]
    out["rules"] = {"enabled": len(team_rules), "in_instructions": min(len(team_rules), RULES_GIVEN),
                    "cut": long_rules}
    if len(team_rules) > RULES_GIVEN:
        problems.append(f"{len(team_rules)} rules: the first {RULES_GIVEN} are always given, the others only when "
                        "the search finds them for a question (merge rules, or make notes of the less general ones)")
    for title in long_rules:
        problems.append(f"rule {title!r} is longer than {RULE_CHARS} characters: its end is only found by the search "
                        "(shorten it, or make a note of the details)")

    proposed = db.session.query(Memory).filter(Memory.status == "proposed").count()
    helpful = db.session.query(Recipe).filter(Recipe.status == "helpful").count()
    failed = [d.title or d.url for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.status == "error")]
    empty = [d.title or d.url for d in db.session.query(Doc).filter(Doc.enabled.is_(True), Doc.status == "ok")
             if not (d.content or "").strip()]
    out["waiting"] = {"team_memories_to_approve": proposed, "learned_answers_to_confirm": helpful,
                      "documents_failed": failed, "documents_empty": empty}
    if proposed:
        problems.append(f"{proposed} team memories wait for an admin (Chat settings > Memory): not used until "
                        "approved")
    for title in failed:
        problems.append(f"document {title!r} could not be read: not used (see its error in Chat settings > "
                        "Documents)")
    for title in empty:
        problems.append(f"document {title!r} has no text: nothing of it is used")
    out["dictionary"] = {
        "objects": db.session.query(KObject).filter(KObject.gone_at.is_(None)).count(),
        "curated": db.session.query(KObject).filter(KObject.gone_at.is_(None),
                                                    KObject.description_source == "curated").count(),
        "to_verify": db.session.query(KObject).filter(KObject.gone_at.is_(None), KObject.description_source == "llm",
                                                      KObject.verified.is_(False)).count(),
    }
    out["context_pages"] = db.session.query(ContextPage).count()
    out["catalog"]["entries"] = dict(db.session.query(Entry.classification, func.count())
                                     .filter(Entry.deleted_at.is_(None), Entry.enabled.is_(True))
                                     .group_by(Entry.classification).all())
    out["search"] = {"enabled": bool(settings.get("search.enabled")), "top_k": settings.get("search.top_k"),
                     "prompt_chars": settings.get("search.prompt_chars")}
    if not out["search"]["enabled"]:
        problems.append("the knowledge search is off (search.enabled): documents, Context pages, the glossary and "
                        "notes reach the agent only when it calls search_knowledge")
    db.session.commit()
    return out
