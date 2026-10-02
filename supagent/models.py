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

SCHEMA_VERSION = 14


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
    feedback_reason = sa.Column(SafeText)      # what was wrong, said with Not helpful (for the admins, the memory)
    task_id = sa.Column(SafeString(64))
    created_at = sa.Column(sa.DateTime, default=_now)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)   # last progress (steps saved)
    finished_at = sa.Column(sa.DateTime)
    # 14: the subject of the chat a question and its answer belong to (1, 2, ...): the answer is given the messages
    # of its subject only (supagent.knowledge.topics); empty for the messages of older versions
    topic = sa.Column(sa.Integer)


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
    edited_by = sa.Column(SafeString(255))       # 14: an admin changed its question or its query
    edited_at = sa.Column(sa.DateTime)
    previous = sa.Column(sa.JSON)                # 14: its question and query before the last change


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
    source = sa.Column(SafeString(16))                     # 14: empty (learned from the answers) | admin (by hand)
    added_by = sa.Column(SafeString(255))                  # 14: the admin who added it


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


class Note(db.Model):  # type: ignore[name-defined]
    """A note any user writes in a few seconds (what was said in a meeting, a decision, a fact the team must
    not lose): for the team (every user of the agent reads it) or for its author only. The agent finds it
    like the documents, and says whose note it is and when: a note is not verified (never a team rule, never
    a source of a query's condition); an admin may make a catalog entry of it."""

    __tablename__ = "supagent_note"
    id = sa.Column(sa.Integer, primary_key=True)
    scope = sa.Column(SafeString(8), default="team", index=True)     # team | user
    user_id = sa.Column(sa.Integer, index=True)                       # the author
    title = sa.Column(SafeString(300))
    text = sa.Column(SafeText, nullable=False)
    tags = sa.Column(sa.JSON)                                         # ["pricing", "meeting"]
    meeting_on = sa.Column(sa.Date)                                   # the day it is about (a meeting's), if said
    pinned = sa.Column(sa.Boolean, default=False)
    source = sa.Column(SafeString(16), default="page")                # page | command (/note) | dictionary | agent
    entry_id = sa.Column(sa.Integer)                                  # the catalog entry an admin made of it
    versions = sa.Column(sa.JSON)                                     # its states before the last changes (12)
    created_at = sa.Column(sa.DateTime, default=_now, index=True)
    updated_at = sa.Column(sa.DateTime, default=_now, onupdate=_now)
    updated_by = sa.Column(SafeString(255))                           # who changed it last (its author, an admin)


class ChartScan(db.Model):  # type: ignore[name-defined]
    """What the nightly look at a Superset chart found (0.7): its last full day against the same weekday of the
    weeks before, per series (high, low, data stopped), what it shows in words (the LLM's, cached on what it
    reads), and why a chart was left out. Read by the agent (chart_anomalies) for the users who may see the
    chart; one row per chart, replaced at each look."""

    __tablename__ = "supagent_chart_scan"
    id = sa.Column(sa.Integer, primary_key=True)
    chart_id = sa.Column(sa.Integer, nullable=False, unique=True)
    dashboard_ids = sa.Column(sa.JSON)                 # the dashboards it is on
    datasource_id = sa.Column(sa.Integer, index=True)  # its dataset (the permission filter)
    database_id = sa.Column(sa.Integer, index=True)
    scanned_at = sa.Column(sa.DateTime)
    day = sa.Column(sa.Date)                           # the day compared (the last full day before "now")
    status = sa.Column(SafeString(16))                 # ok | stale | skipped | error
    reason = sa.Column(SafeText)                       # why skipped, the error, or how it was read
    findings = sa.Column(sa.JSON)                      # [{metric, series, now, median, change_pct, deviations, verdict}]
    checked = sa.Column(sa.Integer)                    # series compared
    understanding = sa.Column(SafeText)                # what it shows (AI-written)
    understanding_hash = sa.Column(SafeString(64))     # of what the LLM read: written again when it changes
    understood_at = sa.Column(sa.DateTime)


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
    reader = sa.Column(SafeString(16))                        # 14: web (empty) | confluence | bitbucket
    auth = sa.Column(sa.JSON)                                 # 14: {"type": bearer | basic | header, "user", "header"}
    secret = sa.Column(encrypted_field_factory.create(sa.Text))   # 14: its token or password (SECRET_KEY-encrypted)


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


