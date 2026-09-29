# supagent: the AI agent inside Apache Superset

supagent is a Python package that you install in Superset's virtualenv, like a database driver.
It adds:

* **a chat** (the **Chat** tab of Superset's top bar opens it as a panel on the right of the page:
  the dashboard, chart, dataset or SQL Lab page stays beside it, and a link in an answer opens
  there while the chat stays open; Ctrl+click on the tab, or the panel's ⤢ button, opens the full page).
  A user asks a question in plain words; the agent
  works with **that user's Superset permissions**, runs the queries, and answers with the key
  figures. Every query result is shown under the answer as a **table and a chart** (bars for
  rankings, lines over time, figures for a single row). The user can switch between them,
  **copy** the rows, the SQL or the answer, and **download** CSV, Excel or a PNG of the chart.
  The agent also makes **Excel extracts** (a download button in the chat), **screenshots** of
  saved charts and dashboards (shown in the chat), e-mails, scheduled reports and Superset
  charts. It can be stopped at any time, and an answer marked *Helpful* becomes a learned
  answer for similar questions.
* **a data dictionary learned every day** (*Settings → Data dictionary*, and a tab of the chat page), stored in
    Superset's own database:
    * every OpenSearch index and field (through osagg): its type, values, ranges, fill rate and
      time range;
    * every Prometheus / Mimir metric (through promagg): its type (counter, gauge, histogram,
      summary), unit, meaning, series count, data range, typical values, and its labels with
      their values;
    * **how they relate**: metric labels that hold the same values as index fields (label `node`
      = jobs `NODE`, measured), join keys between indices, metric families (histogram
      `_bucket` / `_sum` / `_count`), metrics that share labels;
    * **what changed** since the day before: new, gone or back objects, changed types or units,
      big changes in series counts.

    Descriptions come from your catalog first, then from the source (the exporters' HELP
    texts), then from the LLM. LLM texts are marked *AI-written* until an admin approves or
    corrects them.

* **learning from the chats** (0.2): the answers users mark *Helpful* (the final query, under a
  short generic question; an admin confirms or rejects them), how long each kind of query
  takes, and what users ask to remember (preferences for them, rules and facts for the team).
  A similar question later starts from what worked.
* **knowledge search** (0.2): every question comes with the few pieces of knowledge that match
  it (dictionary, catalog, learned answers, memory, your documents and sites), found by words
  (PostgreSQL full-text search) and by meaning (an embedding model such as BGE-M3). No
  database extension: the vectors are kept in Superset's database, or in Qdrant.
* **a catalog in separate entries** (0.2): title, classification, category and content, each
  edited on its own with its history. The agent adds entries itself when the evidence is
  certain (formulas of answers marked Helpful, approved team rules, definitions quoted from your
  documents), marked as written by the agent.
* **a settings page for admins** (*Settings → Chat settings*): the LLM, including company
  token middleware; the daily learning; knowledge search, memory, documents and sites; the
  catalog entries; the learning runs.

Everything is Python, HTML and JavaScript served by Superset itself: there is no JavaScript
build, no external script, no Docker and no extra service. The pages work with Superset's
Content-Security-Policy (Talisman nonces).

## Requirements

* Apache Superset 6.x in a Python 3.10–3.12 virtualenv. Tested with 6.1.0 on PostgreSQL
  (production-like, with Celery workers and beat) and on SQLite; the test suite also passes on
  6.0.0. supagent needs `pydantic>=2.8`, which Superset 6.1 already has; on 6.0 pip installs it
  (offline: the small `pydantic` wheelhouse delivered with supagent).
* Saving Superset charts and dashboards from the chat uses Superset 6.1's own MCP service
  (package `fastmcp`, which Superset 6.1 installs with its `mcp` extra). Without it everything
  else works; the agent says that it cannot save charts.
* The learner reads OpenSearch through **osagg** and Prometheus / Mimir through **promagg**.
  Both are optional: supagent learns the databases of these two kinds.
* The chat answers in Superset's **Celery workers** when they run (recommended); without a
  worker it answers in a thread of the web server. The daily learning is started by Superset's
  **Celery beat**; without beat, run `superset supagent learn` from cron.
* An OpenAI-compatible LLM with tool calls (llama.cpp `--jinja`, vLLM, a company gateway...).
* Optional: an OpenAI-compatible **embedding** endpoint (`/embeddings`, e.g. BGE-M3 behind the
  same gateway) for search by meaning. Without it the search uses words only. No `pgvector`
  or other database extension is needed.

## Install (pip only)

```bash
# the Python of Superset's virtualenv
PY=$(head -1 "$(command -v superset)" | sed 's/^#!//')
$PY -m pip install supagent-0.4.4-py3-none-any.whl          # Superset 6.1: nothing else to install
# Superset 6.0 offline: add  --find-links ./wheelhouse-pydantic  (pydantic is not in 6.0)
```

One line in `superset_config.py` registers it. It holds no logic:

```python
from supagent import init_app as FLASK_APP_MUTATOR
```

If your config already has a `FLASK_APP_MUTATOR`, chain it:
`import supagent; FLASK_APP_MUTATOR = supagent.chain(my_mutator)`.

Then, once, and again after each upgrade of the package:

```bash
superset supagent init        # tables supagent_*, permissions, role "AI Agent" (idempotent)
superset supagent grant alice bob     # or give the role "AI Agent" in Superset's user list
```

Restart the web server, the Celery workers and beat.

The role **AI Agent** gives the chat and the data dictionary; the settings stay with the
Admin role. `superset init` never gives these pages to Gamma or Alpha. What a user can query
through the agent stays what Superset lets that user query.

## Connect the LLM

Use *Settings → Chat settings → LLM* (the **Test the LLM** button asks the model for one
word), or the command line:

```bash
# a server without authentication (e.g. llama.cpp in the same network)
superset supagent settings --set llm.base_url=http://llm-host:8080/v1

# a fixed access token
superset supagent settings --set llm.auth=token --set llm.token=...

# a company middleware that issues short-lived tokens (OAuth2 client credentials)
superset supagent settings \
  --set llm.base_url=https://llm-gateway.example/openai \
  --set llm.model=qwen3.6-27b \
  --set llm.auth=middleware \
  --set llm.middleware.token_url=https://middleware.example/oauth2/token \
  --set llm.middleware.consumer_key=... \
  --set llm.middleware.consumer_secret=... \
  --set llm.middleware.cert_path=/etc/superset/agent-client.pem \
  --set llm.middleware.key_path=/etc/superset/agent-client.key \
  --set llm.ca_bundle=/etc/superset/company-ca.pem
superset supagent test-llm
```

In **middleware** mode supagent does what the company code does. It sends
`POST <token_url>` with `grant_type=client_credentials` (plus `scope` if set), with the
consumer key and secret as HTTP Basic and the client certificate (mutual TLS). It reads
`access_token` and `expires_in` from the answer, then calls
`<base_url>/chat/completions` with `Authorization: Bearer <token>`. The token is reused
until `llm.middleware.renew_before` seconds (default 60) before it expires. A 401 from the
LLM renews it once. One token is shared by the threads of a process.

Secrets (the token and the consumer secret) are stored encrypted with Superset's
`SECRET_KEY` and are never shown again. Every setting can also come from `superset_config.py`
or from the environment, using the key in upper case with `SUPAGENT_` in front
(`SUPAGENT_LLM_MIDDLEWARE_CONSUMER_SECRET`). The admin page wins over the config, and the
config wins over the environment.

Reasoning models: `llm.thinking` (default off) sends `chat_template_kwargs.enable_thinking=false`
(Qwen 3 and similar). If a gateway refuses that field, supagent stops sending it.

## The daily learning

**One run per day**, at `learn.hour` (server time, default 02:00) on the days of `learn.days`
(default every day). Celery beat ticks every hour only to catch up: a day that has no run yet
gets it at the next tick, after an install or an outage too. Nothing is learned every N hours.

A run is gentle with Mimir and OpenSearch, because production has millions of series and
billions of documents:

* **Cheap daily pass**: the list of metrics with their type, unit and HELP text (one metadata
  request), the list of indices with their mappings. New, gone and changed objects are found
  this way every day.
* **Rolling profiles**: the statistics (series count, data range, typical values, label and
  field values, fill rates) are refreshed for each metric or index every
  `learn.profile_every_days` days (default 7), about a seventh of them each day, plus the new
  ones. **It is never a full scan every day**: a metric is profiled once, then again on its own
  day of the cycle. A profile costs about one request of its own (0.2.4): the series counts of
  50 metrics come from one query, the labels of metrics with a few series from one shared
  series request, and each metric has one combined statistics query. Metrics above
  `learn.stats_max_series` series get no value statistics. The start of the data (the depth of
  the history) is looked up with the time left, about 8 label-index requests per 50 metrics,
  then once a month.
* **Thousands of metrics**: the first profiles take a few runs. A run that reaches
  `learn.max_minutes` is marked **partial** on the settings page ("stopped at the time limit:
  the next run continues"); the next run starts with the metrics not profiled yet. To go
  faster the first time, run it once at night with more time
  (`superset supagent learn --database "<name>" --minutes 240`), leave out what nobody asks
  about (`learn.metrics_exclude`: `go_*`, `process_*`, `promhttp_*`...) and, if the Mimir team
  agrees, raise `learn.max_requests_per_minute`.
* **Dated and rolled-over indices** (`logs-2026.09.27`, `traces-000123`) are learned as one
  family: the newest member is profiled, the family keeps the pattern.
* **Field statistics** are batched (`learn.fields_per_request` per request) on a sample of
  `learn.sample_docs` documents per shard.
* **Limits**: at most `learn.max_requests_per_minute` requests per minute to each database, one
  at a time, each with `learn.request_timeout` seconds; after `learn.stop_after_errors`
  overload errors in a row (429, 503, timeouts, circuit breakers) the run leaves that database
  for the day. A run stops after `learn.max_minutes` (default 30); the next one continues.

The databases are learned one after the other, and the **AI descriptions are written during the
learning**: while the learner reads the databases (slowly on purpose, one request at a time),
the LLM describes what has none yet, as the objects are learned: the catalog first (what people
wrote always wins), then metrics and indices, fields and labels (100 objects at a time, 10 per
LLM call, each call saved at once; a label is described once per database for every metric that
has it; exporters' HELP texts and catalog texts count as descriptions; marked *AI-written*). A
database of thousands of metrics that takes two hours to learn gets its descriptions during
those two hours, not after them. Every osagg and promagg database is learned, except the ones
`learn.databases` leaves out and the ones the learning user (`learn.user`) may not read: each
run lists those, with the reason (settings page, and `superset supagent learn --plan`). While a
run is going, the runs list shows how many objects it learned and described so far.

Once every database is read, it measures the **relations** between them all (a metric label and
an index field hold the same values; the evidence is kept; a relation an admin marked **Wrong**
in *Data dictionary → Relations* is never measured again nor given to the agent, and a catalog
entry of classification *relationships* states the right one), applies the **catalog**, describes
what is still missing with the time left (the run's time plus ten minutes; the next run goes on
where it stopped), lets the agent write the **catalog entries it is certain of** (below), and
updates the knowledge search.

**Stopping a run**: *Stop learning* on the settings page (or `superset supagent learn --stop`)
stops the running run at its next step (a few seconds, or the LLM request in progress); it keeps
what it learned, and *Learn now* starts a new one as soon as it stopped.

**Starting again**: `superset supagent forget-learned` shows what the learning learned, per
database (without `--yes` nothing changes); `--yes` forgets it: the indices, fields, metrics,
labels and families with their statistics and AI-written descriptions, the measured relations
and the history of changes, for every database or those given with `--database`. The next run
(`superset supagent learn`, or the daily one) learns them again as new. Kept: the catalog
entries (applied again), the learned answers, query timings, memory, documents, chats and
settings, and what admins did in the Data dictionary page (descriptions written or approved
there, synonyms, relations marked Wrong: those objects stay, with their learned facts cleared);
`--everything` forgets that too.

`superset supagent learn --plan` tells what today's run would do, without reading any data:
objects due, requests, minutes at the rate limit. **Learn now** on the settings page, or
`superset supagent learn`, runs one at once. Limit what is learned with `learn.indices` /
`learn.indices_exclude` and `learn.metrics` / `learn.metrics_exclude` (patterns with `*`).

Lab measurement (Raspberry Pi 4, first 0.2 run): 6 indices and 95 fields, 28 metrics with 94
labels, and a federated database of 4 tenants with 35 metrics. The lab's metrics stopped on 25
September, so the end of each metric's data was searched in the series index, and its history
checked once: 5 requests to OpenSearch and 1,451 to Mimir (about 23 per metric), at 60 per minute,
no error; 27 minutes with 40 LLM descriptions. The history is checked again only once a month,
and a metric whose data stopped now costs about 6 requests when its profile is due (an estimate:
that path has not run in the lab yet, nothing being due for 7 days); a live metric about 3.

## The catalog

What people know about the data, kept as **separate entries** so that one edit never touches
the rest: the glossary, one entry per index, groups of metrics, relationships, health checks,
rules the agent always follows, notes and runbooks, formulas. Each entry has a title, a
classification, a category and its content (YAML for the structured ones, checked with the
line and column of an error before it is saved). Every change is kept and can be restored;
deleting is soft; two admins editing the same entry cannot overwrite each other.

When two entries define the same index, metric, term or check, the most recent change wins,
except that **a person's entry always wins over the agent's**; the settings page lists such
conflicts. `superset supagent import-catalog catalog.yaml` splits a whole catalog (the format
of the osagg bundle's `catalog.yaml`) into entries (`--replace` also deletes the structured
entries the file does not have); `export-catalog` merges them back into one YAML. Upgrading
from 0.1 splits the former single catalog into entries once and keeps it as a backup.

### Entries written by the agent

With `learn.agent_catalog` (on by default) the agent adds entries itself, **only on evidence
it can check**, never on its own judgement:

* **formulas**: a calculated column (name = expression, on a table) of answers confirmed with
  *Helpful*: the same expression in two confirmed answers (or one, used three times), never
  another expression under that name, never an answer marked *Not helpful*. Plain reads
  (`COUNT(*)`, `SUM(bytes)`, `ROUND(AVG(x), 2)`) are not formulas. A formula learned on a
  database is found only by the users who may query that database;
* **team rules and facts** an admin approved in the team memory: they move into rule and note
  entries (and leave the memory, so the prompt carries them once);
* **definitions from your documents** (`learn.agent_catalog_docs`): the LLM points at the
  sentences that define a term; an entry keeps only a sentence that is word for word in the
  document and reads as a definition ("X is the...", "X means...", "X: ...", a glossary table
  row). The value is the document's own sentence, never the LLM's words. One glossary entry
  per document, read once per new or changed content.

They are marked **agent** in the list (a filter shows them), and each shows its evidence (the
answers, the approval, the document). When the evidence breaks (an answer marked *Not
helpful*, another expression, the document removed), the agent takes its entry back. **Once a
person edits an agent entry it is theirs**: the agent never changes it again; delete it and the
agent never writes it again. `superset supagent agent-catalog` runs this pass at once.

## How the agent finds the data (0.3)

Before the LLM starts, supagent looks for **where the data of the question is**: the metrics,
indices and fields whose names (split on `_ : . -`), HELP texts, descriptions and synonyms share
words with the question (built-in synonyms such as cpu / processor, mem / memory,
es / elasticsearch, and French words), and the ones earlier answers read for the same words. The
best ones are given to the agent with their database id, type, unit, labels and a SQL to adapt,
so that a question like "the CPU of the Elasticsearch cluster over the last 12 hours" goes
straight to the right metric. The live list of metric names is used (cached five minutes): it
works while the dictionary is empty or still learning. Only the databases the agent may use
(`agent.databases`, by default the osagg and promagg ones) and the user may query are searched.

The instructions are short and strict (the rules every answer needs; the sections on saving
charts, investigations, files / e-mails / reports and images only when the question asks for
them), and in the chat only the tools the question needs are offered. In the chat the page draws
every query result as a table and a chart: the agent runs the query, it does not make images.

Since 0.4 the instructions and the tools are the same from one question and one user to the
next, and what is found for the question (where the data is, the knowledge, the learned
answers, the user's memories) comes with the question: the LLM server keeps the instructions
and the tools in its prompt cache (llama.cpp, vLLM) instead of reading them again for every
question (in the lab: 3,188 of 4,700 prompt tokens from the cache, the first step of an answer
16 s faster). Words meet across their forms (failed, failure, failing) and a match on a rare
word counts more than one on a word hundreds of names have (elasticsearch against node).

While it answers, the agent:
* is told **why a result is empty**, from the data dictionary (no extra query): a value in the
  wrong case (`'failed' is written 'FAILED'`), not a value of the field or label (the closest
  ones), a time window before the data starts or after it stopped;
* never runs the **same call twice** in an answer (reading again after a save is allowed);
* gets an **empty LLM answer** asked again twice; still empty after a query succeeded, the answer
  shows that result instead of failing;
* is sent back once when its answer **announces a step without taking it** ("Let me run the
  query.");
* gets a one-line **correction** when its answer says all is well while a check or a query it
  relies on could not run;
* compares with **the usual** (`compare_to_usual`, when a question asks whether something is
  unusual, and in investigations): the same window on the previous weeks, median and median
  absolute deviation per series, a verdict normal / high / low (unknown with fewer than 3 weeks).

## Learning from the chats

* **Learned answers** (0.2.2): only an answer marked *Helpful* is learned: its final successful
  query (SQL, PromQL or chart), with its time and size, under a short generic question the
  agent writes (no ids, dates or names of one case; the user's own words are not kept). If the
  agent finds the same question already learned, the answer joins it (one more *Helpful*)
  instead of making a duplicate. It is listed as *helpful, to review*; an admin confirms or
  rejects it in *Data dictionary → Learned answers*. *Not helpful* (or taking *Helpful* back)
  withdraws it, unless an admin confirmed it. A similar question of anyone in the team starts
  from the confirmed ones first, then the helpful ones, on the databases the user may query.
  The first answer of a chat names it the same way (a 2-6 word generic title).
* **Where the data was** (0.3, `learn.associations`): after each answer, the words of the
  question and the metrics or indices its successful queries read; the next questions with those
  words find them first. *Not helpful* takes them back; one that sent the agent to data that was
  not there (an error, no rows, while the answer came from elsewhere) loses a use; one no answer
  used for 60 days, or to a metric or index that is gone, is not used. They are not listed with
  the learned answers; `learn.associations = false` switches this off. Learned answers and
  memories that name a metric or index that no longer exists are not given to the agent.
* **What users state**: a message that tells something about the data ("KO means failed") is read
  for durable facts and rules like the explicit ones below (team ones wait for an admin).
* **Query timings** per kind of query (values replaced by `?`) and table or metric: the agent is
  told which way is fast. The page shows each query whole, with its last error; admins also
  see the last one as it ran.
* **The limits of osagg and promagg**: they are not full SQL engines. The agent is told what
  each can run (in its instructions, and next to each database it lists): filters and
  aggregates pushed down, the latest per key with `GROUP BY` + `MAX`, pairs of values as
  `(A = x AND B = y) OR ...`, no subquery, `WITH`, window function or self-join over raw rows,
  work in steps with `WHERE key IN (...)`. A query osagg cannot push down may read at most
  `agent.osagg_max_scan_rows` raw documents (20,000): above, osagg refuses it at once with the
  reason (it counts before reading), and the refusal tells the agent how to rewrite it.
* **Answers come from the tools**: the knowledge given with a question is a summary, never an
  answer. An answer written with no tool call, or showing results (a JSON block, a table of
  numbers, "SQL run") that no query returned, goes back to the model once to be done with the
  tools. `SUM(value)` or `AVG(value)` of a counter (its value is cumulative) is refused before it
  runs, with `rate` / `increase` and the catalog's formulas for that metric.
* **Big results** reach the LLM as a summary (row count, columns, the first 25 rows, min / max /
  average / sum of the numbers, the most frequent values) unless the question asks for every row; the user still gets every row in the table, chart and files.
  Reading a metric of more than 50,000 series without aggregation is refused with advice.
* **Memory**: when a question says *always*, *from now on*, *remember*, *by default*,
  *toujours*, *désormais*, *retiens*... or an answer is marked *Helpful*, the LLM extracts the
  durable points: a user's preferences (used at once, in that user's answers only) and the
  team's rules and facts (used after an admin approves them, `memory.team_approval`). A message
  that tells something ("STATUS_INFO = KO means the job failed") is read the same way. Every
  question gets at most `memory.prompt_chars` characters of them (rules, then preferences, then
  facts); the facts left out are still found by the knowledge search. Users see and delete
  theirs with the chat's *Memory* button; admins review the team's on the settings page.

## Measuring it

Nothing is added to the pages; the commands say:

* `superset supagent stats [--days 7]`: where the time of the answers goes (LLM and tools), the
  LLM calls and tool calls per answer, the prompt sizes, the share the LLM server's prompt cache
  saved, the slowest answers.
* `superset supagent evaluate`: does "Where the data is" find the data of the answers users
  marked *Helpful* (hit@1, hit@3, mean reciprocal rank, and the ones it misses)? Also measured
  after each learning run (in its statistics): it tells whether the learning improves.
* `superset supagent gaps [--days 30]`: the questions not answered well (marked *Not helpful*,
  failed, no data found for their words) and the learned answers about data that is gone: what to
  add to the dictionary (synonyms, descriptions) or the catalog.
* `superset supagent test-llm --profile`: the LLM server's answer time with and without thinking,
  tool calls, and whether its prompt cache works.

## Knowledge search

Every question comes with the `search.top_k` pieces of knowledge that match it best (at most
`search.prompt_chars` characters), and the agent can search more (`search_knowledge`). The
pieces are the learned metrics and indices, the catalog's rules, notes, glossary and formulas,
the learned answers, the memories and the documents. They are found:

* by **words**: PostgreSQL full-text search, built in (other databases: counted in Python);
* by **meaning** when `embed.model` is set: vectors of an OpenAI-compatible embedding endpoint
  (e.g. `bge-m3`, named exactly as the gateway lists it; `embed.base_url`, empty: the LLM's; same
  authentication as the LLM, middleware included: tested in the lab through a mock of the company
  middleware), stored in Superset's
  database as float16 (2 KB per piece with 1,024 dimensions) and searched in memory, or in a
  **Qdrant** server (`search.vector_store = qdrant`, `qdrant.url`);

and the two rankings are fused. A piece found by meaning only must be close enough (cosine 0.35
at least, and within 0.2 of the closest piece): a weak neighbour is noise, not knowledge; each
result of `search_knowledge` says which search found it. A user only finds what they may see: pieces about databases
they may query, the team's pieces and their own memories; the filter is applied before
ranking. Pieces follow their origin (a changed entry is indexed again, a deleted one removed);
new vectors are computed after each learning run, `embed.per_run` at a time.

## Documents and sites

Admins add documents (text, Markdown or HTML files) and web pages or whole sites (the pages
under the same address, up to a number of pages, read again every N days) on the settings page.
Fetching is safe by default: http(s) only, `docs.max_kb` per page, 20 s per request, redirects
checked; only `docs.allowed_domains` are read (with none set, only public addresses: no
intranet, no localhost). PDF is not read (it would need a package Superset does not have).

## Where things are kept

Tables in Superset's database. They have no foreign key to Superset's tables, so deleting a
user or a database is never blocked. `superset supagent init` creates them and adds the
columns of newer versions.

| table | holds |
|---|---|
| supagent_setting | settings (secrets encrypted) |
| supagent_source, supagent_object, supagent_relation | the learned dictionary |
| supagent_run, supagent_change | learning runs and what they found different |
| supagent_conversation, supagent_message | the chats (each user sees only their own) |
| supagent_file | images and Excel files of the answers (kept `tools.keep_days`, default 7) |
| supagent_example | questions answered well (*Helpful*) with their SQL |
| supagent_entry, supagent_entry_version | the catalog entries and every version of them |
| supagent_recipe, supagent_query_stat | learned answers, query timings |
| supagent_memory | preferences, rules and facts (personal or team) |
| supagent_doc | documents and sites (their text) |
| supagent_chunk | the searchable pieces of knowledge (text and vector) |
| supagent_document | the 0.1 catalog, kept as a backup after the upgrade |

Files are kept in the database so that the web server can serve what a worker on another
host wrote. Files bigger than `tools.max_file_mb` (default 50 MB) are named but not kept.

## Command line

```
superset supagent init                 tables, permissions, role "AI Agent"
superset supagent settings [--set k=v] [--unset k]
superset supagent test-llm [--profile]                                  the LLM (--profile: thinking, tools, cache)
superset supagent stats [--days N] | evaluate | gaps [--days N]         where the time goes, resolver, gaps
superset supagent learn [--database NAME] [--no-llm] [--minutes N] [--plan] [--stop]
superset supagent import-catalog FILE [--replace] | export-catalog
superset supagent agent-catalog [--no-docs]                              entries the agent is certain of
superset supagent tidy-learned [--limit N]                               generic questions, duplicates merged
superset supagent forget-learned [--database D] [--everything] [--yes]    learn again from scratch (dry run
                                                                          without --yes)
superset supagent remove-auto-learned                                    answers 0.2.1 saved by themselves
superset supagent index [--refresh-docs]                                 searchable pieces and vectors
superset supagent search "words" [--user U]                              what the agent would find
superset supagent knowledge [--changes DAYS]
superset supagent describe "words" [--name INDEX_OR_METRIC] [--user U]   what the agent reads
superset supagent ask "question" --user U                                 an answer in this process
superset supagent grant USER...
superset supagent push-descriptions [--labels] | push-metrics            catalog -> datasets
superset supagent mcp [--host 127.0.0.1 --port 5009]                     the tools for other agents
```

## Other agents (MCP)

`superset supagent mcp` serves the same tools over MCP (streamable HTTP) for another agent. It
acts as the Superset user `mcp.user` (default `MCP_DEV_USERNAME`). Keep it on localhost or
behind your gateway.

## Operations

* **Where answers run**: `agent.executor` = `auto` (Celery when a worker answers, else a
  thread of the web server), `celery` or `thread`. An answer with no progress for 35 minutes
  is marked as failed, so a restarted worker never locks a conversation.
* **Many users at once**: every question runs on its own (a Celery task, or a thread of the web
  server); there is no one-at-a-time limit in supagent, and a running answer holds no
  connection of Superset's database pool while it waits for the LLM or a query. What limits
  answers in parallel is: the LLM server (how many requests it serves at once: llama.cpp
  `--parallel`, vLLM batches, a gateway's quota per client); the Celery workers'
  `--concurrency`; without Celery, the web server's workers and threads (gunicorn
  `-w 4 -k gthread --threads 8`: heavy queries of the agent then share the web server's
  processes, so give it several workers).
* **Page files after an upgrade**: Superset lets browsers keep static files for a year
  (`SEND_FILE_MAX_AGE_DEFAULT`); since 0.2.4 every CSS and JavaScript file of supagent has its
  content hash in its URL, so an upgrade is seen at once, with no hard reload. (Before 0.2.4 a
  browser could keep the former files: a half-dark page, or former fixes missing; Ctrl+F5 once.)
* **People first**: the background LLM work (the daily learning's descriptions, learned answers
  and memory from a *Helpful*) waits while answers are being computed (2 minutes at most per
  call), so that the LLM serves the people waiting first.
* **Old chats**: `chats.keep_days` (0, the default: keep every chat) deletes the chats nobody
  used for that many days, with their messages and files; what they taught stays.
* **The chat panel** on Superset's pages is added through Superset's own place for custom page
  scripts (`tail_js_custom_extra.html`; what a deployment put there is kept), only for the users
  who may chat, never on embedded or standalone dashboards. It shows the chat page in a frame of
  the same site: Talisman's default `frame_options` (SAMEORIGIN) allows it; with DENY the panel
  says so and offers the full page. Drag its left edge to resize it; it stays open from page to
  page in the browser tab.
* **Stop**: the chat's Stop button stops the answer at once: the chat takes the next question
  right away. A step already running (an LLM call, a query) ends on its own in the
  background; its result is thrown away and the agent does nothing more for that answer.
* **Upgrade**: `pip install` the new wheel, `superset supagent init`, then restart. From 0.1:
  `init` adds the new tables and columns and splits the catalog into entries (the former
  catalog is kept as a backup). From 0.2.0 / 0.2.1: learned answers users marked *Helpful*
  wait for an admin's review; the ones those versions saved by themselves are no longer
  listed nor used (`init` says how many; `superset supagent remove-auto-learned` deletes them).
* **Uninstall**: remove the config line and restart. The tables stay until you drop them
  (`supagent_*`).
* **Logs**: logger `supagent` (answers, learning runs); the runs are also on the settings page.
* **Metadata database not UTF-8** (PostgreSQL created with LATIN1, MySQL without
  `?charset=utf8mb4`): supagent stores what the database can hold (– becomes -, ’ becomes ',
  accents of the encoding are kept) and logs it once at start. For full Unicode, the metadata
  database has to be UTF-8.
