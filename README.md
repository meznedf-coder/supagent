# supagent: the AI agent inside Apache Superset

supagent is a Python package that you install in Superset's virtualenv, like a database driver.
It adds:

* **a chat** (the **Chat** tab of Superset's top bar opens it as a panel on the right of the page:
  the dashboard, chart, dataset or SQL Lab page stays beside it, and a link in an answer opens
  there while the chat stays open; docked, it leaves the page 900 pixels at least so a dashboard
  keeps all its charts; the panel can also float over the page, moved and resized freely (0.8),
  as it does by itself in a window under 1,220 pixels; Ctrl+click on the tab, or the panel's ⤢
  button, opens the full page).
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

    Its tabs: *To review* (what waits for an admin), *Knowledge* (the catalog, the team memory,
    the documents and sites, the notes, the Context, what the agent learned, the categories and
    the **System map**: edited there by admins, read only for the others), *Data* (browse,
    relations, changes) and *Search* (see *The Data dictionary* below). Everyone sees only what
    concerns the databases they may query. Each part's explanation is behind an **i** (0.8).

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
  database extension is needed: the vectors are kept in Superset's database, or in Qdrant.
  With the PostgreSQL extensions pgvector, pg_trgm and pg_textsearch, the **knowledge store**
  (0.6, optional) searches in PostgreSQL itself: BM25, near spellings, meaning, and each
  user's own chats.
* **a router** (0.6): before each answer, one short LLM call chooses the kind of work the
  question needs (functional, technical, incident, charts, observability, infrastructure) from
  its meaning and the team's knowledge, and gives that kind's knowledge and tools first; not
  sure, the normal way. The knowledge is **classified** daily by the LLM (subjects,
  applications, components, relations), an admin reviewing only what is new or unsure.
* **a catalog in separate entries** (0.2): title, classification, category and content, each
  edited on its own with its history. The agent adds entries itself when the evidence is
  certain (formulas of answers marked Helpful, approved team rules, definitions quoted from your
  documents), marked as written by the agent.
* **a settings page for admins** (*Settings → Chat settings*): the LLM, including company
  token middleware; the daily learning; knowledge search; the learning runs. The catalog, the
  team memory, the documents and sites and the review of what the agent proposes are in the
  **Data dictionary** (0.6).

Everything is Python, HTML and JavaScript served by Superset itself: there is no JavaScript
build, no external script, no Docker and no extra service. The pages work with Superset's
Content-Security-Policy (Talisman nonces).

## Requirements

* Apache Superset 6.1 in a Python 3.10–3.12 virtualenv. Tested with 6.1.0 on PostgreSQL
  (production-like, with Celery workers and beat) and on SQLite. supagent needs
  `pydantic>=2.8`, which Superset 6.1 already has: nothing else to install, offline too.
  A **new** Superset 6.1.0 installed from PyPI today needs three pins to start at all (none is
  about supagent; tested on 2 Oct 2026 on an empty PostgreSQL database):
  `pip install "apache-superset==6.1.0" "flask-caching==2.3.1" "flask-limiter<4" cachetools`.
* Saving Superset charts and dashboards from the chat uses Superset 6.1's own MCP service
  (package `fastmcp`, which Superset 6.1 installs with its `mcp` extra). Without it everything
  else works; the agent says that it cannot save charts.
* The learner reads OpenSearch through **osagg** and Prometheus / Mimir through **promagg**.
  Both are optional: supagent learns the databases of these two kinds.
* The chat answers in Superset's **Celery workers** when they run (recommended), with any
  broker: Redis, RabbitMQ, or a queue in Superset's own database (no extra service: see
  *Celery workers and beat*); without a worker it answers in a thread of the web server. The
  daily learning is started by Superset's **Celery beat**; without beat, run
  `superset supagent learn` from cron.
* An OpenAI-compatible LLM with tool calls (llama.cpp `--jinja`, vLLM, a company gateway...).
* Optional: an OpenAI-compatible **embedding** endpoint (`/embeddings`, e.g. BGE-M3 behind the
  same gateway) for search by meaning. Without it the search uses words only. No database
  extension is needed.
* Optional (0.6): the PostgreSQL extensions `vector` (pgvector 0.7 or later), `pg_trgm` and
  `pg_textsearch` in Superset's database, for the knowledge store (PostgreSQL 17 or 18; tested
  on 18.3 with pgvector 0.8.1 and 0.8.6, pg_textsearch 0.5.0 and 1.4.0; see *The knowledge store
  in PostgreSQL*).

## Install (pip only)

```bash
# the Python of Superset's virtualenv
PY=$(head -1 "$(command -v superset)" | sed 's/^#!//')
$PY -m pip install supagent-0.9.1-py3-none-any.whl          # Superset 6.1: nothing else to install
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

## Upgrade from 0.8 to 0.9.0

`pip install` the new wheel on every host, `superset supagent init` once ("schema version 14 -> 15": three nullable
columns of `supagent_link`, the long explanation of an interaction, where it was found and who explained it),
restart. What starts by itself (the investigations, `compare_logs`, the interactions explained, the classification
after the Context) and the way back to 0.8.2 (three SQL lines): INSTALL.txt, *From 0.8 to 0.9.0*.

## Upgrade from 0.7 to 0.8.0

`pip install` the new wheel on every host, `superset supagent init` once ("schema version 13 -> 15": new nullable
columns, the catalog's notes renamed guides, in the knowledge store too), restart. Steps, checks and the way back
(three SQL lines for the guides): INSTALL.txt, *Upgrade from 0.7 to 0.8.0*.

## Upgrade from 0.5.4 to 0.6.0

The same steps are in `INSTALL.txt`. What changes (each one can be switched off):

* **The router (MOA)**: before each answer, one short LLM call (60 s at most) chooses the kind
  of work. Off: `agent.router=false`.
* **The knowledge store**, when `vector`, `pg_trgm` or `pg_textsearch` is in Superset's
  database: `init` builds it, then the search runs in PostgreSQL. Off: `search.store=off`
  (the search of 0.5.4).
* **The chats** go into the store; each user can search their own (a search box above the
  chats). Off: `search.chats=false`.
* **The Data dictionary**: the catalog, the team memory, the Context, the documents and sites
  are now edited there, no longer in *Settings → Chat settings*. A save answers at once.
* **Categories** of the knowledge: classified by the LLM with the daily learning (400 items a
  day at most, `learn.classify_per_run`).
* Not changed: the classic pipeline answers (`agent.pipeline = classic`); the reranker is off.
* Superset's database: 6 new tables (`supagent_route`, `supagent_facet`, `supagent_tag`,
  `supagent_link`, `supagent_classified`, `supagent_item_use`) and the schema
  `supagent_store`; no existing table or column changes, nothing is deleted.
  `superset_config.py` does not change.

### A. Before the day (nothing changes yet)

**1. PostgreSQL**, connected to Superset's database (`SHOW shared_preload_libraries` needs a
superuser or a role with `pg_read_all_settings`; the two `SELECT`s work for any user, Superset's
too):

```sql
SHOW shared_preload_libraries;
SELECT extname, extversion, extnamespace::regnamespace AS schema FROM pg_extension
 WHERE extname IN ('vector', 'pg_trgm', 'pg_textsearch');
SELECT has_database_privilege('<Superset database user>', current_database(), 'CREATE');
```

* **pg_textsearch 0.5.x in `shared_preload_libraries`**: take it out (or upgrade
  pg_textsearch to 1.x), then restart PostgreSQL, *before* the upgrade. With 0.5.x a
  `ROLLBACK` after an error can fail (`ResourceOwnerEnlarge called after release started`) and
  leave the session stuck; preloaded, that is every session, Superset's own too, with 0.5.4
  already. Not preloaded, it is loaded only by the store's sessions, which never run a
  transaction. 1.x has the fix (and must be preloaded).
* The extensions must be in Superset's database itself (`CREATE EXTENSION` is per database),
  in any schema.
* CREATE privilege `f`: `GRANT CREATE ON DATABASE <Superset database> TO <Superset database
  user>;`, or keep the store off (step 8). Without it `init` says *knowledge store: not built*
  and the search of 0.5.4 answers.

**2.** Check the wheel (`sha256sum -c SHA256SUMS`) and keep the 0.5.4 wheel for a rollback.

### B. The upgrade (Superset stopped for a few minutes)

**3.** Stop the web servers, the Celery workers and Celery beat, on every host. A running 0.5.4
process must not load files of 0.6.0: install while everything is stopped.

**4. Back up:**

```bash
PY=$(head -1 "$(command -v superset)" | sed 's/^#!//')     # the Python of Superset's virtualenv
pg_dump -Fc -f superset-before-0.6.0.dump "<Superset's database>"
$PY -m pip freeze > pip-before-0.6.0.txt
```

**5. Install**, on every host that runs Superset (web servers, workers, beat). No other package
is installed or changed (pydantic is already there); `supagent[graph]` is not needed.

```bash
PY=$(head -1 "$(command -v superset)" | sed 's/^#!//')
$PY -m pip install supagent-0.6.0-py3-none-any.whl
$PY -m pip show supagent | head -2                     # Version: 0.6.0
```

**6. Once, on one host:**

```bash
superset supagent init
```

It prints, among other lines (the rehearsal of this upgrade on a copy of the lab's database):

```
tables: schema version 7 -> 9
knowledge store: built (2064 pieces, 14146 names, pg_textsearch 0.5.0, vectors 1024) in 10.8 s
```

and the warnings to read (e.g. pg_textsearch 0.5.0 is a pre-release: see step 1). *knowledge
store: not used* or *not built* with the reason: the search of 0.5.4 answers; check with
`superset supagent store status`. `init` takes a few minutes: the store (the lab at scale:
10,000 pieces and 380,000 names in 107 s), then the vectors of the last 2,000 chats' questions,
older ones hourly after (the rehearsal: 3 minutes in all, embeddings on a CPU). To keep the stop
short, build it after the start instead:

```bash
superset supagent settings --set search.store=off       # before init
# after step 9, while people work (the search of 0.5.4 answers meanwhile):
superset supagent settings --set search.store=auto
superset supagent store rebuild
```

**7.** The LLM and the embeddings are unchanged; check them from this host:
`superset supagent test-llm`.

**8. Optional:** start with 0.5.4's behaviour and switch the new parts on later:

```bash
superset supagent settings --set agent.router=false --set search.store=off
# back on, later:
superset supagent settings --set agent.router=true --set search.store=auto
superset supagent store rebuild                         # if init did not build it
```

**9.** Start the web servers, the workers and beat.

### C. Checks

**10. Check:**

```bash
superset supagent store status        # extensions, versions, rows; read its "warnings"
superset supagent check-knowledge     # exit code 0: everything the team put in reaches the agent
superset supagent ask --user <admin> "<a question your team often asks>"
```

In Superset: *Chat* (one question; the search box above the chats), *Settings → Data
dictionary* (tabs To review, Knowledge, Data, Learned by the agent, Search); *Settings → Chat
settings* shows "supagent 0.6.0 (tables v9)".

**11. Categories**, once (needs the LLM; then every day with the learning; a later run
continues). Then *Data dictionary → To review*: the new categories, the unsure tags, the "same
as" relations (the tags on a new category come up once the category is approved; `classify`
counts them already). The rehearsal: 93 items in 3 minutes (12 LLM calls), 18 new categories
and 42 tags to review.

```bash
superset supagent classify --minutes 30
```

### D. Roll back to 0.5.4

**12.** Stop everything, then:

```bash
$PY -m pip install supagent-0.5.4-py3-none-any.whl      # on every host
superset supagent init                                  # once: it records schema 7 again
```

and start everything. The new tables and the schema `supagent_store` stay, unused by 0.5.4 (to
drop the store, before going back, with 0.6.0: `superset supagent store wipe --yes`). The
vectors are still in `supagent_chunk`: nothing to embed again. The backup of step 4 is only for
a damaged database (services stopped; as Superset's database user, the errors *must be owner of
extension ...* are expected: the extensions stay; then `superset supagent init` of the version
installed):

```bash
pg_restore --clean --if-exists -d "<Superset's database>" superset-before-0.6.0.dump
```

Back to 0.6.0 later: steps 3, 5, 6, 9, then `superset supagent store sync` (the chats of the
meantime).

### E. Later, optional

pg_textsearch 1.x (then `superset supagent store rebuild`); a cross-encoder reranker
(`rerank.url`, measure it first: see *A cross-encoder reranker*); the governed pipeline and
other MCP servers (`pip install "supagent[graph]"`, see `INSTALL.txt`).

## Celery workers and beat

The web server alone is enough: without a worker, the answers run in threads of the web server,
and the daily learning waits for `superset supagent learn` (cron). With Superset's Celery
**workers**, the answers and the learning leave the web server; Celery **beat** starts the daily
learning (and Superset's alerts and reports).

**The queue.** The web servers and the workers talk through a broker. With Redis, configure it as
Superset's documentation says. Without Redis, Superset's own database can be the queue: in
`superset_config.py`, after the `SQLALCHEMY_DATABASE_URI` line, this is the whole change
(Superset's other Celery defaults are kept):

```python
from superset.config import CeleryConfig as SupersetCeleryConfig