class LLMCall(db.Model):  # type: ignore[name-defined]
    """One call to the LLM (the admins' usage page): what it was for, for whom, its size and time."""
    __tablename__ = "supagent_llm_call"
    id = sa.Column(sa.Integer, primary_key=True)
    at = sa.Column(sa.DateTime, default=_now, index=True)
    task = sa.Column(SafeString(24), index=True)        # answer | learn | context | memory | helpful | tidy | catalog | test
    user_id = sa.Column(sa.Integer, index=True)         # the person an answer was for (none for the nightly work)
    message_id = sa.Column(sa.Integer)
    run_id = sa.Column(sa.Integer)
    model = sa.Column(SafeString(128))
    prompt_tokens = sa.Column(sa.Integer)               # the context sent (instructions, chat, knowledge, results)
    completion_tokens = sa.Column(sa.Integer)
    cached_tokens = sa.Column(sa.Integer)               # of the prompt, from the LLM server's prompt cache
    seconds = sa.Column(sa.Float)
    tools = sa.Column(sa.Integer)                       # tools offered with the call
    ok = sa.Column(sa.Boolean, default=True)
    error = sa.Column(SafeString(300))


class Usage(db.Model):  # type: ignore[name-defined]
    """What one answer took: its LLM calls (seconds, tokens, and how many prompt tokens the LLM
    server's prompt cache saved) and its tool calls (superset supagent stats)."""

    __tablename__ = "supagent_usage"
    message_id = sa.Column(sa.Integer, primary_key=True, autoincrement=False)
    user_id = sa.Column(sa.Integer)
    created_at = sa.Column(sa.DateTime, default=_now, index=True)
    seconds = sa.Column(sa.Float)                 # the whole answer
    llm_calls = sa.Column(sa.Integer)
    llm_seconds = sa.Column(sa.Float)
    prompt_tokens = sa.Column(sa.Integer)
    completion_tokens = sa.Column(sa.Integer)
    cached_tokens = sa.Column(sa.Integer)
    tool_calls = sa.Column(sa.Integer)
    tool_seconds = sa.Column(sa.Float)
    failed_calls = sa.Column(sa.Integer)
    nudges = sa.Column(sa.Integer)                # answers sent back to the LLM (no tool used, a step announced)


class ContextPage(db.Model):  # type: ignore[name-defined]
    """A page of the Context: what the system is, functionally and technically, written every night
    from the shared knowledge (documents, catalog, team memory, data dictionary, Helpful answers).
    Facts pages are written without the LLM; summary pages by the LLM from the evidence only (marked
    AI-written, with their sources). A page a person edited is never written over by the agent. It is
    shown only to the users who may query every database it draws from (database_ids)."""

    __tablename__ = "supagent_context"
    __table_args__ = (sa.UniqueConstraint("section", "slug", name="uq_supagent_context_page"),)
    id = sa.Column(sa.Integer, primary_key=True)
    section = sa.Column(SafeString(16), nullable=False)      # functional | technical
    slug = sa.Column(SafeString(128), nullable=False)
    title = sa.Column(SafeString(255), nullable=False)
    kind = sa.Column(SafeString(16), default="facts")         # facts (no LLM) | summary (LLM)
    content = sa.Column(SafeText)                             # Markdown
    sources = sa.Column(sa.JSON)                              # [{"ref": "doc:3", "title": ...}]
    database_ids = sa.Column(sa.JSON)                         # the databases it draws from
    input_hash = sa.Column(SafeString(64))                    # of its evidence: written again only when it changes
    author = sa.Column(SafeString(255), default="agent")      # agent | the person who edited it
    version = sa.Column(sa.Integer, default=1)
    llm_calls = sa.Column(sa.Integer, default=0)
    tokens = sa.Column(sa.Integer, default=0)
    created_at = sa.Column(sa.DateTime, default=_now)
    updated_at = sa.Column(sa.DateTime, default=_now)


