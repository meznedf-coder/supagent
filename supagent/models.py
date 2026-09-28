"""supagent's tables, in Superset's own database (PostgreSQL, MySQL or SQLite).

They are created and upgraded by `superset supagent init` (their own schema version in
supagent_meta), outside Superset's Alembic history, and hold no foreign key to Superset's
tables (ab_user, dbs): deleting a user or a database connection is never blocked; rows that
refer to a missing one are ignored.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from superset import db
from superset.extensions import encrypted_field_factory

from supagent.textsafe import SafeString, SafeText

SCHEMA_VERSION = 3


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


class Meta(db.Model):  # type: ignore[name-defined]
    __tablename__ = "supagent_meta"
    key = sa.Column(SafeString(64), primary_key=True)
    value = sa.Column(SafeText)


class Setting(db.Model):  # type: ignore[name-defined]
    """Settings changed in the admin page or with `superset supagent settings`. Secrets (tokens,
    consumer secrets) are encrypted with Superset's key (SECRET_KEY)."""

    __tablename__ = "supagent_setting"
    key = sa.Column(SafeString(128), primary_key=True)
    value = sa.Column(sa.JSON)
    secret = sa.Column(encrypted_field_factory.create(sa.Text))   # encrypted with Superset's SECRET_KEY
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    updated_by = sa.Column(SafeString(255))


class Source(db.Model):  # type: ignore[name-defined]
    """A Superset database the learner reads: metrics (promagg) or indices (osagg)."""

    __tablename__ = "supagent_source"
    id = sa.Column(sa.Integer, primary_key=True)
    database_id = sa.Column(sa.Integer, nullable=False, unique=True)
    database_name = sa.Column(SafeString(255))
    backend = sa.Column(SafeString(32))        # promagg | osagg
    last_learned_at = sa.Column(sa.DateTime)
    stats = sa.Column(sa.JSON)


class KObject(db.Model):  # type: ignore[name-defined]
    """One thing the agent knows: an index, a field, a metric, a metric label, a metric family."""

    __tablename__ = "supagent_object"
    __table_args__ = (sa.UniqueConstraint("source_id", "kind", "parent", "name", name="uq_supagent_object"),)
    id = sa.Column(sa.Integer, primary_key=True)
    source_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_source.id", ondelete="CASCADE"), index=True)
    kind = sa.Column(SafeString(16), nullable=False)          # index | field | metric | label | family
    parent = sa.Column(SafeString(512), nullable=False, default="")   # index of a field, metric of a label
    name = sa.Column(SafeString(512), nullable=False)
    data_type = sa.Column(SafeString(64))       # field: keyword, long, date...; label: string
    metric_type = sa.Column(SafeString(32))     # counter, gauge, histogram, summary, info, unknown
    unit = sa.Column(SafeString(64))
    backend_help = sa.Column(SafeText)          # HELP text of the exporter, mapping meta
    description = sa.Column(SafeText)
    description_source = sa.Column(SafeString(16))   # curated | backend | llm | inferred
    verified = sa.Column(sa.Boolean, default=False)  # a person wrote or approved the description
    category = sa.Column(SafeString(64))
    synonyms = sa.Column(sa.JSON)
    stats = sa.Column(sa.JSON)                 # cardinality, top values, value range, series, time range...
    first_seen = sa.Column(sa.DateTime, default=_now)
    last_seen = sa.Column(sa.DateTime, default=_now)
    gone_at = sa.Column(sa.DateTime)           # not seen by the last run
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    fingerprint = sa.Column(SafeString(64))     # of the learned facts: unchanged objects are skipped