class CeleryConfig(SupersetCeleryConfig):
    broker_url = "sqla+" + SQLALCHEMY_DATABASE_URI
    result_backend = "db+" + SQLALCHEMY_DATABASE_URI

CELERY_CONFIG = CeleryConfig
```

Without a `CELERY_CONFIG`, Superset's default queue is a SQLite file (`celerydb.sqlite`) in the
directory each process starts from: web servers and workers started elsewhere, or on other
hosts, do not see each other. The first start creates the tables `kombu_queue`, `kombu_message`,
`celery_taskmeta` and `celery_tasksetmeta` in Superset's database (a database in LATIN1 is fine:
the messages are ASCII). Celery never deletes a delivered message: supagent deletes them after
`agent.queue_keep_days` days (default 90; about 1 KB each, three months of Superset's reports
scheduler are about 150 MB, and keeping them does not slow the queue down). Celery beat removes
its own old results every night.

**Start them** with the `celery` of Superset's virtualenv, the same `SUPERSET_CONFIG_PATH` and the
same environment as the web server (the worker computes the answers: the LLM middleware's
certificate paths, `SUPAGENT_*`, `EXPORT_*`, `EMAIL_*`...), for example as services with
`Restart=always`:

```bash
celery --app=superset.tasks.celery_app:app worker --pool=prefork -O fair -c 4 --loglevel=INFO
celery --app=superset.tasks.celery_app:app beat --pidfile= -s /path/to/celerybeat-schedule --loglevel=INFO
```

`-c 4`: four answers or reports at once per worker (a learning run keeps one busy while it runs;
each takes about 250-300 MB). Then restart the web servers.

**Several web servers and workers** (behind a load balancer):

* Workers on as many hosts as needed, all with the same configuration, and **one beat** for all
  of them (two beats start everything twice: the learning still runs once, Superset's reports
  would not). The same supagent version on every web server and worker, upgraded together.
* **One question, one answer**: whatever the number of workers and web servers, a question is
  answered once, by the process that starts it first; the others leave it. A worker holds at
  most as many questions as it has processes, so a busy worker leaves the next ones to the others.
* `agent.executor` = `auto` (the default) sends the questions to the workers while one of them is
  alive: every worker writes a heartbeat in Superset's database every 30 seconds (with any broker:
  a queue in a database cannot carry Celery's ping) and removes it when it stops. With no worker
  alive, the web server answers at once; a question no worker started within 15 seconds (every
  worker busy, or one stopped since its last heartbeat) is answered by the web server that
  received it. `celery` sends every question to the workers (questions wait while none runs),
  `thread` never.
* The files an answer made are kept in Superset's database: the next answer finds them on any
  host ("mail me that Excel file"). One learning run at a time for all the hosts.

The settings page says "Celery workers answer (2)" with two workers alive.

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
* **Thousands of metrics**: the first profiles take a few runs. A database that reaches its
  share of `learn.max_minutes` is marked **partial** on the settings page ("stopped at its share
  of the time: the next run continues"); the next run starts with the metrics not profiled yet. To go
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

The databases are learned one after the other, **the ones never learned first**, each with **a
fair share of the time left** (the time left divided by the databases left: a database of
thousands of metrics goes on at the next run instead of keeping the others waiting; a quick one
leaves its time to the next), and the **AI descriptions are written during the learning**: while the learner reads the databases (slowly on purpose, one request at a time),
the LLM describes what has none yet, as the objects are learned: the catalog first (what people
wrote always wins), then metrics and indices, fields and labels (100 objects at a time, 10 per
LLM call, each call saved at once; a label is described once per database for every metric that
has it, and the labels of that name found later take that description without an LLM call, a
person's first; exporters' HELP texts and catalog texts count as descriptions; marked *AI-written*). A
database of thousands of metrics that takes two hours to learn gets its descriptions during
those two hours, not after them. Every osagg and promagg database is learned, except the ones
`learn.databases` leaves out and the ones the learning user (`learn.user`) may not read: each
run lists those, with the reason (settings page, and `superset supagent learn --plan`). While a
run is going, the runs list shows the database being learned and what it does there ("profiles:
1,200 of 4,904 due, 1,300 requests"), the ones still to come ("next: logs, jobs"), the ones left
out with the reason, the new objects found so far and the AI descriptions written meanwhile (LLM
calls and tokens, apart from the ones copied from a label of the same name).

**Steps of a run**: *steps* under each run lists everything it did, with the time it started and
how long it took: per database the objects listed, new, due and profiled, the requests and
errors, the batches read one metric at a time (with their first error), the history lookups, the
changes by type (new, gone, back, type, unit, cardinality) and the search update; then the
descriptions written while reading, the wait for the last ones, the relations, the catalog, the
categories, the descriptions still missing, the agent's catalog entries, the generic questions,
the search index and its embeddings, and the check of "where the data is". A run that is stopped
keeps the step it was doing, marked. Each step is also written in the server log
(`supagent learn: run 9: read the database (Mimir): 4190.2 s {...}`).

Once every database is read, it measures the **relations** between them all (a metric label and
an index field hold the same values; the evidence is kept; a relation an admin marked **Wrong**
in *Data dictionary → Relations* is never measured again nor given to the agent, and a catalog
entry of classification *relationships* states the right one), applies the **catalog**, describes
what is still missing with the time left (the run's time plus ten minutes; the next run goes on
where it stopped), lets the agent write the **catalog entries it is certain of** (below), and
updates the knowledge search.

**Stopping a run**: *Stop learning* on the settings page (or `superset supagent learn --stop`)
marks the running run **stopped at once** (also a run whose process died, e.g. a restart during
the run); it keeps what it learned, and *Learn now* starts a new one right away. The run's own
work ends at its next check, every few seconds at most, also while it waits (for the last
descriptions, or for the people's answers first); a request to a database or an LLM call already
in progress finishes in the background.

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
`learn.indices_exclude` and `learn.metrics` / `learn.metrics_exclude` (patterns with `*`). These say
what the dictionary holds: an index or a metric learned before that a new pattern leaves out is
marked gone at the next run (the agent no longer sees it), not only skipped.

Lab measurement (Raspberry Pi 4, first 0.2 run): 6 indices and 95 fields, 28 metrics with 94
labels, and a federated database of 4 tenants with 35 metrics. The lab's metrics stopped on 25
September, so the end of each metric's data was searched in the series index, and its history
checked once: 5 requests to OpenSearch and 1,451 to Mimir (about 23 per metric), at 60 per minute,
no error; 27 minutes with 40 LLM descriptions. The history is checked again only once a month,
and a metric whose data stopped now costs about 6 requests when its profile is due (an estimate:
that path has not run in the lab yet, nothing being due for 7 days); a live metric about 3.

**Not to be used**: a description written by a person (Data dictionary page, catalog) that says a
field, label, metric or index is not used, at its start or at the start of one of its sentences
("Not used", "Deprecated: take HOST", "This field is no longer used", "Ne pas utiliser", "Ce champ
n'est plus utilisé"...), is honoured (a word inside an explanation, such as "mode idle = unused", is
not such a statement): "where the data is" never proposes it and
lists it as *DO NOT USE* under its index or metric, `describe_data` and the dataset information
mark it, and a query or a chart that uses it is refused with the team's words, so that the agent
takes another field. A description written by the LLM never counts.

## The catalog

What people know about the data, kept as **separate entries** so that one edit never touches
the rest: the glossary, one entry per index, groups of metrics, relationships, health checks,
rules the agent always follows, notes and runbooks, formulas. Each entry has a title, a
classification, a category and its content (YAML for the structured ones, checked with the
line and column of an error before it is saved). Every change is kept and can be restored;
deleting is soft; two admins editing the same entry cannot overwrite each other.

When two entries define the same index, metric, term or check, the most recent change wins,
except that **a person's entry always wins over the agent's**; the catalog (Data dictionary) lists such
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

## Context: the system, functionally and technically

Every night (`context.hour`, 04:00, after the day's learning run) the agent writes the **Context**:
the documentation of your system, in two parts, *Functional* and *Technical* (Data dictionary →
Context). It is written from what the team shares: the documents and sites, the catalog, the team
memory, the data dictionary (databases, domains, relations, and the values of the labels and fields
that name servers, services, applications, environments, regions, clusters and teams) and the
answers marked Helpful. Raw chats are not read: each was answered with its user's own permissions.

* **Facts pages**, written without the LLM: each data source (engine, domains with their counts,
  the 25 indices and metrics that matter most: described by people, in the catalog, used by the
  answers and the learned answers; formulas, links with the other sources), each inventory (servers
  and hosts, services and jobs, applications, environments, regions, clusters, Mimir tenants and
  teams, with their values), the glossary, and the rules and facts of the team. With 10,000 metrics
  and 80,000 labels a page stays short: it never lists every description (the Data dictionary has
  them all).
* **Summary pages**, written by the LLM from the evidence given only, citing it and marked
  *AI-written*: how the system works, the technical overview, and a page per main application
  (what it is, its chain, its data, what people ask about it). A summary page is written again only
  when its evidence changed: a night with nothing new asks the LLM nothing. At most
  `context.max_llm_calls` calls per build (12); the next night goes on. The steps, calls and tokens
  of each build are in the runs list of the settings page (kind *context*); Stop stops it; it never
  runs next to a learning run. `superset supagent context --build` builds it now, *Build now* in the
  Context tab too.
* **Who sees what**: a page is shown only to the users who may query every database it draws from
  (the tab, and the agent's knowledge search, where it ranks below the catalog and the documents).
* **Corrections**: an admin corrects a page in the tab; the agent then never writes over it (until
  *Give it back to the agent*).
* **Read as documentation** (0.8): the contents on the left (functional, technical; a box finds the pages by their
  words), the page on the right with its sources; **Word (.docx) and PDF** of a page or of the whole Context
  (pure Python: a cover, the contents with their page numbers, bookmarks, tables and code kept).
* Code read through MCP servers can become one more source of evidence later.

## Questions about an earlier answer

"Create the chart in Superset of that finding", "the same for srv-emea-001", "enregistre ce
graphique": with the new question, the agent is given what the last two answers of the chat were
computed from: the queries that produced the rows they showed (the tool, the database, the exact
SQL or PromQL, the time window and the columns), each under the question it answered, marked as
the queries that ran (a query only written in an answer's text did not). The earlier answers
themselves are given as they were. When the question refers back ("that", "this finding", "the same", "ce
résultat"...), "where the data is" (below) is looked for with the question it refers to, not only
with its own few words.

A finding that is a calculation (a percentage such as the CPU busy %, a ratio, PromQL
arithmetic) is not a column or saved metric of the metric's dataset: the agent saves its query as
a Superset **virtual dataset** (`create_virtual_dataset`: a PromQL finding as
`SELECT ts, <labels>, value FROM promql('<the PromQL>')` on its promagg database), then saves the
chart on that dataset with the finding's time range. Creating a dataset needs Superset's
*Dataset write* permission (Alpha, Admin; not Gamma), and `promql()` needs access to the whole
database; Superset's own check of the SQL's tables applies. The same name and query give back the
same dataset (no copies when the agent tries again).

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

Under an answer, the chat shows the results it is based on, each named by what it shows ("FAILED by
APPLICATION · 23 Sep"); a query the agent ran again in the same shape (fixed after an error, a check
or a rule) replaces its earlier try, which stays in the answer's tool calls.

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

**The team's words** come first: the glossary terms of the question (all the words of a term, or
its rare word; a code such as `D-1` or `W-4` that its definition uses; the terms a found term names,
or that name it) are given in full with the question, before where the data is. With the question,
nothing is given twice: the knowledge found leaves out the metrics and indices already in "Where
the data is" (and the same metric in another database), the learned answers, the memories and the
rules already given, so that its room goes to the catalog, the documents and the Context.

**What is happening now** ("in production", "with an application", "on this metric, chart or
dashboard", "is everything normal"): the agent finds the dashboards and charts about the subject
(Superset's charts and dashboards are part of its knowledge: what each shows, its dataset, metrics and
filters, the dashboards it is on), reads their latest data, the team's checks and limits
(`check_health`) and the alerts firing, compares with the same window of the previous weeks
(`compare_to_usual`), and answers subject by subject (normal or not, value, usual, limit, since when),
with a screenshot of a chart that shows a problem. It can also save a query in SQL Lab, give a link
that opens SQL Lab or Explore, and read the data of an existing chart, when asked.

Mimir tenants (`__tenant_id__`) usually separate applications or subjects: the agent filters on the
tenant a question is about, compares by tenant, and never adds tenants up unless asked.

**Several databases with the same data** (two OpenSearch clusters, a DR replica, one Mimir reached
directly, federated and through a gateway): the same index or metric name can hold other data in
each. The database comes from, in this order:
* the question: a database it names (its whole name, or words of its name no other database has,
  next to a word for a database: "on the DR replica cluster", "in the federated Prometheus"), or a
  Mimir tenant only one database has;
* the charts and dashboards the question or the chat is about (an id, a link, a title in quotes, or
  one a tool of the answer read): the database of each chart's dataset. "Is the dashboard 'Tenants
  via the gateway' normal?" is answered from the gateway's database, not from another one with the
  same metric;
* otherwise `agent.preferred_databases` (names or ids, in order), then the catalog's metrics database,
  then the database the team's charts use for that index or metric. "Where the data is" names the
  database used, the others that have the name, and why.

The first two are checked: a query on the same index or metric in another database is sent back
before it runs, with the database to use; another database is read only when the question names it
(its name, or "database 4").

A value the question names that is in more than one kind of data ("BILLING_API": an application of
the jobs index and a label of HTTP metrics) is shown with where it is, when the question does not say
which data it means; the answer then says which one it took and names the other, and when it read
only one of them without naming the other, a line under the answer does (*Not read for this answer:
... Ask if you meant that data.*).

While it answers, the agent:
* **asks when the question can mean two things** that give different numbers (two fields or
  metrics that fit, a term nobody defined, a period not said where no default applies), and
  nothing given decides (the team's words, the memory, the learned answers, the chat): one short
  question naming the readings and the one it would take; otherwise it answers and says in one
  line which reading it took. The answer to that question is learned (see below);
* **applies the team's rules**: they come again next to the question and win over the learned
  answers; a rule that filters on a field or label ("ENVIRONMENT_TYPE = 'UAT'") is checked on each
  query and saved chart **before it runs**: one on data that has that field and does not use it is
  sent back with the rule and the condition to add (`"ENVIRONMENT_TYPE" <> 'UAT'`, a chart filter
  `{"column": "ENVIRONMENT_TYPE", "op": "!=", "value": "UAT"}`); sent again unchanged, it runs and the
  answer is marked (unless the question asks for the rule's value). In the lab the model ignored the
  rule even after being told once; a query refused before it runs is always written again;
* **adds no condition of its own**: every condition of a query (a status, an environment, a threshold,
  a chart filter) must come from what was said: the question and the chat, the team's rules and words,
  the memory, the documents and learned answers found, the formulas of the instructions, or the rows an
  earlier query of the same answer gave. "Late" jobs counted among the successful ones only, or "over 45
  minutes" read from the 60-minute bucket, are sent back before they run (once per condition); kept
  anyway, the answer says which condition nobody asked for. Open questions (what is happening, why) are
  not checked: the agent chooses what to look at;
* **counts events, not samples**: `COUNT(*)` on a counter of a metrics database (requests, errors,
  jobs) is sent back once with `SUM(increase)`;
* **queries the question's period**: a question with a date or a period needs queries on the time of
  the data (the index's time field, `ts` for metrics), not on business dates (position date, D-1)
  unless it speaks of them; a question about one whole day needs that whole day (not 00:00-06:00,
  not the next day), a span of days ("14 to 20 September") exactly those days. Sent back once before
  it runs, like the rules;
* **does what a chart was asked for**: a saved chart asked for the top N of something that still
  shows more (a row limit does not cut the categories of every chart type) is told NOT DONE, with
  how to do it (a dataset of the query that keeps the top N), never to claim it;
* reads **a reply to its question** ("The failed jobs.") with the question it answers, and a short
  reply after an answer ("Only PROD.") with the question it completes;
* is sent back once when a question asks **how many** and the answer gives only shares or rates;
* saves charts with the **query context** Superset's front end would save (Superset's MCP service
  saves none): their data API, CSV and text reports work;
* when its calls run out (twice `agent.max_steps` when it builds charts and dashboards), writes what
  it did and saved and what remains, instead of stopping without an answer; that answer, and the last
  result shown when the model writes nothing, get the same checks (numbers, rules, conditions);
* sends back once an answer that **goes round in circles** (a model writing its working notes again
  and again until its token limit), then cuts it where it repeats and says so; one LLM answer has at
  most `llm.max_answer_tokens` tokens (8192);
* has **every number checked** (`agent.check_numbers`): a number of its answer must come from what
  it was given: a value of a result as shown or rounded, a share as a percentage, seconds in
  minutes or hours, bytes in kB to GB, a column's total or average, the total of its first rows, a
  row count, a rate within a row, a duration between two times, or a number of the question, the
  chat or the knowledge. Otherwise the answer goes back once ("compute totals, rates and
  differences in the query"), and a number still made up is marked in the answer (*Check: these
  numbers or names do not come from the results of this answer's queries*). Names with digits
  (servers, hosts: `srv-amer-002`), in `code` too, are checked the same way: a series is never
  continued. A big result tells the model how many rows have an empty value (200 servers and one
  job without a server, not 201 servers);
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

## The governed pipeline (0.6, optional)

`superset supagent settings --set agent.pipeline=governed` (back: `classic`, the default). The model no longer
writes queries; each answer goes through:

1. **the decider**: the tables (an index or a metric in one database) and the knowledge the question needs,
   chosen from every source. Candidates come from the dictionary directly (names, descriptions, synonyms,
   associations, the values the question names, the database or chart it names, learned answers, the team's
   charts), ranked by a small learned gate, then one LLM call chooses among the best and says what is ambiguous
   or missing. The gate learns only from answers a person confirmed (Helpful, an admin's confirmation, the
   reply to a question back), hourly, around fixed defaults;
2. **the plan**: one LLM call fills a typed plan (tables, measures, groups, conditions, period, top N) where
   every condition names its source (the question's words, the chat, a knowledge item, an earlier step);
3. **the checks**, by code: a condition nobody gave, a period that is not the question's, a top N nobody asked
   for, a value the data does not have, a dimension the question asks per that is not grouped, a counter
   averaged, a unit that cannot be converted are sent back once; the team's rules are added by code;
4. **the queries**, built by code from the plan: through Superset's chart data API when the table has a
   dataset (its permissions and row-level security apply), else as execute_sql with the user's permissions;
5. **the answer**: written from a figures sheet made by code, its numbers checked, with a line saying how it
   was counted (each condition with its source).

Charts and dashboards to save, files, e-mails, screenshots, status and "is it normal" questions, notes,
questions for another MCP source, and a question no plan passed the checks for, go to the classic agent (its
steps say so).
With `pip install "supagent[graph]"` the pipeline runs as a LangGraph graph and other MCP servers can be added
(`mcp.servers`, see INSTALL.txt).

### The router (MOA, 0.6)

Before each answer, one short LLM call reads what the question asks and chooses the kind of work it needs:
*functional* (the business meaning or figures of the data), *technical* (how the systems work), *incident*
(what went wrong and why, now or over a past period: see *Investigations*), *charts* (Superset charts and dashboards themselves),
*observability* (is it normal, anomalies, health) or *infrastructure* (servers and services: CPU, memory,
latency, errors, availability). Nothing in it is about one business: what is specific to a deployment comes from
its own knowledge (shown to the router with its categories) and its own confirmed examples. The route gives the
knowledge of its kind first, the tools it needs and a short instruction; in the governed pipeline, *charts*,
*observability* and *incident* go to the classic agent. The LLM scores every kind (0-100); when the best is not
clearly ahead, nothing is routed: the normal way, as before (`agent.router`, `router.min_confidence`).

It learns from the answers people confirmed whose execution followed the route. Similar questions of the team
are shown to the LLM (how this team names things); the same question asked again is not: its examples only vote,
and decide alone when two or more agree or when an admin set the route (*Data dictionary → To review*). An admin
confirming an answer judged the answer, not its route: that is not an admin example. On the lab's two benchmarks
(the 94 questions of the held-out test half): 82% routed right with no example (57% before 0.6.0b3), 88% with
examples, 83% with a wrong confirmed example of the same question (31% before).

## The Data dictionary (0.6)

One page for everything the agent knows, in four tabs (deep links: `#review`, `#catalog`, `#memory`,
`#docs`, `#notes`, `#context`, `#learned`, `#categories`, `#map`, `#browse`, `#relations`, `#changes`, `#search`).
What each part is says itself behind its **i**; what cannot be undone (a delete) is confirmed in a small card that
says what goes with it (0.8):