class Route(db.Model):  # type: ignore[name-defined]
    """What the decider showed for a question (the candidate tables with their features and scores), what
    it chose, what the answer's queries read, and what a person said of the answer: the gate learns from
    the confirmed ones (governed.gate)."""

    __tablename__ = "supagent_route"
    id = sa.Column(sa.Integer, primary_key=True)
    message_id = sa.Column(sa.Integer, index=True)    # the answer (no foreign key: old chats are purged)
    user_id = sa.Column(sa.Integer)
    question = sa.Column(SafeText)
    terms = sa.Column(SafeText)                     # the question's words (stems), space separated
    shown = sa.Column(sa.JSON)                      # [{"subject", "features", "score"}]
    chosen = sa.Column(sa.JSON)                     # subjects the decider chose
    used = sa.Column(sa.JSON)                       # subjects the queries of the answer read
    signal = sa.Column(SafeString(16))              # helpful | confirmed | clarified | not_helpful
    signal_at = sa.Column(sa.DateTime)
    created_at = sa.Column(sa.DateTime, default=_now)
    moa = sa.Column(SafeString(16), index=True)     # the router's route (supagent.router), other: the normal way
    moa_by = sa.Column(SafeString(16))              # llm | examples | admin example | fallback | off
    moa_confidence = sa.Column(SafeString(8))
    moa_followed = sa.Column(sa.Boolean)            # the execution kept to the route (only these teach)
    moa_detail = sa.Column(sa.JSON)                 # what the LLM said, the examples it was shown


class Facet(db.Model):  # type: ignore[name-defined]
    """A value of a category of the knowledge, in the deployment's own words: a subject, an application, a
    component (the aspect, functional or technical, is fixed). Seeded from what people wrote and from the data
    (approved), proposed by the LLM (used once an admin approves it)."""

    __tablename__ = "supagent_facet"
    __table_args__ = (sa.UniqueConstraint("facet", "value", name="uq_supagent_facet"),)
    id = sa.Column(sa.Integer, primary_key=True)
    facet = sa.Column(SafeString(24), nullable=False, index=True)   # aspect | subject | application | component
    value = sa.Column(SafeString(128), nullable=False)
    description = sa.Column(SafeText)
    synonyms = sa.Column(sa.JSON)
    status = sa.Column(SafeString(16), default="proposed")          # approved | proposed | rejected
    source = sa.Column(SafeString(16))                              # seed | data | llm | admin
    created_at = sa.Column(sa.DateTime, default=_now)
    reviewed_by = sa.Column(SafeString(255))
    reviewed_at = sa.Column(sa.DateTime)
    # 13: the values this one is part of (ids), in other categories or its own: a component of two applications,
    # an application of a subject. Its items count as about each of them too.
    parents = sa.Column(sa.JSON)
    # 13: what the LLM suggests about the value, waiting for an admin: {"parents": [ids it would be part of],
    # "same_as": id of an existing value naming the same thing (a merge)}
    suggested = sa.Column(sa.JSON)
    # 13: where the value comes from ("field NODE of jobs-* (OpenSearch)", "catalog category", "added by admin"...)
    origins = sa.Column(sa.JSON)


class Tag(db.Model):  # type: ignore[name-defined]
    """A knowledge item (entry:3, memory:5, doc:2, context:7, recipe:9, object:44) under a category value."""

    __tablename__ = "supagent_tag"
    __table_args__ = (sa.UniqueConstraint("ref", "facet_id", name="uq_supagent_tag"),)
    id = sa.Column(sa.Integer, primary_key=True)
    ref = sa.Column(SafeString(128), nullable=False, index=True)
    facet_id = sa.Column(sa.Integer, nullable=False, index=True)
    confidence = sa.Column(sa.Float)
    source = sa.Column(SafeString(16))                              # llm | admin | family
    status = sa.Column(SafeString(16), default="approved")          # approved | proposed | rejected
    created_at = sa.Column(sa.DateTime, default=_now)
    reviewed_by = sa.Column(SafeString(255))


class Link(db.Model):  # type: ignore[name-defined]
    """A relation between two knowledge items, or an item and a table (data:<database>:<name>), found by the LLM
    in the texts (about, depends_on, part_of, explains, runs_on), used once approved or confident."""

    __tablename__ = "supagent_link"
    __table_args__ = (sa.UniqueConstraint("a_ref", "b_ref", "kind", name="uq_supagent_link"),)
    id = sa.Column(sa.Integer, primary_key=True)
    a_ref = sa.Column(SafeString(128), nullable=False, index=True)
    b_ref = sa.Column(SafeString(128), nullable=False, index=True)
    kind = sa.Column(SafeString(24), nullable=False)
    confidence = sa.Column(sa.Float)
    source = sa.Column(SafeString(16))
    status = sa.Column(SafeString(16), default="approved")
    created_at = sa.Column(sa.DateTime, default=_now)
    reviewed_by = sa.Column(SafeString(255))
    note = sa.Column(SafeText)                  # 14: what the relation is, in a few words (the system map shows it)