class Relation(db.Model):  # type: ignore[name-defined]
    """How two objects relate, with the evidence: e.g. metric label node <-> index field NODE,
    82 of 90 values in common."""

    __tablename__ = "supagent_relation"
    __table_args__ = (sa.UniqueConstraint("a_id", "b_id", "relation", name="uq_supagent_relation"),)
    id = sa.Column(sa.Integer, primary_key=True)
    a_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_object.id", ondelete="CASCADE"), index=True)
    b_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_object.id", ondelete="CASCADE"), index=True)
    relation = sa.Column(SafeString(32))        # same_values | shares_label | histogram_parts | curated
    evidence = sa.Column(sa.JSON)              # {"a_values": n, "b_values": m, "common": k, "coverage": ...}
    confidence = sa.Column(sa.Float)
    origin = sa.Column(SafeString(16))          # learned | curated
    rejected_at = sa.Column(sa.DateTime)        # an admin marked it wrong: kept so that it is never measured again
    rejected_by = sa.Column(SafeString(255))
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)


class Run(db.Model):  # type: ignore[name-defined]
    __tablename__ = "supagent_run"
    id = sa.Column(sa.Integer, primary_key=True)
    kind = sa.Column(SafeString(16), default="learn")
    reason = sa.Column(SafeString(64))          # schedule | manual | cli
    started_at = sa.Column(sa.DateTime, default=_now)
    finished_at = sa.Column(sa.DateTime)
    status = sa.Column(SafeString(16), default="running")     # running | done | partial | error
    stats = sa.Column(sa.JSON)
    error = sa.Column(SafeText)


class Change(db.Model):  # type: ignore[name-defined]
    """What a learning run found different from the day before."""

    __tablename__ = "supagent_change"
    id = sa.Column(sa.Integer, primary_key=True)
    run_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_run.id", ondelete="CASCADE"), index=True)
    object_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_object.id", ondelete="CASCADE"), index=True)
    change = sa.Column(SafeString(32))          # new | gone | back | type | unit | cardinality | range
    detail = sa.Column(sa.JSON)
    at = sa.Column(sa.DateTime, default=_now)


class Conversation(db.Model):  # type: ignore[name-defined]
    __tablename__ = "supagent_conversation"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, index=True, nullable=False)   # ab_user.id, no foreign key
    title = sa.Column(SafeString(255))
    created_at = sa.Column(sa.DateTime, default=_now)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)


class Message(db.Model):  # type: ignore[name-defined]
    __tablename__ = "supagent_message"
    id = sa.Column(sa.Integer, primary_key=True)
    conversation_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_conversation.id", ondelete="CASCADE"),
                                index=True)
    role = sa.Column(SafeString(16))            # user | assistant
    content = sa.Column(SafeText)               # question, or the answer (Markdown)
    status = sa.Column(SafeString(16), default="done")   # pending | running | done | error | cancelling | cancelled
    steps = sa.Column(sa.JSON)                 # tools called so far: name, arguments, seconds, result head
    files = sa.Column(sa.JSON)                 # files the answer made (images, Excel), by id
    results = sa.Column(sa.JSON)               # rows of the queries it ran (table / chart views of the page)
    feedback = sa.Column(sa.Integer)           # +1 / -1
    task_id = sa.Column(SafeString(64))
    created_at = sa.Column(sa.DateTime, default=_now)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)   # last progress (steps saved)
    finished_at = sa.Column(sa.DateTime)


class File(db.Model):  # type: ignore[name-defined]
    """A file an answer made (chart image, Excel extract), kept in the database so that the web
    server can serve what a Celery worker on another host wrote; removed after
    tools.keep_days."""

    __tablename__ = "supagent_file"
    id = sa.Column(sa.Integer, primary_key=True)
    message_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_message.id", ondelete="CASCADE"), index=True)
    name = sa.Column(SafeString(255))
    mime = sa.Column(SafeString(128))
    size = sa.Column(sa.Integer)
    data = sa.Column(sa.LargeBinary)
    created_at = sa.Column(sa.DateTime, default=_now)