* **To review** (admins; the page opens on it when something waits, the count is on the tab and in Settings): the
  team memory proposed from the chats, the answers marked Helpful, the categories and relations the LLM proposes,
  each a card with its actions (approve, correct, merge, reject; approve all shown). A proposed value is edited
  whole before it is approved (**Edit…**, 0.8.2: its category, its name, what it covers, its other names and what
  it is part of, then *Save and approve*, or *Save* to decide later). Then, folded, what the agent already uses
  and an admin may check (the kinds of work learned by the router). The AI-written descriptions of the data are
  not listed there (0.8.2: tens of thousands on a platform); *Data → Browse → AI-written, not approved* shows
  them, to correct the ones that matter;
* **Knowledge**: the catalog, the team memory, the documents and sites, the notes, the Context, **Learned by the
  agent** (the learned answers, most useful first, which an admin may correct before confirming them: Edit, then
  *Check the query* runs it with the admin's permissions, and a changed query is confirmed only if it runs; the
  catalog entries the agent wrote; where the data of the questions was, where an admin also puts words on a table
  by hand, which never fade; query timings), the categories and the **System map**; edited here, read only for
  users who are not admins;
* **Data**: browse the dictionary, the relations, the changes; **Search**: the knowledge as the agent searches it.

The catalog's classifications: glossary, index, metrics, relationships, checks, rule, **guide** (documentation,
runbooks, how-tos: called "note" before 0.8, renamed so that it is not taken for the users' quick notes; a YAML
with "note" still imports) and formula.

