"""Settings: what the admin page and `superset supagent settings` change.

A value comes from, in order: the supagent_setting table (admin page, CLI), superset_config.py
(SUPAGENT_<KEY> with dots as underscores, e.g. SUPAGENT_LLM_BASE_URL), the environment (the
same name), the default below. Secrets are stored encrypted with Superset's SECRET_KEY and
never shown back (the admin page only says whether one is set).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Spec:
    key: str
    default: Any
    kind: str            # str | int | float | bool | list | json | choice
    help: str
    secret: bool = False
    choices: tuple[str, ...] = ()


SPECS: list[Spec] = [
    # ---- the LLM (OpenAI-compatible chat completions with tool calls)
    Spec("llm.base_url", "", "str", "OpenAI-compatible API base: what comes before /chat/completions, "
         "e.g. https://llm-gateway.example/v1/openai or http://llm-host:8080/v1"),
    Spec("llm.model", "", "str", "Model name; empty: the first model the server lists"),
    Spec("llm.auth", "none", "choice", "How the agent authenticates to the LLM: none, token (a fixed "
         "access token), middleware (a token obtained from the company middleware, renewed before "
         "it expires)", choices=("none", "token", "middleware")),
    Spec("llm.token", "", "str", "The fixed access token (llm.auth = token)", secret=True),
    Spec("llm.middleware.token_url", "", "str", "Middleware token endpoint (OAuth2 client "
         "credentials: POST grant_type=client_credentials)"),
    Spec("llm.middleware.consumer_key", "", "str", "Consumer key (sent with the consumer secret as HTTP Basic)"),
    Spec("llm.middleware.consumer_secret", "", "str", "Consumer secret", secret=True),
    Spec("llm.middleware.cert_path", "", "str", "Client certificate file (PEM) for the middleware, if it "
         "requires one (mutual TLS)"),
    Spec("llm.middleware.key_path", "", "str", "Private key file (PEM) of the client certificate"),
    Spec("llm.middleware.scope", "", "str", "OAuth2 scope to ask for (empty: none)"),
    Spec("llm.middleware.renew_before", 60, "int", "Renew the token this many seconds before it expires"),
    Spec("llm.ca_bundle", "", "str", "CA file to trust for the middleware and the LLM (empty: the "
         "system's CAs)"),
    Spec("llm.verify_tls", True, "bool", "Check the TLS certificates of the middleware and the LLM"),
    Spec("llm.timeout", 900, "int", "Seconds to wait for one LLM answer"),
    Spec("llm.temperature", 0.2, "float", "Sampling temperature"),
    Spec("llm.thinking", False, "bool", "Let reasoning models think before each step (slower)"),
    Spec("llm.extra_headers", {}, "json", "More HTTP headers for the LLM calls (JSON object)"),
    # ---- the agent
    Spec("agent.max_steps", 16, "int", "Tool calls per question at most"),
    Spec("agent.databases", [], "list", "Databases the agent may use (names or ids); empty: the OpenSearch "
         "(osagg) and Prometheus / Mimir (promagg) ones. Superset's database access still applies"),
    Spec("agent.osagg_max_scan_rows", 20000, "int", "OpenSearch (osagg): raw documents one query of the agent may "
         "read when it cannot be pushed down (the connection's own cap applies if lower); above, the query is "
         "refused at once with the reason instead of running for minutes. 0: the connection's cap"),
    Spec("agent.now", "", "str", "A fixed 'now' (YYYY-MM-DD HH:MM) for demos on old data; empty: the clock"),
    Spec("agent.extra_instructions", "", "str", "More instructions added to the agent's prompt"),
    Spec("agent.disabled_tools", [], "list", "Tools the agent must not use (e.g. send_email)"),
    Spec("usage.keep_days", 90, "int", "Days the record of every LLM call is kept (the LLM usage page); 0: kept"),
    Spec("agent.check_numbers", True, "bool", "Every number of an answer must come from what the agent was given "
         "(query results, their totals and rates, the question, the knowledge): the agent is asked once to take "
         "the others from a query, then they are marked in the answer"),
    # ---- learning
    Spec("learn.enabled", True, "bool", "Learn once a day (at the hour below, on the days below)"),
    Spec("learn.hour", 2, "int", "Daily at this hour (0-23, server time): ONE run per day, not every N hours"),
    Spec("learn.days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "list",
         "Days of the week with a learning run (mon, tue, wed, thu, fri, sat, sun)"),
    Spec("learn.user", "", "str", "Superset user the learner reads the data as (empty: the first Admin)"),
    Spec("learn.databases", [], "list", "Database names or ids to learn (empty: every osagg and promagg database)"),
    Spec("learn.indices", [], "list", "Only these indices (patterns with *, e.g. batch-jobs*); empty: every index "
         "the OpenSearch database lists"),
    Spec("learn.indices_exclude", [], "list", "Never these indices (patterns with *)"),
    Spec("learn.metrics", [], "list", "Only these metrics (patterns with *, e.g. node_*, batch_*); empty: every metric"),
    Spec("learn.metrics_exclude", [], "list", "Never these metrics (patterns with *, e.g. go_*)"),
    Spec("learn.max_minutes", 30, "int", "Stop a learning run after this many minutes (the next run continues)"),
    Spec("learn.max_objects", 20000, "int", "Metrics or indices listed per database at most"),
    Spec("learn.profile_every_days", 7, "int", "Refresh the statistics of each metric or index every N days (a "
         "rolling cycle: each day only its share; new ones at once). The list of metrics, indices and types is "
         "read every run"),
    Spec("learn.max_requests_per_minute", 60, "int", "Requests per minute to each database while learning (one at "
         "a time)"),
    Spec("learn.request_timeout", 30, "int", "Seconds before one learning request is given up"),
    Spec("learn.stop_after_errors", 5, "int", "Stop learning a database after this many failures in a row "
         "(429, 5xx, timeouts): the backend is busy"),
    Spec("learn.profile_hours", 24, "int", "Window of the value statistics (hours; one hour above 50,000 series)"),
    Spec("learn.stats_max_series", 200000, "int", "No value statistics for metrics with more series than this"),
    Spec("learn.series_sample", 1000, "int", "Series read per metric to learn its labels and their values"),
    Spec("learn.sample_docs", 100000, "int", "Documents sampled per index (per shard) for the field statistics"),
    Spec("learn.fields_per_request", 40, "int", "Field statistics per OpenSearch request (big indices: several "
         "requests)"),
    Spec("learn.group_rollover", True, "bool", "Learn dated or rolled-over indices (logs-2026.09.27, "
         "...-000123) as one family, through its latest member"),
    Spec("learn.associations", True, "bool", "Learn where the data is from the answers: the words of a question and "
         "the metrics or indices its successful queries read (used to find them for the next questions; Not helpful "
         "takes them back). Not listed with the learned answers"),
    Spec("learn.llm_descriptions", True, "bool", "Ask the LLM to describe what has no description (marked unverified)"),
    Spec("context.enabled", True, "bool", "Build the Context every night: the system's functional and technical "
         "documentation, from the documents, the catalog, the team memory, the data dictionary and the Helpful answers"),
    Spec("context.hour", 4, "int", "Hour of the nightly Context build (after the day's learning run)"),
    Spec("context.max_llm_calls", 12, "int", "LLM calls of a Context build at most (its summary pages: only the ones "
         "whose sources changed are written again; the facts pages need no LLM)"),
    Spec("learn.agent_catalog", True, "bool", "The agent adds catalog entries when the evidence is certain: "
         "formulas used in answers confirmed as helpful, team rules and facts approved by an admin, definitions "
         "quoted word for word from the documents. Written as (agent): edit one to take it over; delete it and the "
         "agent never writes it again"),
    Spec("learn.agent_catalog_docs", True, "bool", "... and reads the definitions of the documents (the LLM, once "
         "per new or changed document)"),
    # ---- knowledge search (retrieval): words (PostgreSQL full-text) and vectors (embedding model)
    Spec("search.enabled", True, "bool", "Give the agent the knowledge relevant to each question (dictionary, "
         "notes, rules, learned answers, memories, documents)"),
    Spec("search.top_k", 6, "int", "Pieces of knowledge given with each question"),
    Spec("search.prompt_chars", 2500, "int", "Characters of knowledge given with each question at most"),
    Spec("embed.model", "", "str", "Embedding model (e.g. bge-m3); empty: search by words only"),
    Spec("embed.base_url", "", "str", "Embedding API base (what comes before /embeddings); empty: the LLM's "
         "(llm.base_url), with the LLM's authentication (token or middleware)"),
    Spec("embed.batch", 16, "int", "Texts per embedding request"),
    Spec("embed.per_run", 2000, "int", "Pieces embedded per indexing run at most (the next run continues)"),
    Spec("search.vector_store", "database", "choice", "Where the vectors are searched: database (Superset's "
         "database, compared in memory: no extension, no service) or qdrant (a Qdrant server)",
         choices=("database", "qdrant")),
    Spec("qdrant.url", "", "str", "Qdrant server (search.vector_store = qdrant), e.g. http://qdrant-host:6333"),
    Spec("qdrant.api_key", "", "str", "Qdrant API key", secret=True),
    Spec("qdrant.collection", "supagent", "str", "Qdrant collection"),
    # ---- documents and sites
    Spec("docs.allowed_domains", [], "list", "Domains the agent may fetch pages from (e.g. wiki.company.com); "
         "empty: public sites only (no private addresses)"),
    Spec("docs.max_kb", 2048, "int", "Largest page or file read (KB)"),
    # ---- memory learned from the chats
    Spec("memory.enabled", True, "bool", "Learn preferences, rules and facts from the chats"),
    Spec("memory.team_approval", True, "bool", "Team memories need an admin's approval before they are used "
         "(personal ones are used at once)"),
    Spec("memory.prompt_chars", 2000, "int", "Characters of memories given with every question at most (rules, "
         "then preferences, then facts; the facts left out are still found by the knowledge search)"),
    # ---- other agents (MCP server mode)
    Spec("mcp.user", "", "str", "Superset user the MCP server mode (`superset supagent mcp`, for other agents) "
         "acts as; empty: MCP_DEV_USERNAME of superset_config.py"),
    # ---- files, e-mails
    Spec("tools.export_dir", "~/superset-exports", "str", "Where images and Excel files are written"),
    Spec("tools.export_max_rows", 500000, "int", "Rows of an Excel extract at most"),
    Spec("tools.email_allowed_domains", [], "list", "E-mail domains the agent may send to (empty: any)"),
    Spec("tools.keep_days", 7, "int", "Days the files of the answers (images, Excel) are kept"),
    Spec("chats.keep_days", 0, "int", "Delete the chats nobody used for this many days (0: keep every chat); "
         "what they taught (learned answers, memory, associations) stays"),
    Spec("tools.max_file_mb", 50, "int", "Files bigger than this (MB) are not kept for the chat page"),
    # ---- where answers are computed
    Spec("agent.executor", "auto", "choice", "Where questions are answered: celery (Superset's workers), "
         "thread (the web server), auto (celery when a worker answers, else thread)",
         choices=("auto", "celery", "thread")),
    Spec("agent.celery_queue", "", "str", "Celery queue of the answers and the learning runs (empty: the default "
         "queue); set it when the workers only read named queues (celery worker -Q ...)"),
    Spec("agent.queue_keep_days", 90, "int", "With Celery's queue in Superset's database (broker_url \"sqla+...\"): "
         "delivered messages are deleted after this many days (0: never; Celery itself never deletes them)"),
]
BY_KEY = {s.key: s for s in SPECS}


def _env_name(key: str) -> str:
    return "SUPAGENT_" + key.upper().replace(".", "_")


def _coerce(spec: Spec, value: Any) -> Any:
    if value is None:
        return spec.default
    if spec.kind in ("str", "choice"):
        v = str(value)
        if spec.kind == "choice" and v not in spec.choices:
            raise ValueError(f"{spec.key}: one of {', '.join(spec.choices)}")
        return v
    if spec.kind == "int":
        return int(value)
    if spec.kind == "float":
        return float(value)
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if spec.kind == "list":
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        text = str(value).strip()
        if text.startswith("["):
            return [str(v) for v in json.loads(text)]
        return [v.strip() for v in text.split(",") if v.strip()]
    if spec.kind == "json":
        return value if isinstance(value, (dict, list)) else json.loads(str(value) or "null")
    return value


def get(key: str) -> Any:
    spec = BY_KEY[key]
    from supagent.models import Setting
    from superset import db

    try:
        row = db.session.get(Setting, key)
    except Exception:  # pylint: disable=broad-except  (tables not created yet)
        db.session.rollback()
        row = None
    if row is not None:
        if spec.secret and row.secret:
            return row.secret
        if not spec.secret and row.value is not None:
            return _coerce(spec, row.value)
    from flask import current_app

    conf = current_app.config.get(_env_name(key)) if current_app else None
    if conf is not None:
        return _coerce(spec, conf)
    env = os.environ.get(_env_name(key))
    if env is not None:
        return _coerce(spec, env)
    return spec.default


def set_value(key: str, value: Any, by: str = "") -> None:
    """Store a setting (value None: back to the default)."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"unknown setting {key!r}")
    from supagent.models import Setting
    from superset import db

    row = db.session.get(Setting, key) or Setting(key=key)
    if spec.secret:
        row.secret = None if value in (None, "") else str(value)
        row.value = None
    else:
        row.value = None if value is None else _coerce(spec, value)
    row.updated_by = by
    db.session.merge(row)
    db.session.commit()


def describe() -> list[dict]:
    """Every setting with its current value (secrets: only whether they are set)."""
    out = []
    for spec in SPECS:
        value = get(spec.key)
        out.append({"key": spec.key, "kind": spec.kind, "help": spec.help, "choices": list(spec.choices),
                    "secret": spec.secret, "default": None if spec.secret else spec.default,
                    "value": (bool(value) if spec.secret else value)})
    return out