class Example(db.Model):  # type: ignore[name-defined]
    """Questions answered well (thumbs up), kept with the SQL that answered them: the agent
    sees the closest ones as examples."""

    __tablename__ = "supagent_example"
    id = sa.Column(sa.Integer, primary_key=True)
    question = sa.Column(SafeText)
    sql = sa.Column(SafeText)
    database_id = sa.Column(sa.Integer)
    tools = sa.Column(sa.JSON)
    message_id = sa.Column(sa.Integer)
    created_at = sa.Column(sa.DateTime, default=_now)


class Entry(db.Model):  # type: ignore[name-defined]
    """One piece of the catalog, edited on its own: a glossary, one index, a group of metrics,
    relationships, health checks, rules for the agent, notes. Deleting is soft; every change is
    kept in supagent_entry_version."""

    __tablename__ = "supagent_entry"
    id = sa.Column(sa.Integer, primary_key=True)
    title = sa.Column(SafeString(255), nullable=False)
    classification = sa.Column(SafeString(32), nullable=False)    # glossary | index | metrics | relationships |
    #                                                              checks | rule | note
    category = sa.Column(SafeString(128))
    fmt = sa.Column(SafeString(16), default="yaml")               # yaml | text | markdown
    content = sa.Column(SafeText)
    enabled = sa.Column(sa.Boolean, default=True)
    version = sa.Column(sa.Integer, default=1)
    deleted_at = sa.Column(sa.DateTime)
    created_at = sa.Column(sa.DateTime, default=_now)
    created_by = sa.Column(SafeString(255))
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    updated_by = sa.Column(SafeString(255))
    origin = sa.Column(SafeString(255))        # written by the agent: what it came from (formula:..., memory:<id>,
    #                                           doc:<id>); the agent changes it only while it made the last change
    evidence = sa.Column(sa.JSON)             # the agent's evidence (answers, approval, quoted document)


class EntryVersion(db.Model):  # type: ignore[name-defined]
    __tablename__ = "supagent_entry_version"
    id = sa.Column(sa.Integer, primary_key=True)
    entry_id = sa.Column(sa.Integer, sa.ForeignKey("supagent_entry.id", ondelete="CASCADE"), index=True)
    version = sa.Column(sa.Integer)
    title = sa.Column(SafeString(255))
    classification = sa.Column(SafeString(32))
    category = sa.Column(SafeString(128))
    fmt = sa.Column(SafeString(16))
    content = sa.Column(SafeText)
    enabled = sa.Column(sa.Boolean)
    deleted = sa.Column(sa.Boolean, default=False)
    changed_at = sa.Column(sa.DateTime, default=_now)
    changed_by = sa.Column(SafeString(255))


class Recipe(db.Model):  # type: ignore[name-defined]
    """The shortest successful way an answer was reached: the final query (SQL, PromQL) or the
    chart configuration, with its time and size. auto after an answer, confirmed by Helpful,
    rejected by Not helpful; similar questions of the team start from it."""

    __tablename__ = "supagent_recipe"
    id = sa.Column(sa.Integer, primary_key=True)
    question = sa.Column(SafeText)
    words = sa.Column(SafeText)                  # the question's words, for matching
    tool = sa.Column(SafeString(64))             # execute_sql | promql_query | generate_chart | export_excel
    database_id = sa.Column(sa.Integer, index=True)
    target = sa.Column(SafeString(512))          # table / metric / index
    query = sa.Column(SafeText)
    signature = sa.Column(SafeString(64), index=True)
    args = sa.Column(sa.JSON)
    seconds = sa.Column(sa.Float)
    rows = sa.Column(sa.Integer)
    steps = sa.Column(sa.Integer)               # tool calls the answer needed
    # helpful: a user marked an answer Helpful (an admin confirms or rejects it) | confirmed |
    # rejected | auto: saved by itself by 0.2.1 and before (no longer listed nor used)
    status = sa.Column(SafeString(16), default="helpful")
    confirmations = sa.Column(sa.JSON)          # ids of the answers marked Helpful that it comes from
    generic = sa.Column(sa.Boolean, default=False)   # question written generic by the LLM (not the user's words)
    uses = sa.Column(sa.Integer, default=1)
    user_id = sa.Column(sa.Integer)
    message_id = sa.Column(sa.Integer, index=True)
    created_at = sa.Column(sa.DateTime, default=_now)
    last_used_at = sa.Column(sa.DateTime, default=_now)


