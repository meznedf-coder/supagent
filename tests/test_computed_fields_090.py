"""0.9: a column the connector computes (a business-date label, its time) is described by what the connector says
it is, never by the LLM: shown a column with no value of its own, the LLM guessed ("the status of a run") and the
agent then filtered the label on states."""

from __future__ import annotations

from test_knowledge import world  # noqa: F401  (the fixture)

LABEL = "computed by the connector: the business-day label of DAY (D, D-1, W-1, Y-1...)"


def test_a_computed_column_is_described_by_the_connector(world, app):  # noqa: F811
    from superset.extensions import db

    from supagent.knowledge import enrich
    from supagent.knowledge.describe import _field_line
    from supagent.knowledge.learn_indices import _field_facts, computed_help, describe_computed
    from supagent.models import KObject, Source

    src = db.session.query(Source).first()
    guessed = KObject(source_id=src.id, kind="field", parent="jobs", name="DAY_LABEL", data_type="varchar",
                      stats={"computed": LABEL}, description="The current status of a run, such as WAITING or DONE.",
                      description_source="llm", verified=False)
    checked = KObject(source_id=src.id, kind="field", parent="jobs", name="DAY_TIME", data_type="timestamp",
                      stats={"computed": "computed by the connector: ts moved onto the D-1 position date of DAY"},
                      description="The run's time on its business day.", description_source="llm", verified=True)
    blank = KObject(source_id=src.id, kind="field", parent="jobs", name="DAY_LABEL_2", data_type="varchar",
                    stats={"computed": LABEL})
    db.session.add_all([guessed, checked, blank])
    db.session.commit()
    try:
        assert describe_computed(src.id) == 2                                  # the guess and the blank one
        assert guessed.description == computed_help(LABEL) and guessed.description_source == "backend"
        assert guessed.description.startswith("Computed by the connector: the business-day label of DAY")
        assert checked.description == "The run's time on its business day."     # what an admin verified stays
        assert describe_computed(src.id) == 0
        blank.description = None
        assert enrich._write(blank, {"id": blank.id, "description": "The status of the run."}) == 1
        assert blank.description == computed_help(LABEL) and blank.description_source == "backend"
        line = _field_line(guessed)
        assert line.count("the business-day label of DAY") == 1 and "status" not in line
        assert _field_facts("varchar", {"computed": LABEL})["backend_help"] == computed_help(LABEL)
        assert "backend_help" not in _field_facts("keyword", {"cardinality": 3})
    finally:
        for o in (guessed, checked, blank):
            db.session.delete(o)
        db.session.commit()


def test_a_computed_time_is_not_taken_as_the_time_of_the_rows(world, app, monkeypatch):  # noqa: F811
    """compare_groups asked with time_field = the business date's own time (a computed column): the table's own
    time field is used, and said; and the table's own time field is never one of the stages of its rows."""
    from superset.extensions import db

    from supagent import tools as T
    from supagent.models import KObject, Source

    src = db.session.query(Source).first()
    made = [KObject(source_id=src.id, kind="field", parent="runs9", name="DAY_TIME", data_type="timestamp",
                    stats={"computed": "computed by the connector: ts moved onto the D-1 position date of DAY"}),
            KObject(source_id=src.id, kind="field", parent="runs9", name="ts", data_type="date", stats={}),
            KObject(source_id=src.id, kind="field", parent="runs9", name="ENDED", data_type="date", stats={})]
    db.session.add_all(made)
    db.session.commit()
    monkeypatch.setattr(T, "_catalog", lambda: {"indices": {"runs9": {"time_field": "ts"}}})
    try:
        assert T._row_time_field("runs9", "DAY_TIME") == ("ts", "DAY_TIME")
        assert T._row_time_field("runs9", "ENDED") == ("ENDED", None)          # another time of the rows: as asked
        assert T._row_time_field("runs9", None) == ("ts", None)
        stages = [m.fields[0] for m in T._measures("runs9", "ENDED") if m.kind == "reached"]
        assert stages == []                                                    # neither ENDED (asked) nor ts (its own)
        assert [m.fields[0] for m in T._measures("runs9", "ts") if m.kind == "reached"] == ["ENDED"]
    finally:
        for o in made:
            db.session.delete(o)
        db.session.commit()