class Classified(db.Model):  # type: ignore[name-defined]
    """An item the LLM classified, with the hash of its text then: classified again only when it changes."""

    __tablename__ = "supagent_classified"
    ref = sa.Column(SafeString(128), primary_key=True)
    content_hash = sa.Column(SafeString(64))
    classified_at = sa.Column(sa.DateTime, default=_now)


class ItemUse(db.Model):  # type: ignore[name-defined]
    """A knowledge item given to the LLM for an answer (what the agent learned is ranked from it and from what
    people said of the answers)."""

    __tablename__ = "supagent_item_use"
    id = sa.Column(sa.Integer, primary_key=True)
    ref = sa.Column(SafeString(128), nullable=False, index=True)
    route_id = sa.Column(sa.Integer, index=True)
    message_id = sa.Column(sa.Integer, index=True)
    user_id = sa.Column(sa.Integer)
    how = sa.Column(SafeString(16))                                 # given | chosen | used
    created_at = sa.Column(sa.DateTime, default=_now, index=True)


TABLES = [Meta, Setting, Source, KObject, Relation, Run, Change, Conversation, Message, File, Example, Document,
          Entry, EntryVersion, Recipe, QueryStat, Memory, Doc, Chunk, Association, Usage, ContextPage, LLMCall,
          Route, Facet, Tag, Link, Classified, ItemUse, Note, ChartScan]


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


def _restem() -> None:
    """0.4.0: words are stemmed further (failed, failure -> fail): the words stored by an older
    version (learned answers, associations) are stemmed again; associations that become the
    same are merged."""
    from supagent.knowledge.describe import stem

    for r in db.session.query(Recipe):
        new = " ".join(sorted({stem(w) for w in (r.words or "").split()}))
        if new != (r.words or ""):
            r.words = new
    db.session.flush()
    rows = db.session.query(Association).order_by(Association.id).all()
    kept: dict[tuple, Association] = {}
    for a in rows:
        key = (stem(a.word or "")[:64], a.database_id, a.kind, a.parent or "", a.name)
        if key in kept:                                  # "failed" and "failure" -> one "fail"
            k = kept[key]
            k.uses = (k.uses or 0) + (a.uses or 0)
            k.messages = (list(k.messages or []) + list(a.messages or []))[-50:]
            db.session.delete(a)
            continue
        kept[key] = a
    db.session.flush()
    for key, a in kept.items():
        if a.word != key[0]:
            a.word = key[0]
    db.session.flush()


def _rename_classification(old: str, new: str) -> None:
    """0.8: the catalog's "note" entries are "guide" entries (the users' quick notes are the notes): the entries,
    their history and their search pieces (the knowledge store follows: store_kinds)."""
    db.session.query(Entry).filter(Entry.classification == old).update({Entry.classification: new},
                                                                       synchronize_session=False)
    db.session.query(EntryVersion).filter(EntryVersion.classification == old).update(
        {EntryVersion.classification: new}, synchronize_session=False)
    db.session.query(Chunk).filter(Chunk.kind == old, Chunk.ref.like("entry:%")).update(
        {Chunk.kind: new}, synchronize_session=False)
    db.session.flush()
    _RENAMED_KINDS.append((old, new))


_RENAMED_KINDS: list[tuple[str, str]] = []     # done by this upgrade: the knowledge store follows after the commit


def store_kinds() -> int:
    """The knowledge store's pieces of the kinds renamed by this upgrade (rows changed; 0 without a store)."""
    if not _RENAMED_KINDS:
        return 0
    from supagent.knowledge import pgstore

    n = 0
    for old, new in _RENAMED_KINDS:
        n += pgstore.rename_kind(old, new, "entry:")
    _RENAMED_KINDS.clear()
    return n


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
    if 0 < before < 4:
        _restem()
    if 0 < before < 14:
        _rename_classification("note", "guide")
    if row is None:
        db.session.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
    else:
        row.value = str(SCHEMA_VERSION)
    db.session.commit()
    return before, SCHEMA_VERSION