A save answers at once; what follows (the catalog applied to the dictionary, the search pieces, their vectors,
the knowledge store) runs in the background of the process that saved (`knowledge.apply_background`), and the line
next to the tabs says when the agent's search is up to date (on the lab: saves 0.03-0.5 s instead of 0.8-1.6 s,
up to date about 2 s later; a process that stops meanwhile loses nothing: the hourly indexing catches up).

**Categories** (0.6, `superset supagent classify`, and every day with the learning): every piece of knowledge
(catalog entries, memories, documents, Context pages, learned answers, indices, metric families) is classified by
the LLM: functional or technical, its subjects, the applications and the components it is about, and its
relations to other items and tables (about, depends on, part of, explains, runs on). The values come first from
what people wrote (the categories written on catalog entries and documents); the learning reads others in the
data (the values of the fields each category is read from, `categories.fields`) and the LLM may propose new ones.
**Nothing the learning finds changes the categories before an admin approves it** (0.9, `categories.review_all`,
on): the values read in the data, the categories the learning gave to the data's objects, the LLM's values, tags
and relations all wait in *To review* (each value says whether the LLM proposed it or which field it was read
from; a category's values found in the data are approved together in one click); no value in use is merged,
retired or changed by the learning: a proposed value that has the name of an approved value of another category
says so, and one click merges it. The router and the search read the approved ones. In
*Data dictionary → Knowledge → Categories* an admin also adds a value by hand (used at once) and, with Edit,
changes a value's category (subject, application, component) as well as its text, its description and its other
names; a value moved or renamed onto one that exists is merged with it, items included. A value can be **part of
several others** (a jvm of two applications and four components): its items count as about each of them, the
LLM proposes these relations for new values and for known ones (To review), and **Categories → System map**
draws the whole system from them (see *The System map*), with the items about each value that exist now. Categories of your own
(server, environment, team...) are added there too, each with the field names its values are read from in the
data (`categories.fields`): their values are kept with where they come from, and two such fields of one index
show which values go together, proposed as "part of" for an admin to approve. In that list (0.8.2) **Edit**
changes the name of one of yours (its values follow) and the field names of any, and **Remove** takes one of yours
away with its values and everything that names them (the items they were given to, the "part of", the interactions
of the map), after asking and saying what goes. In To review every suggestion can
be approved, rejected or changed first (Change…). With `categories.review_all` off, what the LLM is sure of is
used at once and the values of the data are approved at once (the behaviour before 0.9); the values the LLM
proposes, the "part of" relations and the removals always wait.

More about the categories in 0.9:

* **What each category is**: a sentence an admin writes in *Categories → Categories and where their values come
  from → Edit*, or on the System map (the **i** in the category's name). It is shown with the category (under its
  name on the map) and given to the agent with the system picture and to the router with the parts a question
  names (`categories.about`).
* **Where each value comes from**: a value read in the data says which field of which indices and which label of
  which metrics it was read from ("field NODE of 12 indices such as jobs, steps, alerts (Platform)", "label host
  of 2,314 metrics such as node_cpu_seconds_total, node_load1, up (Metrics)"), in Categories, in To review and on
  the map's panel: the tables and the fields a query about that part needs.
* **A category read from the data is not the LLM's**: its values (hundreds of servers, hosts, environments) are
  read once each from the fields and labels you configured, whatever the number of metrics that carry the label
  (this is what made `superset supagent classify` take more than half an hour on a platform: every value was
  looked up again for every metric, one query each; now a second for thousands of metrics). The LLM is neither
  given their list nor asked to add to it; it still classifies the knowledge items into subjects, applications,
  components and the categories you fill by hand.
* **"Part of" is chosen in a box where you type**: the matching values are listed (the server searches the names
  and the other names of every value, the wider categories first), several are chosen one after the other and
  shown as chips; nothing typed lists the first values and says how many more there are. No list of thousands is
  loaded. It is the same box in a proposed value's *Edit…* and *Part of…*, a relation's *Change…*, a new value
  and a value's *Edit*.
* **Parts that look retired are proposed, never retired by the learning** (*To review → Parts that look
  retired*). Two signs: a value the learning read in the data that none of its category's fields and labels has
  shown for `categories.retire_days` days (14; asked of the data itself over that period, and seen so by two
  checks on two days; nothing is concluded from a list that may be cut, a query that failed, an index with no
  time field, or data that stopped as a whole); and any value, **the ones you put by hand too**, that a document,
  a catalog entry, a Context page or a team note says was retired or decommissioned (the sentence is shown). A
  value put by hand with no base in the data is only ever proposed by the second sign. *Retire* keeps the value,
  marked retired (no longer used nor drawn; adding it again by hand brings it back); *Keep* declines that reason
  for good. A value seen again in the data loses its proposal.
* **The classification is a run of its own**: *Settings → Daily learning → Classify now* (or `superset supagent
  classify`) is listed with the learning runs, with its steps (the values read in the data's fields, what waits,
  the items that changed, the categories given by the LLM, the parts that look retired, the interactions the
  documents state, the interactions explained), their time and counts, and what it asked of the LLM (calls,
  tokens, seconds). Never two runs at a time.
* **Right after the Context** (0.9, `context.classify_after`, on): each Context build (the nightly one, one asked
  in the page or with `superset supagent context --build`) is followed by the classification, which then reads
  what the Context wrote that night with the documents, the metrics and the data: the categories of the items,
  the part-of of the values, the interactions the texts state. The nightly learning leaves the categories to it
  (its step says *after tonight's Context build*) unless a Context was already built that day; a learning run an
  admin starts, or one with the Context off, does them at its end as before.

**Search** (*Data dictionary → Search*, 0.7): everything the search finds for the words, as the agent searches
(at most 500, 20 per page). Each name opens the item: charts and dashboards in Superset, everything else in the
dictionary's side panel, at an address (`…/supagent/dictionary/#open/<item>`) that can be shared with someone
who may read it.

## The System map (0.8)

*Data dictionary → Knowledge → System map*: the architecture of the system as the categories describe it, for every
user of the dictionary (each sees the parts with an item of the databases they may query, and what those are part
of). One column per category (subjects, applications, components, then your own: servers, environments...), each
part in a box with what it does: its description, else the sentence of a document, a guide or a Context page that
names it (the one that says what it is, from a document first), and how many items are about it now (a part whose
items are gone says so). A grey line joins a part to what it is part of; the **interactions** an admin draws
between parts (depends on, sends data to, reads from, calls, runs on, triggers, monitors) are arrows, with no
text on them: a click on one shows what it is and what to do when following it (see *Every interaction
explained*). *Focus on…* shows a part with what it is part of, what it is made of and what it interacts with. Click a
part: its panel (part of, made of, interactions, its knowledge in the search). Admins: **Edit** to move the boxes
(their places are kept for everyone), draw an interaction (a part, *Draw an interaction from here*, then another
part), write what a part is (or take the sentence found), hide a part. Exports: **PNG, SVG, PDF** (the map as
drawn, the focus too).

What is shown (0.9):

* **Fold and open with a click on a category's name**: its parts become one box (its name, how many, the first
  ones) and come back with a second click, or a click on the box. Any category folds; one read from the data with
  more than 18 parts (60 for the others) starts folded. There is no Open / Fold button any more. What a viewer
  folds is theirs for the time of the page; in *Edit*, an admin's fold is kept as the view everyone starts from.
* **What you see, chosen in the box above the map**: type to find categories and parts, choose several (chips);
  nothing chosen shows everything. A category shows its parts; a part shows itself with what it is part of, what
  it is made of (a part with parts says how many) and what it interacts with; categories and parts together show
  the parts chosen and, around them, only those categories. Each category of the legend is also a switch, and the
  cross in a category's name on the map hides it; *Show the hidden categories* brings them back. Only the lines
  between what is shown are drawn, so the map of "applications, pools and services" is three clicks away. The
  categories chosen are each viewer's own (kept in the browser, not for the others); the columns close up, and a
  box an admin placed by hand stays next to its column.
* **What a category is**: its description under its name, and in its panel (the **i** in its name) with how many
  parts it has and the fields its values are read from; an admin writes it there.
* **Full screen**: the map with its tools, its legend and its side panel on the whole screen (the browser's full
  screen, else the whole window); *Escape* or the button comes back.
* **Move a category** (admins, *Edit*): drag its name; its boxes follow, the ones placed by hand too. Kept for
  everyone.
* **Groups of categories** (admins, *Edit* → *Group categories…*): a name and the categories it holds (servers,
  resources and network under "Infrastructure"); their columns are put side by side inside a frame with the
  group's name. Display only: nothing changes for the categories, their values or the agent. A category is in one
  group; a renamed or removed category is followed.

The map follows the categories by itself (0.8.2): it is read again each time it is shown, when the window comes
back and every 20 seconds while it is looked at (once a minute for who is not an admin; never while an admin
edits it; not after 15 minutes with nobody at the page), and says what the reading
brought next to its title ("Updated: new part ..."), the new box and the new "part of" outlined for a moment;
*Show* brings them in view. An admin sees every category, the ones with no value yet too (a dashed "No value
yet" box that leads to Categories), and how many proposed values wait in To review: the map draws the approved
values, a proposed one comes once approved.

## Investigations (0.9)

"Today the night batch of the billing applications is far behind, the runs take longer than usual: why?" is not one
query. It is finding out what is really off, where, what that depends on, what changed, and checking the cause
before naming it. A question that asks what is wrong and why (its words: *why*, *cause*, *what happened*,
*investigate*...; or the router's *incident* route, which covers what is wrong now as well as a past period) gets:

* **The system around the question**, built without the LLM from what the team prepared and given with the
  question: the parts it names (values of the categories: a family, an application, a pool, a service...), what
  each consists of (the applications of a family, the servers of a pool), what they **depend on, run on, read
  from, send data to and call** (the interactions of the System map, with their notes), one step further (what an
  upstream application reads, the servers of the pool they run on), **where each kind of part is in the data** (the
  fields and metric labels its category is read from), how the tables join and which field holds the usual value
  of a measure (the catalog), the health checks. A plain question that names a family gets only what the family
  consists of, so that "the billing applications" is counted on the right values.
* **`compare_groups`**: what is unusual in a table, and where is it concentrated? **One call compares every
  measure of the table** over the question's scope with the same time of each of the 8 previous days that have
  data (their median), in total and value by value for each of the table's fields of few values (application,
  server, step, region, version...: the ones asked first, then the others, so that the field that localizes is
  not missed because nobody thought of it):
  * the number of rows, each duration **against the field that holds its usual** (`usual_of` in the catalog), the
    average of each numeric field (waits, volumes...);
  * for a business date (`POSITION_LABEL = 'D-1'`), **the stages of the rows**: the table's time fields in the
    order the rows reach them (scheduled, released, started, ended), each with its description from the catalog:
    how many rows had reached it by that time of day, how long after the stage before, how many were still
    waiting for it and since when, against the same time of day of the earlier days. The rows are the business
    date's, as they were at that time on each day: a run not started yet counts the same way on every day. The
    first stage that is clearly off is said first: released late (they waited for something upstream), released
    on time but not started (they wait for a slot), started but longer.
  * for each measure that is off, **the field that holds the change**: the one whose values that stand out hold
    most of it on the fewest rows (the region whose every run is late, rather than the application of which a
    third is, or the ten servers of that region); the others it is also seen on are named after it.
  * **what the related tables hold about what stands out**: the latest records of the last 7 days that the
    catalog's joins lead to (the changes made to that application, the alerts raised on those servers); for a
    field nothing joins, the records whose text mentions the value (a change that says it concerns that
    perimeter).

  * **the reference days your team compares with** (`agent.compare_against`: *yesterday, 1 week ago, 4 weeks
    ago* by default; any of yesterday, N days ago, N weeks ago, N months ago, 1 year ago, a date; weeks and
    months are the same weekday): each figure that stands out is given on those days too ("3 now, 1 yesterday,
    1 one week ago, 1 four weeks ago"). A figure that is **as usual against the last days and far from an older
    reference day** is said, with where: a change that is ten days old no longer shows against the last eight
    days, and does against four weeks ago. A question that names its own days ("compared with three months ago")
    gets them (`against`), each compared in full: what differs from that day, and where. On a Monday, "yesterday"
    is the Friday; a reference day with no data says so. A figure far from the last days and **the same as on
    the same weekday of the earlier weeks** is said to be what that weekday is, not a change (twice the volume
    every Monday).
  * **since when**: for what stands out, the earlier days that were already off the same way ("off since
    2026-09-17 (the 2 days before too)", or "new on this day").
  * **a lead followed once, in the same call**: when the rows wait at a stage where they do not usually, on one
    value of a field (the runs of one pool waiting for a slot, the orders of one country paid and not shipped),
    the tool asks the next question itself, whatever the question's scope left out:
    * `outside_the_scope`: **who else is there**. The rows outside the question's scope that share that value
      during the window, against the same window of the earlier days and by the scope's own fields: "on POOL =
      P1, the rows outside the scope during the window: 911 against 350 usually (above usual). Who they are:
      ENVIRONMENT = TEST (562 rows, none on the earlier days)"; or that nothing else has more rows there than
      usual. A row is there when its life overlaps the window (from its first stage to its last): a row's own
      time moves as it advances, and would count a day in progress and a finished one differently.
    * `the_rows_there`: **the same comparison over every row that has this value** (the business date kept, the
      scope's other conditions left out): whether all of them are held or only the question's, what lasts
      longer there and where (four servers of the pool three times slower), since when, and the related records
      about it (the alerts of those servers). `agent.compare_follow` (on) turns it off; it costs about as many
      queries again, and is not started once half of the call's time is spent.
  * **what is off on its own**: after the first late stage, fewer rows at the later stages are its wake; a stage
    whose rows take longer to reach it from the stage before (a run half longer once started), or are held there,
    is said separately: a late start does not explain a longer run. Rows that wait at a stage since less long
    than usual came late to it, and are not counted as held.

  The tool knows nothing of a kind of system: a table, its time field, its numeric fields, its fields of few
  values, its other time fields, what the catalog says of them and the joins it declares. The runs of a batch,
  orders (created, paid, shipped), tickets or requests are compared the same way; with a business-date label in
  the scope the rows are the business date's, else the rows of the window.

  Noise is kept out: a count of 13 against 8 where counts differ from one day to the next (a count that is the
  same every day has moved when it moves), an average over two rows, 10 s instead of 60 where the usual of all
  is an hour, a status (its values change as a row advances, and the earlier days are read as they ended), a
  field the runs not started do not have yet (their server), a value that holds nearly all the rows (the one kind
  of run there is: no place to look). Four servers of seven three times slower with the three others as usual is
  a place to look, though more than half of the values stand out. A value never seen on the earlier days stands
  out in a count when there are more rows in all (as many rows in all are rows that moved to it). A value that
  took the place of another (a new version of an application) is read against the history of the one it
  replaces: "2.0 (new, in place of 1.9: x3 its usual)". A weekend is read once. A window that starts the day
  before does not skip every other day. One measure can still be asked for its values in full (`measure:
  "avg(FIELD)"`, `"ratio(A, B)"`, `"count"`). The result stays within the room of a tool result (about 6,500
  characters), what it found first.
  **A scope written loosely is read as it was meant**: a condition on a time in `where` (the rows since 01:00
  today) would leave every earlier day empty: a date moves with each earlier day (on any time field, cast or cut
  to its day), what is no date (since now minus six hours) is left out and said, a condition between two fields
  or on the hour stays. `"APP" = 'a' OR "APP" = 'b' AND "ENV" = 'P'` is read with the alternatives of one field
  taken together (in SQL the AND binds first, and 'a' would be read over every environment and every day). A
  condition that holds only while a row is in progress (`"STATUS" = 'QUEUED'`: no row of the earlier days has
  it) is left out and said: the stages already say how many rows wait at each. A state written as a
  business-date label, a subquery, or a scope that matches nothing are refused with how to write it. **A
  business date is the one of the day asked about**: on a window that ended days ago, the label D-1 is that
  day's own, and each earlier day's own from there. When nothing stands out, the result says to answer that
  (`next`), so that a question whose premise is wrong is not investigated for thirty calls.
  **What one call costs**: about ten small aggregations per field (the window, the 8 earlier days, the reference
  days), so about a hundred for nine fields, each filtered on the scope and pushed down to OpenSearch, three at a
  time (`agent.compare_threads`; 1 on a busy cluster); a lead followed adds about sixty. After
  `agent.compare_seconds` (45) it reads no further field and answers with what it has. With osagg 0.2.13 a call
  takes 4 to 6 seconds on a hundred thousand rows, 8 to 10 when it follows a lead.
* **`compare_logs`** (0.9): **what do the logs say that they do not usually?** Application logs, batch logs,
  events in an OpenSearch index (or any table with a time field and a field of text): the lines of the window
  (the quiet levels, INFO and DEBUG, left out unless asked) are grouped into **patterns**, what changes from one
  line to the next left out: the numbers, the times, the ids, the names written as identifiers
  (`GC overhead on node105: heap 97% used` and `... node107: heap 95% used` are one pattern; a list of names
  is one name, a line cut in the middle of its list the same pattern), the quoted values. Each pattern is counted
  against the same window of the 8 earlier days that have data, and said when it is:
  * **new** (none on the earlier days; from 2 lines for an error, 5 for a warning), **far above its usual**
    (twice, and beyond the day-to-day noise), or **rare** (there on fewer than half the earlier days: "there on
    only 1 of the earlier days (2026-08-07 with 63)": a weekly job, or the start of something);
  * as frequent as usual but **with numbers far from their usual**: each number of a line is compared by its
    place, named by the words around it ("its number \"after [#] min\" is 60 against 30 usually (x2)"): the same
    slow write every night, 40 s tonight against 8 s;
  * **gone** (there on most earlier days, none now: a heartbeat that stopped), and **as every day** (a warning
    that comes every night is no finding, and is said to be one);
  * with **where its lines come from**, counted: the values of the table's fields of few values (the servers, the
    applications, the steps, the loggers), and the names the lines hold when they are a few (written back in
    the pattern when nearly all of its lines hold one: `no free slot on GRID_EU_STD for <name>`); when
    (its first and last line); an example; the errors first, then the most lines.

  The patterns are found from up to 450 lines of each level read in each window (its latest of each third),
  scaled to the lines the window has; the ones that are said are then **counted line by line** in the database
  (a piece of text all their lines hold), with their first and last line and where they come from counted by
  field: a burst at the end of the window does not lean the counts or the places. A big table: after
  40 seconds the counting stops and the rest is said to be estimated. The scope (`where`) is written as for
  `compare_groups` (a business-date label or date moves with each earlier day); `against` gives each pattern on
  reference days too. Nothing in it knows a kind of log: the message field is the text field of the table (the
  one named like a message first), the level field the one named like a level. The picture of a question names
  the log tables of its parts and their fields (*Their logs*).
* **`system_links`**: what the System map says of any part by its name (what it is, what it is part of and
  consists of, its interactions both ways, where its kind is in the data): to go from the part that looks wrong to
  what it depends on, and to what depends on it. The *technical* route offers it too.
* **Five steps**, in this order: the facts in one call (`compare_groups` on the question's table and scope, from
  the start of the runs to now); the reading of its stages (the first that is clearly off says what kind of
  problem it is, and fewer rows at the stages after it are its wake, while a stage off on its own is a second
  finding to explain: released late, look upstream; waiting for a slot, who holds it is in the same result,
  `outside_the_scope` and `the_rows_there`; longer, look at the steps, the servers, the version, the volume);
  why (the related records it gives, what those parts depend
  on, the health of their servers and services, their logs, the changes before it, what the team wrote of such a
  day); the
  check of the cause (it must cover the same runs and the same time, and what is not affected must be free of it;
  what else happened and does not match is said to be ruled out; a day the team documented, with the effect it
  documented, is the explanation); then the answer: what is wrong with its figures against usual, where, since
  when, the cause with the record or the figure that shows it, what was ruled out, what could not be checked.
* **Twice the calls** of an answer (`agent.max_steps`), and the reminder to conclude two calls before the end.
  The calls are kept for what is not yet known: the third query of an answer that differs from earlier ones only
  by its dates (the same query once per day) is sent back with how to get every day at once (one query grouped by
  day, or `compare_groups`), and eight tool calls at most are run from one message of the model.
* **A check of what is blamed**: a part of the system the answer gives as the cause must be tied to the question's
  parts, by the System map within two steps or by a result of the answer's own queries on those parts. A part
  that is neither (the servers of another pool that had an alert that night) is sent back once, then marked under
  the answer.
* **Investigation paths**: an investigation marked *Helpful* keeps its path, written generic by the LLM in the
  background: the kind of problem, the kind of cause, the checks in their order (tools, tables, fields and metrics
  by name; no value of that one case), what confirms the cause, what was ruled out. It waits in *To review* with
  the answers marked Helpful (*Investigation path*; an admin corrects, confirms or rejects it) and in *Learned by
  the agent*. Once confirmed it is given with the next investigations of that kind of problem. One symptom has
  several known causes ("the batch is late": a late feed, servers out of memory, a release, a licence...): each
  is its own path, and they come together, the closest and the most often found first, three with their steps and
  the others with their cause and what confirms it; the agent is told to find which one holds, with the check
  that tells them apart, and that another cause is possible. Nothing is kept when the answer found no cause.
* **The path of every answer marked Helpful** (0.9): the same learning for a small request as for a big one. A
  count, an extract, a check or a confirmation marked *Helpful* keeps, with its query, **its path**: the data it
  used (each table with the fields its queries name, each metric with its labels) and its steps in their order
  (the tool, what it was asked on, the shape of the query). It is read from the answer itself, without the LLM,
  and holds no value of that one case: the dates, names and numbers of the queries are left out, and so are the
  steps that failed. An answer that ran no query to keep (a health check, a comparison with usual, a comparison
  of groups) is learned **as its path** (*path (no query to run)*); one that only looked things up teaches
  nothing. The path is shown in *To review* and in *Learned by the agent* under the query, and given to the agent
  with the learned answer for the next similar question (`its path: ...`), so that it goes to the same tables,
  fields and metrics by the same steps. **An admin corrects it** in their own words in *Edit* (the path box under
  the query) before confirming the answer; emptied, it goes back to what the answer did; what an admin wrote
  stays when the same question is later answered another way. A path is given only to who may query its
  database, like every learned answer.

**What to prepare** (the more of it, the less the agent has to discover query by query):

* *Categories*: the families, the applications, and your own categories (server, pool, service, feed...) with the
  field names their values are read from (`categories.fields`: the same pattern covers the index field and the
  metric label), and what is **part of** what (an application of a family, a server of a pool).
* *System map*: the **interactions** between parts, with a few words each (which jobs, which step). The daily
  learning proposes the ones your documents state (below).
* *Catalog*: for each index its `time_field`, its `relationships` (to: another index, keys: the fields that
  join them: `compare_groups` follows them to the changes of the application or the alerts of the servers that
  stand out) and, on the field that holds a usual value, `usual_of: <the measured field>` (an average duration
  kept on each run: `AVG_DURATION: {usual_of: DURATION}`); **a description of each time field** (when a run was
  released: "when its feeds and upstream jobs were done"; started: "when it got a slot"; ended): the stages are
  given with those words, and they say what a late stage waits for; the `relationships` between metric labels and
  index fields; the health `checks`.
* *Documents and guides*: how a night goes, what waits for what, the calendar (month end, maintenance windows),
  what each alert means. They are searched, and read for interactions.

**What is still missing** is listed for you, without the LLM: `superset supagent check-system` counts what the
agent knows of the system (per category: the values, those that are part of something, those with interactions,
the fields and metric labels they are read from; the interactions by kind; per table of the catalog: its time
field, its joins, its usual values; the health checks; the paths) and says in words what it does not find: a
category whose values interact on the map but are read from no field, values that were left with no interaction
while others of their category have some, a table with no time field, a field named like a usual value
(`AVG_...`, `USUAL_...`) whose measured field is not said, no join between the tables, no health check, what waits
in *To review*. `--question "..."` adds what the agent is given for that question, the parts it names of which
nothing is said, and the words it writes as names (`ABC_DEF`) that are neither a value of the categories nor a
table, a field, a metric or a value the agent learned. Admins see the same list on the System map page (*N points
to complete for investigations*).

**Interactions proposed from your texts** (`learn.interactions`, on): with the daily learning the LLM reads each
document, guide, Context page and team note next to the names of the known parts it mentions, and proposes how
those interact (depends on, runs on, reads from, sends data to, calls, triggers, monitors), each with the sentence
that says so. A proposal is kept only when that sentence is in the text word for word and both parts are named
there; it waits in *To review* (with its sentence) and is neither drawn on the map nor followed by an
investigation before an admin approves it; a rejected one is never proposed again; a text is read once.
`superset supagent interactions` runs the pass at once (`--again` reads every text again, after many new values).

**Interactions the logs show** (0.9, `learn.interactions_logs`, on; no LLM): a scheduler writes what a run waits for
("APP_B.EU.02 still waiting for its inputs after 40 min: APP_A.EU, MD_EOD_RATES"), an application what it calls
("request to MDCACHE took 840 ms") or where it writes ("commit to RESULTS_DB took 3.2 s"). With the classification,
the recent lines of each log table (a time field, a text field and a field of a category's values: its
application) are read, spread over each hour of the last 14 days (the lines of the levels that are not quiet
too), and grouped into patterns; the patterns that state an interaction are then read whole (their own lines,
the first and the last of each day). A line ties the part it
comes from to the parts it names (the map's names, and the identifiers that start with one: a job of an
application), the kind read in its words (waits for, inputs: depends on; calls, requests: calls; reads, loads:
reads from; writes, publishes, commits: sends data to). A line that states no interaction (a GC pause on a server,
no free slot) proposes nothing; a kind the map already has ties only the categories it ties there (an application
calls services: a pool named in the same line is not something it calls). A pair seen on 5 lines and 2 days at
least waits in *To review* with its evidence ("18 lines of app_logs on 3 of the last 14 days, such as ..."), never
drawn nor followed before an admin approves it; one drawn, proposed or rejected already is never proposed again.
The step keeps to its time (`learn.interactions_logs_seconds`, 120 s), checked before each of its queries: up to
about 900 small queries per log table (729 for the simulated platform's 14 days; some search a piece of text in the
messages), the ones left over not sent (what was read is used; the next night goes on). Not timed on a large
index: on a big or shared cluster, lower it or switch the step off.
On a simulated batch platform, from 14 days of its logs, it found the 18 real dependencies between its applications
and none that was not (16 of the 18 from 7 days).

**Every interaction explained** (0.9): a **short explanation** (a few words from the first part's side: *waits
for the positions of its perimeter*, *writes its results there*) and a **long one**, what to do when an
investigation follows it (what to check on the other part in the same window and for the same perimeter, what a
problem there does here). The LLM writes both with each interaction it reads in a text; for the others (drawn by
an admin, older ones) the classification's step *interactions explained* writes them from what is known of the
two parts, the sentence that states it and the Context sentences that name both. Only an empty explanation is
written: what an admin wrote stays, and a link says who explained it (*Explained by the AI*, *by alice and the
AI*). On the System map the lines carry no text: **a click on a line** (or on its name in a part's panel) shows
its short explanation, the long one folded under *What to do when following it*, who explained it and the
sentence it was read in; an admin corrects both there, in the new-interaction form, or in *To review* before
approving a proposal. The agent gets them: the short ones with the system around a question, the long ones of the
question's parts in it too (*What to do when following them*, within its room), and both from `system_links`.

**New, or every night?** `check_health` says of each breach on how many of the seven previous days the same
series breached the same check in the same window (`earlier_days`), marks the ones that do so on most days
(`usual: true`) and lists the new ones first: an alert that fires every night is not what changed today. Only the
checks that breached are run again. `compare_to_usual` gives each series on the team's reference days too
(`then`: its average over the same window yesterday, a week ago, four weeks ago, or on the days asked with
`against`). `promql_query` does the same for a level: a query of a few series (12 at
most, over two days at most) gives each one what it was over the same window of the 7 previous days (`usual_avg`,
`usual_max`) and says so in words (`against_usual`: "every series is as on the same window of the 7 previous
days: these levels are their usual, not a change", or which series differ): a pool that is full every night and a
queue that is as long every night are not causes.

Also in 0.9: `compare_to_usual` refuses a counter read as it is (its value only grows: it says to compare its rate
or its increase) and says when a value far from its median is still within what the earlier weeks differ by; the
times of the metric tools ("now-6h") follow `agent.now` when an admin pinned it.

## The subjects of a chat (0.8)

A chat often follows one subject over several questions, then changes subject, then comes back. Each question now
belongs to a subject (the chat shows a thin line where one starts or comes back), and its answer is given the
messages of that subject only: from its start (the first exchange, what the follow-ups refer to; the exchanges in
between shortened; the last three in full; 24,000 characters at most), never the other subjects' messages.

* **The same subject**: a reply to the agent's question, a short completion ("Only PROD."), a question that starts
  with *and*, *also*, *what about*, *same*..., one that refers to the answer (*that*, *those*, *it*), another day,
  period or value for the same thing, or a question about the same data (its named values, the tables the resolver
  says it needs and the tables the subject's answers read, its words).
* **A new subject**: the question says so (*another question*, *autre sujet*), or it is about other data with
  nothing in common with the chat.
* **Back to an earlier subject**: the question is about that subject's data (or says *back to...*).
* When the words cannot tell, one short LLM call (30 s at most) says which subject the question continues; not sure,
  the same subject: more context costs tokens, less context costs right answers.

Within a subject, a follow-up for another day ("And on the 23rd?", "Et le 23 ?", "And the day before?", "la
veille") is the previous question for that day: the day is read from the one named before, the answer gets its own
query with the previous question's conditions, and the period check reads the earlier question with the new day in
place (its "shipped on", its "during the night" kept). "Back to …" takes the scope of the question it goes back to,
not the last answer's conditions.

`agent.subjects=false`: the last exchanges of the chat, as before 0.8.

## Notes (0.7)

What a meeting decided, a fact the team must not lose: any user of the agent writes it down in a few seconds.

* **Where**: the chat's **Notes** button (on every Superset page, in the panel too): write (Ctrl+Enter saves),
  search, read the team's notes and the catalog's; or type `/note ...` in the question box (for the team) or
  `/mynote ...` (for you): saved at once, the LLM is not asked. *Data dictionary → Knowledge → Notes* lists them
  with pages.
* **Who reads them**: a team note, every user of the agent; a personal one, its author only (an admin does not
  see it). Its author changes or deletes it; an admin may change, pin (first for everyone) or delete a team note,
  or make a **catalog entry** of it (classification note: verified from then on).
* **The agent** finds the notes like the documents (the knowledge search, and `search_notes` with words and days
  for "what did we decide on Monday"), and always says whose note it is and of which day: a note is **not
  verified**. It is never a team rule, and never the source of a query's condition (the check of conditions
  leaves note lines out). The daily classification includes team notes.
* **The agent as a notes assistant**: ask it in the chat, in your words: "note that the ops meeting moved the
  express orders to FASTPOST from 18 September 14:00", "add to my note about the carriers that QUICKSHIP is back
  on the 21st", "show my notes of last week", "undo the last change to that note", "delete my note about the
  carriers review". It acts as you, with the page's rights: it reads the team's notes and your own, writes a
  team note (or a personal one when you say "for me", "my note", "personal"), changes your notes (an admin, the
  team's too) and keeps each note's earlier versions (`undo` puts the last one back). It deletes a note only in
  its answer to your **yes**: it shows the note first and asks. A request about notes is given the notes tools
  only (no data queries), and an answer that says a note was saved, changed or deleted while no such tool call
  did it is sent back once, then marked "no note was ... in this answer".

## Charts and dashboards, looked at every night (0.7)

With the nightly Context build (`charts.scan`), the agent looks at the team's Superset charts as the learning
user, through Superset's own chart data API (the dataset's permissions and row-level security apply):

* **what the data did**: each chart's metrics per day over the last five weeks, with the chart's own dimensions
  and conditions, on the dataset's main time column; the last full day before "now" against the same weekday of
  the four weeks before, per series (median and median absolute deviation, as `compare_to_usual`): **high**,
  **low**, normal. A difference of a few events is not unusual; a series that is a count without a row on a day
  the data has counts 0 (no breach is 0 breaches); data that **stopped** (the dataset's own last day is before)
  is said as such, never as "low". A chart left out says why: raw records, no metric, no time column, a fixed
  period that ended, its dataset gone.
* **what it shows**, in words: written by the LLM from what the chart reads (its metrics and dimensions with the
  dictionary's descriptions, its conditions, its dashboards), marked AI-written, written again only when that
  changes; one Context facts page per dashboard (its charts, what each shows, the last check).
* **the agent** answers "is there anything unusual on the X dashboard?" with `chart_anomalies` (a dashboard, a
  chart, or every chart; `look_now` looks again as the user): only charts the user may see; on a dataset with
  row-level security for that user, a look now as them (written nowhere). The charts' pieces in the knowledge
  search and the dashboard pages say only whether something was unusual, never the figures.
* **gentle**: `charts.max_charts` (200, the charts on dashboards first), `charts.minutes` (20),
  `charts.max_requests_per_minute` (12 per database), one query per dataset for its first and last day; a
  database that answers it is overloaded (a circuit breaker, 429...) `learn.stop_after_errors` times in a row is
  left for the next night; Stop; the LLM after the people, `charts.max_llm_calls` (40) a night.
  `superset supagent charts [--scan] [--understand] [--chart N]` runs it now and prints what was found.

## Learning from the chats

* **Learned answers** (0.2.2): only an answer marked *Helpful* is learned: its final successful
  query (SQL, PromQL or chart), with its time and size, under a short generic question the
  agent writes (no ids, dates or names of one case; the user's own words are not kept). If the
  agent finds the same question already learned, the answer joins it (one more *Helpful*)
  instead of making a duplicate. It is listed as *helpful, to review*; an admin confirms or
  rejects it in *Data dictionary → Learned*. *Not helpful* (or taking *Helpful* back)
  withdraws it, unless an admin confirmed it. A similar question of anyone in the team starts
  from the confirmed ones first, then the helpful ones, on the databases the user may query.
  The first answer of a chat names it the same way (a 2-6 word generic title).
* **Ranked by what happened** (0.6): after every answer, the items its prompt was given (learned
  answers, catalog entries, memories, documents, Context pages), the tables its queries read, and the
  learned queries it ran again (and whether they worked) are recorded. Each learned item's usefulness
  comes from what people said of the answers it was given to (Helpful, Not helpful): the learned answers
  proposed for a question follow it, one that people keep saying is wrong is no longer proposed (unless
  an admin confirmed it), and *Data dictionary → Learned by the agent* lists them most useful first with
  their reasons; one unused for 90 days says so (retire it there).
* **Search your own chats** (0.6): the box above the conversations finds a user's own earlier chats (the
  knowledge store: every word typed, or close in meaning, `search.chat_box_similarity`; else the words in
  the messages) and opens the one chosen on the message found.
* **Where the data was** (0.3, `learn.associations`): after each answer, the words of the
  question and the metrics or indices its successful queries read; the next questions with those
  words find them first. *Not helpful* takes them back; one that sent the agent to data that was
  not there (an error, no rows, while the answer came from elsewhere) loses a use; one no answer
  used for 60 days, or to a metric or index that is gone, is not used. *Data dictionary → Learned
  by the agent → Where the data of the questions was* (0.7) shows them one row per table: the
  words that led there, a word that also led to other tables or databases said so (such a word
  alone does not say where the data is), the answers and the Helpful among them, with a search, a
  database filter and pages; an admin's *Wrong* takes a word away from a table.
  `learn.associations = false` switches this off. Learned answers and memories that name a metric
  or index that no longer exists are not given to the agent. Measured (0.7, the lab's two
  benchmarks, the questions of one half never teaching): "Where the data is" finds the right table
  first (MRR) 0.45-0.53 without what the answers taught, 0.82-0.88 with it; weighting the Helpful
  answers more, a word's specificity, two-word phrases, a cap per table, or the tables of the most
  similar earlier questions (meaning) did no better on both halves, so the learning stays as it is.
* **What users state**: a message that tells something about the data ("KO means failed") is read
  for durable facts and rules like the explicit ones below (team ones wait for an admin). So is
  the answer to a question the agent asked back ("the killed jobs too": what the word meant), and
  the reason given with *Not helpful* (the chat asks what was wrong, optional): the admins see the
  reasons (`superset supagent gaps`), and what they say about the data is proposed to the memory.
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
  theirs with the chat's *Memory* button; admins review the team's in *Data dictionary → To review*.

### A second, independent computation (0.7, a test: off)

`agent.cross_check` (off): when the classic agent answers with figures from its own queries, the governed
pipeline computes the same question its own way, and the answer says so when its figures differ from the
answer's headline figures. Measured on the lab's 112 labelled classic answers of the domain benchmark (56
questions; 21 answers wrong): it decided 45% of them (the governed side gives the rest to the classic agent or
asks back); it disagreed with 4 of the 21 wrong answers (19%) and with 12 of the 91 right ones (13%): a
disagreement meant a wrong answer 1 time in 4, and an agreement a right one 85% of the time (81% without the
check), for 24 s more per answer (median). Not worth it as it is: it stays off.

## Measuring it

**LLM usage** (admins: the tab next to *Settings*): every call to the LLM, recorded with what it was for
(the chat's answers, the daily learning, the Context, the memory, the learned answers, the agent
catalog, tidying, tests), for whom, its model, its context size, the tokens written, the share from the
LLM server's prompt cache, its time and whether it failed. For a period (24 hours, 7, 30 or 90 days, or
dates; by hour or by day): totals, tokens or calls over time by task, per person, per task, per model,
the answers (time, LLM and tool calls per answer, sent back by the checks, marked), the biggest
contexts and the slowest answers. Kept `usage.keep_days` days (90).

The commands also say:

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
pieces are the learned metrics and indices, the catalog's rules, notes, glossary terms and formulas,
the learned answers, the memories, the documents, the Context pages and Superset's charts and
dashboards. They are found:

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
ranking. Pieces follow their origin at once: a description written in the Data dictionary, a
catalog entry saved, deleted, restored or imported, a memory, a document, a Context page: its
pieces are written and their vectors computed straight away (more than 64 at once: at the next
indexing, hourly, `embed.per_run` at a time). Each glossary term is its own piece. A change also
makes every web server and worker read the dictionary, the catalog and what not to use again at
its next question (a stamp in Superset's database). A description the catalog no longer gives is
taken back from the dictionary, unless a person wrote another one since.

`superset supagent check-knowledge` says whether everything the team put in is given to the agent:
pieces out of step or without a vector, catalog entries ignored (invalid) or in conflict, names
the catalog describes that the dictionary does not have (misspelled, or not learned yet: their text
reaches nothing), rules not given in full (the first 30, 1,500 characters each, are in the
instructions; the others are found by the search), team memories and learned answers waiting for
an admin, documents that could not be read. Exit code 1 when there is a problem.

### A cross-encoder reranker (0.6, optional)

`rerank.url` (empty: none): the first `rerank.depth` pieces the search found (40) are read with the question by a
reranker model served over HTTP (a cross-encoder reads the question and the piece together, which the words and
the vectors cannot), then ordered by both ranks, the search's and the reranker's (`rerank.weight`). APIs:
`rerank.api = jina` (Jina, Cohere, vLLM, llama.cpp's server with `--reranking`, Infinity) or `tei`
(text-embeddings-inference); `rerank.auth` none, token or the LLM's (on the same gateway as the embeddings). A
reranker that fails or answers later than `rerank.timeout` (2 s) is skipped for a minute: the search's own order.
The router never waits for it. Serve a multilingual model next to the embeddings (e.g. Qwen3-Reranker-0.6B or
bge-reranker-v2-m3), and measure it on your data before switching it on: on the lab at production size (64,016
objects, no learned answers yet), a generic multilingual cross-encoder (jina-reranker-v2-base-multilingual) made
the decider's ranking worse (MRR 0.535 to 0.49 with equal weights; worse with more weight), preferring the short,
on-topic descriptions of look-alike metrics over the real tables' pieces; on a CPU it also took 8.5 s per question.

### The knowledge store in PostgreSQL (0.6)

With PostgreSQL and its extensions **pgvector** (`vector`, 0.7 or later for `halfvec`), **pg_trgm** and **pg_textsearch**
(PostgreSQL 17 or 18; each extension is optional, the store uses those it finds), the search runs in PostgreSQL instead (`supagent/knowledge/pgstore.py`), three ways at once, the ranks fused:

* **words**: BM25 of pg_textsearch (a rare word counts more than a common one), on the words of each piece (names
  cut at `_` and at capitals, light stems, English and French stop words out); without pg_textsearch, PostgreSQL's
  full-text search with an index;
* **near spellings**: pg_trgm on the names and values of the data (metrics, indices, labels, fields, the values
  seen, synonyms, glossary terms): a name or a value typed with a typo or as a variant (`BILLNG`,
  `node_cpu_secnds_total`) still finds its table;
* **meaning**: the vectors of `embed.model` in an HNSW index (`halfvec`), so no process holds them in memory,
  however many pieces there are.

It also keeps **each user's chats** (their questions, the beginning of the answers, the tables they read; found by
their owner only: the tool `search_my_chats`, offered when a question refers to an earlier chat) and the **routes
people confirmed** (Helpful, an admin's confirmation, the reply to a question back): the governed decider counts
the tables that answered the questions most like a new one (`neighbors`, with the pieces found by near spelling as
`spelling`), and its gate learns how much to trust them.

The store is derived and can be wiped at any time: it lives in a schema of its own (`search.store_schema`, default
`supagent_store`) in Superset's database, or in another PostgreSQL (`search.store_uri`); `superset supagent init`
builds it the first time it finds the extensions, `superset supagent store rebuild` builds a new version beside the
one in use and then switches to it, `store wipe` drops the schema (the search of 0.5 answers until the next
rebuild), `store status` shows the extensions, their versions, the size and the warnings, `store search` searches it
as a user would. It follows the pieces as they change, hourly for the chats, routes and names, and is built again
when the embedding model changes. Its connections are its own (autocommit, closed after use). `search.store = off`
goes back to the search of 0.5 without touching anything else, and the vectors stay in `supagent_chunk`, so going
back to 0.5.4 needs no new embedding. Settings: `search.store`, `search.store_uri`, `search.store_schema`,
`search.bm25`, `search.chats`, `search.chat_days`, `embed.query_instruction`.

**pg_textsearch before 0.6.1**: tested on PostgreSQL 18.3 with 0.5.0, a `ROLLBACK` after an error inside a
transaction can fail with `ResourceOwnerEnlarge called after release started` (pg_textsearch issue #247). That
happens in every session when the library is in `shared_preload_libraries`, and without it in sessions that touched
a BM25 index. The session is then stuck in its aborted transaction until it is closed. The store never runs a
transaction there, but Superset's own sessions do: do not preload 0.5.x, and upgrade to 1.x (it needs the preload
and has the fix). `store status` warns about it.

## Documents and sites

Admins add documents (text, Markdown or HTML files) and web pages or whole sites (the pages
under the same address, up to a number of pages, read again every N days) in *Data dictionary →
Knowledge → Documents and sites*. Fetching is safe by default: http(s) only, `docs.max_kb` per page, 20 s per
request, redirects checked; only `docs.allowed_domains` are read (with none set, only public addresses: no
intranet, no localhost). PDF is not read (it would need a package Superset does not have).

**A site behind a sign-in** (0.8): a token (`Authorization: Bearer`), a user with a password or an app token
(Basic), or a token in a header of its own (e.g. `Private-Token`). The secret is kept encrypted with Superset's
SECRET_KEY, sent only to the document's own site (never after a redirect to another site), never shown on the page
nor written in a log or an error; an empty secret in Edit keeps the saved one, an address moved to another site
needs it again. A sign-in page or an HTTP 401/403 says "the site asked to sign in". What a sign-in reads becomes
searchable by everyone who can open the Data dictionary: use a token that only reads what they may see.

**Wikis and repositories** (0.8), detected from the address (or chosen: *Read as*):

* **Confluence** (Data Center / Server: `/display/SPACE/...`, `/pages/viewpage.action?pageId=`, `/spaces/SPACE/...`;
  Cloud: `/wiki/spaces/...`): the page and the pages under it, breadth first, or a whole space, through the REST
  API (the page's storage format, code macros and tables kept), up to the number of pages asked.
* **Bitbucket** (Data Center: `/projects/P/repos/R/browse/...`; Cloud: `bitbucket.org/ws/repo/src/...`): the text
  files of a folder (Markdown, txt, rst, adoc, HTML, YAML, JSON, CSV, config, XML, SQL), README and docs first, each
  at most `docs.max_kb`, through the REST API.
* Other addresses: the web pages under the same address.

**In the search** (0.8): each page is cut in its own pieces (a piece never mixes two pages), each with the page's
address, so the agent finds the passage a question needs and names its page; they go in the knowledge store when
Superset's PostgreSQL has pgvector, pg_textsearch or pg_trgm (words, near spellings, meaning), else in the search of
0.5. The table says how many pieces each document has in the search.

## Backups of the knowledge (0.9)

What people and the learning put into the agent's knowledge is long to make again. A **backup** is one zip file
holding its latest state, a part per folder:

| Part | What it holds |
|---|---|
| `categories` | the values of the categories, what they are part of, the items they are given to, the relations and the interactions of the System map, the map's arrangement, the categories themselves (their fields, their descriptions) |
| `catalog` | the catalog's entries and their versions |
| `memory` | the team's and the users' memories |
| `documents` | the documents and sites, with their texts |
| `notes` | the notes |
| `context` | the Context pages |
| `learned` | the learned answers and the investigation paths, the examples, the words that lead to a table, the kinds of work people confirmed |
| `dictionary` | the descriptions of the data's objects (written by people or by the LLM), their units, other names and categories |
| `settings` | the settings |
| `vectors` | (`backup.vectors`, off) the vectors of the search pieces: several times bigger files, and a full restore then needs no embedding |

**Never a secret** in a file (the LLM's token, a site's sign-in: on the same Superset a restore keeps them, elsewhere
they are entered again), and not the chats, the usage figures nor the runs.

* **Every day** at `backup.hour` (`backup.enabled`, on; `backup.days`) one is made by the workers' beat, before the
  learning; the last `backup.keep` (14) are kept: the history to go back to. *Settings → Backups of the knowledge →
  Back up now*, or `superset supagent backup`, makes one at once (without the beat, plan that command yourself).
* **Where**: `backup.dir` on the server (default: `supagent-backups` in Superset's home; files readable by their
  owner only). With several servers, give them a directory they share: the scheduled backup is written by the
  worker, the page lists and restores the files of the web server. *Download* keeps one elsewhere.
* **Restore**, whole or by part: *Restore…* on a backup, choose the parts, confirm; or `superset supagent restore
  <name> --parts categories,catalog --yes` (without `--yes` it lists what the backup holds). The present rows of
  each part asked are replaced by the backup's, with their ids; the other parts stay as they are. **The present
  state is saved first** in a backup of its own (`…-before-restore.zip`, the last 5 kept): a restore can be undone.
  A part put back alone may name items that no longer exist (a category given to an entry deleted since): they
  are simply not shown. The descriptions of the data go back on the objects of the same name (the objects
  themselves are learned again from the data). The search follows at once; the vectors of what came back are
  computed by the hourly indexing (or `superset supagent index`).
* Backups and restores are **runs**, listed in the settings with the learning runs (their parts, their counts,
  their time); never while another run is running. A backup made by a later version is refused.

`superset supagent backups` lists them. For the whole of Superset's database (the chats too), your database's own
dump stays the tool; this one is the knowledge, by part.

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
| supagent_association | words of the questions and the data they led to |
| supagent_context | the Context pages |
| supagent_usage, supagent_llm_call | where the time of the answers goes; the LLM calls (the LLM usage page) |
| supagent_route | per answer: the tables shown, chosen and read, and the router's route (0.6) |
| supagent_facet, supagent_tag, supagent_link, supagent_classified | the categories, the items' tags and relations, what was classified (0.6) |
| supagent_item_use | which learned items each answer was given and used (their ranking, 0.6) |
| supagent_note | the notes users write (team or personal, 0.7) |
| supagent_chart_scan | per chart: the nightly look's findings and what it shows (0.7) |
| supagent_meta | the schema version and the workers' heartbeats |
| supagent_document | the 0.1 catalog, kept as a backup after the upgrade |

The knowledge store (0.6, optional) is a schema of its own, `supagent_store` (`search.store_schema`),
derived from these tables: `superset supagent store wipe` drops it, `store rebuild` builds it again.

Files are kept in the database so that the web server can serve what a worker on another
host wrote. Files bigger than `tools.max_file_mb` (default 50 MB) are named but not kept.

## Command line

```
superset supagent init                 tables, permissions, role "AI Agent"
superset supagent settings [--set k=v] [--unset k]
superset supagent test-llm [--profile]                                  the LLM (--profile: thinking, tools, cache)
superset supagent stats [--days N] | evaluate | gaps [--days N]         where the time goes, resolver, gaps
superset supagent learn [--database NAME] [--no-llm] [--minutes N] [--plan] [--stop]
superset supagent context [--build] [--no-llm] [--force]                 the Context: its pages, or build it now
superset supagent prompt "question" --user U [--full]                    what the LLM is given for a question
superset supagent import-catalog FILE [--replace] | export-catalog
superset supagent agent-catalog [--no-docs]                              entries the agent is certain of
superset supagent tidy-learned [--limit N]                               generic questions, duplicates merged
superset supagent forget-learned [--database D] [--everything] [--yes]    learn again from scratch (dry run
                                                                          without --yes)
superset supagent remove-auto-learned                                    answers 0.2.1 saved by themselves
superset supagent index [--refresh-docs]                                 searchable pieces and vectors
superset supagent check-knowledge [--json]                               is everything put in given to the agent?
superset supagent search "words" [--user U]                              what the agent would find
superset supagent knowledge [--changes DAYS]
superset supagent describe "words" [--name INDEX_OR_METRIC] [--user U]   what the agent reads
superset supagent ask "question" --user U [--pipeline classic|governed]  an answer in this process
superset supagent classify [--minutes N] [--limit N]                      categories of the knowledge, now: a run
                                                                          listed in the settings with its steps (0.9)
superset supagent interactions [--minutes N] [--limit N] [--again]        the interactions the texts state, proposed (0.9)
superset supagent check-system [--question "..."] [--user U] [--json]     what the agent knows of the system, and
                                                                          what is missing for investigations (0.9)
superset supagent backup [--vectors] | backups                            a backup of the knowledge now; the list (0.9)
superset supagent restore NAME [--parts a,b] [--yes]                      a backup put back, whole or by part (0.9)
superset supagent charts [--scan] [--understand] [--chart N]              the nightly look at the charts, now (0.7)
superset supagent store status | rebuild | sync | wipe [--yes] | search "words" [--user U] [--chats]
                                                                          the knowledge store (0.6)
superset supagent grant USER...
superset supagent push-descriptions [--labels] | push-metrics            catalog -> datasets
superset supagent mcp [--host 127.0.0.1 --port 5009]                     the tools for other agents
```

## Other agents (MCP)

`superset supagent mcp` serves the same tools over MCP (streamable HTTP) for another agent. It
acts as the Superset user `mcp.user` (default `MCP_DEV_USERNAME`). Keep it on localhost or
behind your gateway.

## Operations

* **Where answers run**: `agent.executor` = `auto` (the Celery workers while one is alive,
  else a thread of the web server; see *Celery workers and beat*), `celery` or `thread`. An answer with no progress for 35 minutes
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
  used for that many days, with their messages and files (90: three months); what they taught
  stays. The delivered messages of a queue in Superset's database: `agent.queue_keep_days`
  (default 90).
* **The chat panel** on Superset's pages is added through Superset's own place for custom page
  scripts (`tail_js_custom_extra.html`; what a deployment put there is kept), only for the users
  who may chat, never on embedded or standalone dashboards. It shows the chat page in a frame of
  the same site: Talisman's default `frame_options` (SAMEORIGIN) allows it; with DENY the panel
  says so and offers the full page. Drag its left edge to resize it; it stays open from page to
  page in the browser tab.
* **Stop**: the chat's Stop button stops the answer at once: the chat takes the next question
  right away. A step already running (an LLM call, a query) ends on its own in the
  background; its result is thrown away and the agent does nothing more for that answer.
* **Upgrade**: `pip install` the new wheel, `superset supagent init`, then restart (from 0.5.4
  to 0.6.0: *Upgrade from 0.5.4 to 0.6.0* above, with the checks before). From 0.1:
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