class Association(db.Model):  # type: ignore[name-defined]
    """A word of the questions and the table (metric or index) that successful answers to them
    read: how the agent learns where the data is from its own answers (learn.associations)."""

    __tablename__ = "supagent_association"
    __table_args__ = (sa.UniqueConstraint("word", "database_id", "kind", "parent", "name", name="uq_supagent_association"),)
    id = sa.Column(sa.Integer, primary_key=True)
    word = sa.Column(SafeString(64), nullable=False, index=True)
    database_id = sa.Column(sa.Integer, nullable=False)
    kind = sa.Column(SafeString(16), nullable=False)       # metric | index
    parent = sa.Column(SafeString(512), nullable=False, default="")
    name = sa.Column(SafeString(512), nullable=False)
    uses = sa.Column(sa.Integer, default=1)
    messages = sa.Column(sa.JSON)                          # the answers it comes from (the last 50)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)


class QueryStat(db.Model):  # type: ignore[name-defined]
    """How long each kind of query takes (literals removed), per database and table / metric."""

    __tablename__ = "supagent_query_stat"
    __table_args__ = (sa.UniqueConstraint("database_id", "signature", name="uq_supagent_query_stat"),)
    id = sa.Column(sa.Integer, primary_key=True)
    database_id = sa.Column(sa.Integer, index=True)
    target = sa.Column(SafeString(512), index=True)
    signature = sa.Column(SafeString(64))
    pattern = sa.Column(SafeText)                # the query with its literals replaced by ?
    calls = sa.Column(sa.Integer, default=0)
    errors = sa.Column(sa.Integer, default=0)
    total_seconds = sa.Column(sa.Float, default=0.0)
    max_seconds = sa.Column(sa.Float, default=0.0)
    total_rows = sa.Column(sa.Integer, default=0)
    last_error = sa.Column(SafeText)
    last_query = sa.Column(SafeText)             # the last query of this kind as it ran (admins see it)
    last_at = sa.Column(sa.DateTime, default=_now)


class Memory(db.Model):  # type: ignore[name-defined]
    """A preference, rule or fact learned from the chats (or written by a user): personal (only
    in its author's answers) or for the team (active at once or after an admin's approval,
    setting memory.team_approval)."""

    __tablename__ = "supagent_memory"
    id = sa.Column(sa.Integer, primary_key=True)
    scope = sa.Column(SafeString(8), default="user")          # user | team
    user_id = sa.Column(sa.Integer, index=True)               # the author
    kind = sa.Column(SafeString(16), default="preference")     # preference | rule | fact
    text = sa.Column(SafeText, nullable=False)
    category = sa.Column(SafeString(128))
    status = sa.Column(SafeString(16), default="active")       # active | proposed | disabled | catalog (moved
    #                                                           into a catalog entry by the agent)
    source = sa.Column(SafeString(16), default="chat")         # chat | manual
    message_id = sa.Column(sa.Integer)
    created_at = sa.Column(sa.DateTime, default=_now)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    approved_by = sa.Column(SafeString(255))


