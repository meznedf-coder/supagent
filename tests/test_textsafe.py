"""A metadata database that is not UTF-8 (PostgreSQL LATIN1, MySQL latin1): what supagent writes
is folded to what it can store, instead of failing with "'latin-1' codec can't encode"."""

from __future__ import annotations

import pytest


@pytest.fixture()
def latin1(ctx):
    from supagent import textsafe

    textsafe.set_codec("latin-1")
    yield
    textsafe.set_codec(None)


def test_fold_keeps_what_the_encoding_has_and_replaces_the_rest():
    from supagent.textsafe import fold, fold_all

    text = "Échecs – jobs … ‘ok’ “x” non‑breaking a•b → c ████ 5 € ≥ 3 é à ç ő ł 中 🚀"
    assert fold(text, None) == text                                   # UTF-8: nothing to do
    got = fold(text, "latin-1")
    assert got == "Échecs - jobs ... 'ok' \"x\" non-breaking a*b -> c #### 5 EUR >= 3 é à ç o ? ? ?"
    got.encode("latin-1")
    assert fold("é – €", "cp1252") == "é – €"                          # MySQL latin1 is cp1252: both exist there
    assert fold("é – 🚀", "ascii") == "e - ?"
    assert fold("é – 🚀", "utf8mb3") == "é – ?"                        # MySQL utf8: nothing beyond U+FFFF
    assert fold_all({"chart_name": "CPU – 24 Sep", "n": 3, "tags": ["a…"]}, "latin-1") == \
        {"chart_name": "CPU - 24 Sep", "n": 3, "tags": ["a..."]}


def test_supagent_tables_fold_on_write(latin1):
    from superset.extensions import db

    from supagent.models import Conversation, Message

    conv = Conversation(user_id=1, title="Failed jobs – yesterday")
    db.session.add(conv)
    db.session.flush()
    m = Message(conversation_id=conv.id, role="assistant", content="1,907 failed jobs – PRISMA’s … ████",
                status="done", steps=[{"tool": "execute_sql", "result": "a – b"}])
    db.session.add(m)
    db.session.commit()
    db.session.expire_all()
    stored = db.session.get(Message, m.id)
    assert stored.content == "1,907 failed jobs - PRISMA's ... ####"
    assert db.session.get(Conversation, conv.id).title == "Failed jobs - yesterday"
    assert stored.steps[0]["result"] == "a – b"                        # JSON is stored escaped: kept as it is
    db.session.delete(stored)
    db.session.delete(db.session.get(Conversation, conv.id))
    db.session.commit()


def test_download_names_survive_the_http_header():
    from supagent.textsafe import content_disposition

    h = content_disposition("attachment", "échecs – jobs “24”.xlsx")
    h.encode("latin-1")                                                # HTTP headers are Latin-1
    assert h.startswith("attachment; filename=\"echecs - jobs '24'.xlsx\"; ")
    assert "filename*=UTF-8''%C3%A9checs%20%E2%80%93%20jobs" in h


def test_names_saved_in_superset_are_folded(latin1):
    from supagent.agent import SAVING_TOOLS
    from supagent.textsafe import db_codec, fold_all

    assert {"generate_chart", "generate_dashboard", "create_report"} <= SAVING_TOOLS
    args = fold_all({"request": {"chart_name": "CPU busy % – 24 Sep", "config": {"x": {"name": "ts"}}}}, db_codec())
    assert args["request"]["chart_name"] == "CPU busy % - 24 Sep"