class Doc(db.Model):  # type: ignore[name-defined]
    """A document or a site the agent may search: an uploaded text, Markdown or HTML file, or
    web pages fetched again every refresh_days days."""

    __tablename__ = "supagent_doc"
    id = sa.Column(sa.Integer, primary_key=True)
    kind = sa.Column(SafeString(8), default="upload")         # upload | url
    title = sa.Column(SafeString(255))
    url = sa.Column(SafeString(2000))
    category = sa.Column(SafeString(128))
    content = sa.Column(SafeText)                             # the text (pages joined)
    pages = sa.Column(sa.JSON)                               # [{url, title, chars}]
    max_pages = sa.Column(sa.Integer, default=1)
    refresh_days = sa.Column(sa.Integer, default=7)
    enabled = sa.Column(sa.Boolean, default=True)
    status = sa.Column(SafeString(16), default="new")         # new | ok | error
    error = sa.Column(SafeText)
    content_hash = sa.Column(SafeString(64))
    fetched_at = sa.Column(sa.DateTime)
    created_at = sa.Column(sa.DateTime, default=_now)
    created_by = sa.Column(SafeString(255))
    learned_hash = sa.Column(SafeString(64))                  # the content the agent read definitions from


class Chunk(db.Model):  # type: ignore[name-defined]
    """One searchable piece of knowledge (a metric, an index, a note, a recipe, a memory, a part
    of a document), with its words (PostgreSQL full-text search) and its vector (the embedding
    model, float16) - no database extension needed."""

    __tablename__ = "supagent_chunk"
    id = sa.Column(sa.Integer, primary_key=True)
    ref = sa.Column(SafeString(128), unique=True, nullable=False)     # object:12, entry:3, doc:4#2...
    kind = sa.Column(SafeString(16), index=True)      # metric | index | note | rule | glossary | recipe | memory | doc
    source_id = sa.Column(sa.Integer, index=True)     # the learned database it is about (permission filter)
    database_id = sa.Column(sa.Integer, index=True)   # a recipe's database (permission filter)
    scope = sa.Column(SafeString(8), default="team")   # team | user
    user_id = sa.Column(sa.Integer, index=True)       # for scope user
    title = sa.Column(SafeString(512))
    text = sa.Column(SafeText)
    content_hash = sa.Column(SafeString(64))
    embed_model = sa.Column(SafeString(128))
    vector = sa.Column(sa.LargeBinary)                # float16, embed dimension
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)


class Document(db.Model):  # type: ignore[name-defined]
    """Curated knowledge written by people (the catalog: descriptions, checks, glossary)."""

    __tablename__ = "supagent_document"
    key = sa.Column(SafeString(64), primary_key=True)          # catalog
    content = sa.Column(SafeText)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    updated_by = sa.Column(SafeString(255))


TABLES = [Meta, Setting, Source, KObject, Relation, Run, Change, Conversation, Message, File, Example, Document,
          Entry, EntryVersion, Recipe, QueryStat, Memory, Doc, Chunk, Association]


def _add_missing_columns(engine: sa.engine.Engine) -> list[str]:
    """Columns of the models that an older version's tables lack, added (nullable ones only:
    later versions only ever add columns)."""
    added = []
    inspector = sa.inspect(engine)
    for model in TABLES:
        table = model.__table__
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or col.primary_key:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=engine.dialect)}"
            with engine.begin() as conn:
                conn.execute(sa.text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


def create_or_upgrade() -> tuple[int, int]:
    """Create the missing tables and columns and record the schema version; (version before, after)."""
    engine = db.engine
    for model in TABLES:
        model.__table__.create(bind=engine, checkfirst=True)
    _add_missing_columns(engine)
    row = db.session.get(Meta, "schema_version")
    before = int(row.value) if row else 0
    if 0 < before < 3:
        # 0.2.2: learned answers come only from Helpful. A 0.2.1 "confirmed" answer with
        # confirmations was marked Helpful by users: it now waits for an admin ("helpful"); one
        # without was confirmed by an admin and stays. Answers saved by themselves ("auto") are
        # kept but no longer listed nor used (superset supagent remove-auto-learned deletes them).
        for r in db.session.query(Recipe).filter(Recipe.status == "confirmed"):
            if r.confirmations:
                r.status = "helpful"
    if row is None:
        db.session.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
    else:
        row.value = str(SCHEMA_VERSION)
    db.session.commit()
    return before, SCHEMA_VERSION
