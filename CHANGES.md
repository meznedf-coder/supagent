# Changes

## 0.5.2 (2026-09-30)

* A short follow-up that asks for something new ("And on 22 September?", "The CPU of srv-amer-002
  yesterday.") is no longer read as completing the previous question, nor answered from the chat's
  results without a query: 0.5.1 could answer "And on 22 September?" with the number of the 23rd,
  unmarked. Such an answer, still without a query after the reminder, is marked.
* "database 4" names a database; a bare "base 4" no longer does.

## 0.5.1 (2026-09-30)

Checks that run **before** a query or a saved chart, instead of after the answer (a model told
afterwards often keeps its answer; a call sent back before it runs is written again). Each sends the
call back with what to change, every reason at once. Sent again unchanged, a call left out of a rule,
a period or a counter runs (the question may mean it; a rule left out is then marked in the answer);
a call on another database never does (it is read only when the question names it: its name, or
"database 4").

* **The team's rules**: a query or a saved chart on data that has the field or label of a filtering
  rule ("Exclude the UAT environment (ENVIRONMENT_TYPE = 'UAT')") and does not use it, with the
  condition to add (`"ENVIRONMENT_TYPE" <> 'UAT'`, a chart filter, a PromQL matcher). Also the SQL of
  e-mails, SQL Lab saved queries and `compare_to_usual`.
* **The right database** when the same index or metric is in several (two OpenSearch clusters, a DR
  replica, one Mimir reached directly, federated and through a gateway): the database the question
  names (its own words next to a word for a database, its whole name, a tenant only it has), and the
  database of the charts and dashboards the question or the chat is about (an id, a link, a title,
  or read by a tool of the answer). In the lab, questions about the dashboard of the gateway's
  tenants were answered from the federated database (same metric, other data).
* **The question's period**: a question with a date or a period needs a filter on the time of the
  data (the index's time field, `ts` of metrics), not on business dates unless it speaks of them;
  one whole day needs the whole day, a span of days ("the week of 14 to 20 September") exactly those
  days (in the lab: `POSITION_LABEL = 'D-1'` for "on 23 September", a chart of "23 September" built
  on 00:00-06:00, a week ending at 06:00 the next day).
* **Counters counted as samples**: `COUNT(*)` on a counter, histogram or summary of a metrics
  database counts samples, not requests or errors (in the lab: "66.67 % availability, 2,880 errors"
  that were sample counts, and "201,600 jobs over 45 minutes" from a histogram bucket); the call is
  sent back with `SUM(increase)`.

Also:
* When the same index or metric is in several databases, "Where the data is" names the one to use and
  why: `agent.preferred_databases` (new setting, names or ids in order), then the catalog's metrics
  database, then the database the team's charts use for it (before: the lowest score, then the
  catalog, then the lowest id). Chart and dashboard pieces of the knowledge name their database.
* A value the question names that is in several kinds of data (an application of the jobs index and a
  label of HTTP metrics) is shown with where it is when the question does not say which; the answer
  says which one it took, and when it read only one of them and does not name the other, a line under
  it does ("Not read for this answer: BILLING_API is also a value of the label application of 10
  metrics ... Ask if you meant that data."). The lookup reads one label per name, not every metric's.
* A reply to the agent's question back ("The failed jobs.") comes with the question it answers, and so
  does a short reply that completes the previous question after an answer ("The failed jobs.", "Only
  PROD."): in the lab the model then answered for every application.
* A list continued past the results ("... up to `srv-amer-199`") that stays after the check is taken
  out of the answer (the lines before it stay) instead of marking the whole answer.
* A follow-up answered from the chat's own results (every number in them) is not sent back for a new
  query.
* A saved chart asked for the top N that shows more categories is told NOT DONE, with how to do it (in
  the lab the model listed the first 5 rows of the chart, sorted by name, as "the top 5").
* Names in `code` are checked like the others (`srv-amer-200`, a series continued, was missed in
  backticks); `n1-n5` of two given names is not flagged. A big result says how many rows have an empty
  value (201 "servers" were 200 and one row without a server).
* The numbers check no longer reads the date of "now" as a period (30 September was 43,200 minutes:
  a made-up 43 passed on the 30th of the month); the rest of a share the answer shows (99.00 %
  available next to 1.00 % of errors), a converted value rounded twice (19.1949 GiB written 19.20)
  and the unit constants (1,073,741,824 bytes per GiB) are not marked as made up, nor a number said
  as rounded ("roughly 12,300", to the hundred). The numbers check
  asks to delete the sentences it cannot support, rather than to keep them.
* "How many" answered with shares or rates only is sent back once for the count; a result cut by the
  SQL's own LIMIT says that more rows may match (in the lab "61 of the 100 failures" were 100 rows of
  358).
* A question of many parts (a manager's summary of five figures) gets half as many tool calls more;
  a tool call the model writes as text when its calls are used up is not shown as the answer (the
  results it had are); the instructions tell to compare periods one query per period.
* Charts the agent saves or changes get the query context Superset's front end would save (Superset's
  MCP service saves none, so their data API, CSV and text reports failed with "Chart has no query context
  saved"), mixed charts with their two queries; a chart made in Explore keeps its own; a chart type it
  cannot be written for keeps none.

## 0.5.0 (2026-09-29)

* **Context**: every night (after the day's learning) the agent writes the system's documentation,
  *Functional* and *Technical* (Data dictionary -> Context), from what the team shares (documents and
  sites, catalog, team memory, data dictionary, Helpful answers; not the raw chats). Facts pages are
  written without the LLM (each data source, each inventory of servers, services, applications,
  environments, regions, clusters and teams, the glossary, the rules and facts); summary pages (how
  the system works, the technical overview, one page per main application) by the LLM from the
  evidence only, citing it, marked AI-written, and only when their evidence changed (at most
  `context.max_llm_calls` calls per build). A page is shown only to the users who may query every
  database it draws from; admins correct pages (never written over); the agent finds them in its
  knowledge search, below the catalog and the documents. `superset supagent context --build`.
  Tables: `superset supagent init` adds `supagent_context` (schema 5).
* **Data dictionary**: a *Knowledge* tab (the catalog, the documents and sites, the team memory, read
  only) and, in the *Learned* tab, what the agent learned by itself (its catalog entries with their
  evidence, where the data of the questions was found, the relations measured, the AI descriptions
  to verify), each filtered by the databases the user may query. The long lists (query timings,
  changes, relations, and the runs of the settings page) come in pages, like Browse.
* `superset supagent prompt "question" --user U`: what the LLM is given for a question (the team's
  rules, the user's and the team's memory, where the data is with its descriptions, the knowledge
  found: documents, catalog, Context, learned answers), without asking the LLM. A test checks, end
  to end, that each of them reaches the LLM, and nothing the user may not see.
* **Nothing the team puts in is missed, nothing given twice**:
  * a description written in the Data dictionary, a catalog entry saved, deleted, restored or
    imported is used by the next answer on every server (a knowledge stamp in Superset's database
    makes each web server and worker read the dictionary, the catalog and what not to use again,
    instead of after 1 to 5 minutes), and the agent's search finds it at once (its pieces written
    and embedded straight away; before: at the next learning run). A description the catalog no
    longer gives is taken back (unless a person wrote another one since);
  * the team's words: the glossary terms of a question (their words, a code such as D-1 or W-4 that
    their definition uses, the terms they name) are given in full with the question, before where
    the data is; each term is also its own piece of the search (the whole glossary was one piece,
    rarely found);
  * the knowledge found with a question no longer repeats what the other parts give (a metric of
    the same name in two databases, the metrics of "Where the data is", the learned answers, the
    memories, the team's rules): its room goes to the catalog, the documents and the Context (a
    Context page counted as found much lower than it was; now at most two, the page about a subject
    of the question first);
  * the columns the OpenSearch connector computes (the business-day label and time of osagg) are in
    the dictionary with what they are, and the catalog's descriptions reach them;
  * saving a memory or a document no longer reads the whole dictionary again.
* `superset supagent check-knowledge`: is everything the team put in given to the agent? Pieces out
  of step, pieces without a vector, catalog entries ignored (invalid) or in conflict, names the
  catalog describes that the dictionary does not have (misspelled, or not learned: their text reaches
  nothing), rules not given in full, what waits for an admin. Exit code 1 when there is a problem.
* **Numbers checked**: every number of an answer must come from what the agent was given (a value
  of a query result, as shown or rounded, a share as a percentage, seconds in minutes, bytes in GB, a
  column's total or average, the total of its first rows, a row count, a rate within a row, a
  duration between two times; the question's, the chat's and the knowledge's numbers). Otherwise the
  agent is asked once to take it from a query (totals, rates, differences computed in the query),
  and a number still made up is marked in the answer. Replayed on the lab's 271 stored answers, it
  caught totals added up wrongly (7,014 or 6,461 failed jobs for 7,011). Setting
  `agent.check_numbers` (on).
* **The team's rules are applied**: in the lab the model ignored a catalog rule ("exclude UAT unless
  asked") in 4 answers out of 4, although it was in its instructions. The rules now also come next
  to the question (in full when short) and win over the learned answers; and a rule that filters on a
  field or label and asks for it (exclude, only, unless, never...: `ENVIRONMENT_TYPE = 'UAT'`) is
  checked on the queries: a query on data that has that field and does not use it sends the answer
  back once, then the answer is marked (unless the question asks for the rule's value); a rule that
  only says what a value means ("KO = failed") is never checked that way.
* **Names too**: a server, host or other name with digits that the answer writes must come from what
  the agent was given (in the lab it answered "srv-amer-000 through srv-amer-199" after seeing 25 of
  the servers: the series was continued, and wrong).
* **A value that does not exist** ("the jobs of ZEPHYR"): a count of 0 now comes with the
  dictionary's note that the value is not one of the field's values, so the answer says so instead of
  "0 jobs failed".
* **LLM usage page** (admins: the *LLM usage* tab next to *Settings*): every call to the LLM is recorded
  with what it was for (the answers in the chat, the daily learning, the Context, the memory, learned
  answers, the agent catalog, tidying, tests), for whom, its model, its context size (tokens sent), the
  tokens written, how much came from the LLM server's prompt cache, its time, and whether it failed. The
  page shows them for a period (the last 24 hours, 7, 30 or 90 days, or dates; by hour or by day, in
  the viewer's time zone): totals, tokens or calls over time by task, per person, per task, per model,
  the answers (time, LLM and tool calls per answer, answers sent back by the checks, answers marked),
  the biggest contexts and the slowest answers. Calls are kept `usage.keep_days` (90). Table
  `supagent_llm_call` (schema 7: `superset supagent init`).
* **Fixed**: a data source's Context page listed its links with the other data sources, naming their
  metrics to users who may not query them; the links are now pages of their own, seen only by the users
  who may query both databases.
* **What is happening now**: "what is happening in production / with an application / on this metric,
  chart or dashboard", "is everything normal" get a procedure and the tools for it: the dashboards and
  charts about the subject (Superset's charts and dashboards are now part of the knowledge: what each
  shows, its dataset, metrics, filters, the dashboards it is on; seen only by the users Superset lets
  open the chart, or the dashboard and all its charts), their latest data (`get_chart_data`), the team's
  checks and limits, the alerts
  firing, the comparison with the usual (the same window of the previous weeks), and a screenshot of
  a chart that shows a problem.
* **Follow-ups get the tools they need**: "what charts are in it?" after a question about a dashboard
  had no Superset tool (it named no chart nor dashboard); a question that refers back now gets the
  tools and instructions of the one it refers to. A saved chart must have a name saying what it shows
  and its period (Superset's automatic names repeated "Sum(x) by y" for two different charts).
* **More of Superset's MCP tools**: save a query in SQL Lab (`save_sql_query`), a link that opens SQL
  Lab with a query (`open_sql_lab_with_context`), a link to explore a dataset (`generate_explore_link`),
  the data of an existing chart (`get_chart_data`), offered when a question asks for them; an answer
  saying a query was saved in SQL Lab is checked like a chart or a dashboard. After a chart is saved or
  changed, the agent is told what the saved chart returns (rows, first values), so that its answer
  describes the chart, not the intent; a chart of a period on a table must filter that period.
* **Mimir tenants**: `__tenant_id__` usually separates applications or subjects: the agent filters on
  the tenant a question is about, groups by it to compare, never adds tenants up unless asked, and
  names them; "Where the data is" lists a metric's tenants, and the Context inventory has a Tenants
  section.
* **The Context stays short with a big dictionary** (10,000 metrics, 80,000 labels): a data source's
  page lists the 25 metrics and indices that matter (a person's description, the catalog, the ones the
  answers used and the learned answers), the rest counted by domain, not every description.
* **Results named, tries not shown**: an answer's results were shown as "Result 1", "Result 2"... with
  nothing to tell them apart. A query run again in the same shape (the same table, columns and time
  window: fixed after an error, a check or a rule) now replaces its earlier try (still listed in the
  answer's tool calls) when it only adds conditions; other conditions (PROD, then UAT) are a comparison
  and both stay. Each result is named by what it shows ("FAILED by APPLICATION · 23 Sep", "CPU busy %
  over time · 23 Sep 00:00-08:00"); two results of the same name say what differs (their filters).
* **A reply the LLM server cannot read** (a tool call of 53,000 characters, cut): the model is asked
  once again with short arguments instead of the answer failing with an HTTP 500; after that, it says
  what it did. An aggregate inside an aggregate on the metrics (`AVG(SUM(...))`) now comes with the way
  to write it (the ratio of the two sums is already the share over the window; the 5 busiest with
  ORDER BY ... LIMIT 5).
* **Dashboards of several charts**: the agent has twice the tool calls when it builds charts and
  dashboards, is told not to spend them re-checking the data, and when the calls run out it writes
  what it saved (with the links) and what remains, instead of "stopped after too many tool calls".
  A dataset on OpenSearch whose SQL has an ORDER BY could not be created (osagg turned the LIMIT 0
  Superset adds into a top-0 request that OpenSearch refuses): the dataset's SQL is wrapped.
* **AI descriptions**: a unit is written only when the name spells it or the values prove it (in the
  lab a duration in seconds, named `..._d`, was described "in days"); in "Where the data is" an
  unverified AI description is marked as such.
* **The agent asks when a question can mean two things** that give different numbers (two fields or
  metrics that fit, a term nobody defined): one short question naming the readings and the one it
  would take, instead of guessing; otherwise it says in one line which reading it took. The user's
  answer is learned (a team definition, for an admin to approve).
* **No copies in the team's knowledge, and a real Delete**: writing again a memory that exists gave a
  second one (removing a memory only disabled it, and the next one was a new copy); a catalog entry
  could be created twice. Now a memory written again is the same one (brought back if a person writes
  it after it was disabled; one learned again from a chat stays refused), Delete removes a memory for
  good (the user's own, or any for an admin, with *Delete* in the settings' Team memory list; *Disable*
  keeps one unused), `superset supagent init` merges the copies already there, and the catalog refuses
  a second entry of the same title, or of the same classification and content ("edit that one").
* **Not helpful, and why**: after Not helpful, the chat asks what was wrong (optional). The reason is
  shown to the admins (`superset supagent gaps`) and what it says about the data is proposed to the
  memory. Column `feedback_reason` (schema 6).
* **The chat panel on Superset's pages**: with a long conversation open, the buttons Conversations,
  New conversation and Memory scrolled out of sight (the panel's page grew with the conversation);
  now only the messages scroll.
* **Fixed (0.4.8)**: "not used" was also read inside an explanation ("mode idle = unused" in the
  description of a CPU metric made the agent refuse that metric). It now counts only when the
  description, or one of its sentences, starts by saying so.

## 0.4.8 (2026-09-29)

* **Learning much faster on metric names with dots**: the batched Mimir requests (series counts,
  depth of the history) escaped a dot in a way PromQL refuses ("unknown escape sequence"), and every
  batch with such a name fell back to one request per metric (the history: about 7 per metric).
* **No more paying twice for label descriptions**: labels found on newly profiled metrics take the
  description of the same name in the same database (a person's first) without an LLM call; the
  descriptions show the LLM calls and tokens apart from the copies.
* **Steps of a run**: every run lists what it did, step by step (per database: listed, new, due,
  profiled, requests, errors, batches read one at a time with their first error, history, changes
  by type; then descriptions, relations, catalog, categories, agent catalog, generic questions,
  search index, check), with times and durations, also for a run in progress and for the step a
  stopped run was doing; each step is also in the server log. "Objects learned or updated" (which
  counted every object seen again) is replaced by the new objects found.
* **The search index no longer embeds every metric again after each run**: its text had the series
  count and sampled label values, which change every run.
* **"Not used" is honoured**: a person's description saying a field, label, metric or index is not
  used keeps the agent from proposing it and from querying it (a query or chart that uses it is
  refused with the team's words).

## 0.4.7 (2026-09-29)

* **Questions about an earlier answer**: "create the chart in Superset of that finding" was
  answered from the question's own few words (the agent looked for other data, and saved a chart
  of something else). Now the agent is given, with the new question, what the last two answers
  were computed from: the queries that gave their rows (tool, database, SQL or PromQL, time window,
  columns; a query only written in an answer's text is marked as not run), and a question that
  refers back looks for the data of the question it refers to.
* **CPU usage** hints give the busy % (`100 * SUM(rate) FILTER (WHERE mode <> 'idle') / SUM(rate)`)
  for CPU-seconds metrics with a `mode` label: the sum of every mode is the number of cores, which
  an answer about "CPU usage" could show as a flat 4.00.
* **Charts of a calculated finding**: a percentage, a ratio or PromQL is saved as a Superset
  virtual dataset (`create_virtual_dataset`, only offered when a chart is asked; PromQL through
  promagg's `promql()`), then charted with the finding's time range. The tool checks the Dataset
  write permission (and database access for `promql()`), lets Superset check the SQL's tables, and
  gives back the same dataset when asked again. A chart field that does not exist now points to it.
* **Stop learning is immediate**: the run is marked stopped at once (also one whose process
  died), and *Learn now* starts a new one right away (before: "stopping" until its next step, which
  could be the end of a long LLM call, and a new run waited). The run also stops while it waits for
  the last descriptions or for the people's answers.

## 0.4.6 (2026-09-29)

* The same code as 0.4.5, published under a new version number (a package mirror that
  had kept 0.4.5 as not found fetches 0.4.6 as new). Everything below 0.4.5 applies.

## 0.4.5 (2026-09-29)

* **Celery workers without Redis, several web servers and workers**: the workers can use
  Superset's own database as their queue (`broker_url = "sqla+" + SQLALCHEMY_DATABASE_URI`, five
  lines in `superset_config.py`, see *Celery workers and beat*). Every worker writes a heartbeat
  in Superset's database (every 30 seconds, removed when it stops), so `agent.executor` = `auto`
  sees the workers with any broker (a queue in a database cannot carry Celery's ping), and the
  settings page counts them.
* **One question, one answer**: a question is answered by the first process that starts it (worker
  or web server), the others leave it; a busy worker leaves the next questions to the other
  workers. In `auto`, a question no worker started within 15 seconds is answered by the web server
  that received it; with no worker alive, the web server answers at once.
* `agent.queue_keep_days` (default 90): the delivered messages of a queue in Superset's database
  are deleted after that many days (Celery never deletes them), with the heartbeats of workers
  gone for a day, by the hourly tick.
* **Learning**: the databases never learned come first, each database gets a fair share of the
  time left (a database of thousands of metrics no longer keeps the others waiting for days), and
  the runs list shows, while the run goes, the database being learned, the ones still to come and
  the ones left out with the reason (before, only at the end of the run). One run at a time for
  all the hosts (a row lock while a run starts).
* A file made by an earlier answer (Excel, chart image) is found by the next answer on any worker
  or web server (it is kept in Superset's database), for the same user only.

## 0.4.4 (2026-09-29)

* **AI descriptions during the learning**: the LLM describes what has no description while the
  databases are read (the learner waits for the databases most of the time: the LLM works
  meanwhile), as the metrics and indices are learned, instead of after a database (0.4.3) or after
  the whole run (0.4.2). A Mimir of thousands of metrics that takes two hours to learn gets its
  descriptions during those two hours. Once every database is read: the relations between them
  all, the catalog, then the descriptions still missing with the time left.
* A run in progress shows in the runs list how many objects it learned or updated, and how many
  AI descriptions it wrote so far.

## 0.4.3 (2026-09-29)

* **Every database is learned, and a run says which ones it left out and why**: each run lists
  the osagg and promagg databases it did not learn, with the reason (not in `learn.databases`,
  or the learning user `learn.user` may not read it), and the names of `learn.databases` that
  match no database; so does `superset supagent learn --plan`. (A run learns every osagg and
  promagg database the learning user may read, unless `learn.databases` lists some.)
* **Descriptions during the learning, database by database**: each database is learned then
  described by the LLM (after the catalog), with a fair share of the time left, before the next
  database; the relations between them all are measured at the end, and the time left goes to
  the descriptions a database's share did not reach. The runs list shows each database's
  descriptions.
* **Stop learning**: a button on the settings page (and `superset supagent learn --stop`) stops the
  running run at its next step; it keeps what it learned and ends as "stopped", and *Learn now*
  starts a new one as soon as it stopped (a run whose process died does not block the next one
  for more than a minute and a half).

## 0.4.2 (2026-09-29)

* The chat panel's ⤢ button opens the full chat page again (in 0.4.1 it closed the panel: it was
  taken for a click on the Chat tab).

## 0.4.1 (2026-09-29)

* **The chat beside Superset's pages**: the Chat tab of the top bar no longer leaves the page: it
  opens the chat as a panel on the right, and the dashboard, chart, dataset or SQL Lab page stays
  usable beside it (it narrows to make room). A link to Superset in an answer (a chart, a
  dashboard) opens in the page while the chat stays open; other sites open in a new tab. The
  panel stays open from page to page in the browser tab, can be resized by dragging its left
  edge, and follows Superset's light or dark theme. Ctrl+click on the tab, or the panel's ⤢
  button, still opens the full chat page. Only for the users who may chat; never on embedded or
  standalone dashboards. Added through Superset's own place for custom page scripts
  (`tail_js_custom_extra.html`, kept as the deployment has it): nothing to configure.

## 0.4.0 (2026-09-29)

Faster answers, fewer wasted calls, learning runs that never block, and commands to measure it.
Nothing added to the pages or the chat; the same install (pip, `superset supagent init`, restart).

* **The instructions stay in the LLM server's prompt cache**: what is found for a question (where
  the data is, the knowledge, the learned answers, the user's memories) now comes with the
  question instead of inside the instructions, which (with the tools) are the same from one
  question and one user to the next. Measured on the lab LLM (llama.cpp): the first step of an
  answer processed 4,500-4,700 prompt tokens in 25-27 s; now 1,300-1,550 in 9-11 s (3,188 from
  the cache).
* **An empty result says why**, from the data dictionary (no extra query): a value in the wrong
  case ("'failed' is written 'FAILED'"), not a value of the field or label (the closest ones), a
  time window before the data starts or after it stopped.
* **No call twice**: an identical call that succeeded is not run again in the same answer
  (reading again after a save is allowed).
* **An empty LLM answer** is asked again twice; still empty after a query succeeded, the answer
  shows that result instead of failing.
* **An answer that announces a step without taking it** ("Let me run the query.") is sent back
  once; offers ("if you want, I can...") are not.
* **All-clear claims are checked**: an answer that says all is well while a check or a query it
  relies on could not run gets a one-line correction.
* **compare_to_usual**: is a metric unusual for this time? The same window on the previous weeks
  (median and median absolute deviation per series: normal, high, low; unknown with fewer than
  three weeks). Offered when a question asks ("unusual", "higher than usual", "anormal"...) and in
  investigations.
* **Where the data is, learned better**: associations no answer used for 60 days, or to a metric
  or index that is gone, are not used; one that sent the agent to data that was not there (an
  error, no rows, while the answer came from elsewhere) loses a use. Learned answers and memories
  naming a metric or index that no longer exists are not given to the agent.
* **Better matching**: failed / failure / failing, alerting / alerts, throttled / throttling meet
  (stems of at least four letters; `init` stems the stored words again); a match on a rare word
  (elasticsearch) counts more than one on a word hundreds of names have (node).
* **Knowledge search**: a piece found by meaning only must be close enough (cosine 0.35, within
  0.2 of the closest); each result of `search_knowledge` says which search found it.
* **Learning runs never block at the end**: `learn.llm_per_run` is gone. The LLM describes what
  has no description 100 objects at a time (10 per request, each request saved at once), metrics
  and indices first, until the run's time limit; the next run goes on. A label is described once
  per database for every metric that has it (not once per metric). The relations are measured
  reading the objects in steps (never tens of thousands of labels at once) and written 100 at a
  time; a relation marked Wrong holds for the label name of every metric (the first run of 0.4.0
  may measure some relations again: the label that stands for a name is now always the same);
  the rewriting of older learned answers stops at the time limit too.
* **People first**: the background LLM work (the daily learning, learned answers and memory from a
  Helpful) waits while answers are being computed (2 minutes at most per call).
* **Measure it**: `superset supagent stats` (where the time of the answers goes, prompt sizes,
  prompt cache share, slowest answers), `superset supagent evaluate` (does "Where the data is"
  find the data of the Helpful answers: hit@1, hit@3, MRR; also after each learning run),
  `superset supagent gaps` (questions not answered well, learned answers about data that is
  gone), `superset supagent test-llm --profile` (thinking, tool calls, prompt cache).
* `chats.keep_days` (0, the default: keep): chats nobody used for that long go; what they taught
  stays.
* Run `superset supagent init` (one new table; the stored words are stemmed again).

## 0.3.0 (2026-09-28)

Faster, surer answers: the agent is told where the data is, gets short strict instructions and
only the tools the question needs, and learns where the data was from its own answers.

* **Where the data is, before the LLM starts**: the metrics, indices and fields whose names
  (split on `_ : . -`), HELP texts, descriptions and synonyms match the words of the question,
  with built-in synonyms (cpu / processor, mem / memory, es / elasticsearch, disk / fs, French
  words...), are given to the agent with their database id, type, unit, labels and a SQL to
  adapt. It uses the live list of metric names, so it works while the dictionary is empty or
  still learning (10,000 names: well under a second), and only the databases the agent may use
  and the user may query.
* **Learning where the data is from the answers** (`learn.associations`, on by default): the
  words of a question and the metrics or indices its successful queries read; the next
  questions with those words find them first. *Not helpful* takes them back. These are not
  learned answers (that list still only holds what users marked *Helpful*); switch it off with
  `learn.associations = false`.
* **Short, strict instructions**: the rules every answer needs (about 40% shorter), then only
  the sections the question asks for (saving charts, investigations, files / e-mails / reports,
  images); in the chat, the tools that save charts, make files, e-mails, reports or images are
  offered only when the question asks for them. One query when possible; a failing call is
  fixed once, never repeated, and after two failures the agent answers with what it has.
* **The chat draws the charts**: to "see a chart", the agent runs the query (the page shows it
  as a table and a chart); `chart_from_sql` (images) is not offered without Chromium on the
  host, nor `chart_image` without a webdriver.
* **Databases**: every tool takes a database id or name (in any case, or with a letter wrong);
  a SQL on a metric or on `all_metrics` goes to the metrics database; the agent uses only the
  OpenSearch (osagg) and Prometheus / Mimir (promagg) databases unless `agent.databases`
  lists others.
* **Statements are knowledge**: a message that tells something ("STATUS_INFO = KO means the job
  failed", "this metric is the CPU of the cluster nodes") is read for durable facts and rules
  (team ones wait for an admin's approval, `memory.team_approval`).
* **Memories within a budget** (`memory.prompt_chars`, 2,000 characters): the memories given with
  every question are the rules, then the preferences, then the facts; the facts left out are
  still found by the knowledge search when a question is about them. Statements are learned
  now, so the block no longer grows with them.
* **Extracts hold every row**: an Excel extract gets no LIMIT unless the user asks for the first
  N, and when the SQL's own LIMIT is reached the tool says so (the agent runs it again without
  it). Before, an extract could stop at the LIMIT of the tool's example while the answer said
  "all the rows".
* A table name that is no index or metric (in a JOIN too) is named in the error, with the
  closest names, instead of "JOIN not supported" or "Did you mean pg_prepared_statements".
* The knowledge search is updated during a learning run (every 500 metrics, and after each
  database), not only at its end. `describe_data` no longer shows unknown time ranges.
* Run `superset supagent init` (one new table).

## 0.2.5 (2026-09-28)

* **Learning again from scratch**: `superset supagent forget-learned` shows, per database, what
  the learning learned (nothing changes without `--yes`); `--yes` forgets it (dictionary
  objects with their statistics and AI-written descriptions, measured relations, history of
  changes) for every database or those given with `--database`, and the next run learns them
  again as new. The catalog entries, learned answers, query timings, memory, documents, chats
  and settings are kept, and so is what admins did in the Data dictionary page (descriptions
  written or approved there, synonyms, relations marked Wrong); `--everything` forgets that too.

## 0.2.4 (2026-09-28)

* **The pages load their new files after an upgrade**: Superset lets browsers keep static files
  for a year, and supagent's URLs did not change with its files, so a browser could keep the
  former CSS with the new theme script: a half-dark page (dark bubbles with dark text on a light
  page), and former JavaScript fixes missing. Every CSS and JavaScript URL now has its content
  hash. (One Ctrl+F5 is enough for browsers that already hold the former files.)
* **Thousands of metrics are learned in a few runs**: the series counts of 50 metrics come from
  one query, the labels of metrics with a few series from one shared request, and each metric
  has one statistics query of its own: about one request per metric instead of about ten for a
  new one. The start of the data (the depth of the history) is looked up afterwards with the time
  left, about 8 label-index requests per 50 metrics instead of about 7 per metric. Checked on the
  lab Mimir: the same series counts and label values as before for all 28 metrics; 3,000
  metrics in the tests: 60 count queries, 3,000 statistics queries, 60 label requests, 60
  history requests. `learn --plan` gives the new estimate and the history lookups apart.
* **The settings page says why a run is partial**: "N of M due today profiled; stopped at the
  time limit (learn.max_minutes): the next run continues"; history lookups left for later do not
  make a run partial.
* **Wrong relations**: an admin marks a measured relation **Wrong** in *Data dictionary →
  Relations*: it is never measured again nor given to the agent (**Restore** undoes it). A
  catalog entry of classification *relationships* states the right one. Run
  `superset supagent init` (two new columns).

## 0.2.3 (2026-09-28)

* A chat whose first message is not a data question (a greeting) is now named too; before, it
  stopped `superset supagent tidy-learned` (and the daily renaming) from naming the other chats.

## 0.2.2 (2026-09-28)

* **Stop works at once, and no chat is blocked**: Stop marks the answer stopped immediately and
  the chat takes the next question right away (it answered "the previous question is still
  being answered" until the step in progress ended). The run that was in the middle of an LLM
  call or a query never writes over the stopped answer and learns nothing from it. The chat
  page kept checking a stopped answer after another chat was opened, which turned *Send* into
  *Stop* in every chat: fixed.
* **Many users at once**: a running answer held one connection of Superset's database pool for
  its whole duration (idle in a transaction while it waited for the LLM); with many answers at
  once the pool ran out and every page hung. It now holds none while it waits. There is no
  one-at-a-time limit in supagent: see *Operations* in the README for what sets the number of
  answers in parallel (the LLM server, Celery's concurrency, the web server's workers).
* **Learned answers come only from Helpful**: an answer is no longer learned by itself. *Helpful*
  learns it (in the background: the LLM writes its generic question) as *helpful, to review*;
  an admin confirms or rejects it; *Not helpful* or taking *Helpful* back withdraws it unless
  an admin confirmed it. Upgrade (`superset supagent init`): the 0.2.x learned answers users
  had marked Helpful wait for review, the ones an admin had confirmed stay confirmed (told apart
  by their Helpful clicks: an admin-confirmed one that users had also marked Helpful goes to
  review too); the ones saved by themselves are kept but no longer listed nor used, and
  `superset supagent remove-auto-learned` deletes them.
* **Learned answers and chats are named by a short generic question** that the agent writes:
  standalone even when the message only continued or corrected an earlier one, without the
  ids, dates or names of one case ("Number of failed jobs for a given application on a given
  day"), in the user's language. Ids, numbers and dates never stay (checked, sent back once,
  then replaced); names are sent back once. The agent also says whether a question already
  learned asks the same thing: the answer then joins it instead of making a duplicate. The
  first answer of a chat gives it a 2-6 word generic name. `superset supagent tidy-learned`
  rewrites older learned answers and chat names and merges duplicates (the daily learning
  does it too).
* **The agent knows the limits of osagg and promagg** (they are not full SQL engines): what each
  can run is in its instructions and next to each database it lists; refusals come with how to
  rewrite the query (in steps, or pairs of values as `(A = x AND B = y) OR ...`). A query osagg
  cannot push down may read at most `agent.osagg_max_scan_rows` raw documents (20,000; the
  connection's own cap if lower): osagg counts first and refuses at once instead of reading
  hundreds of thousands of documents for minutes. On the lab, a question that took two refused
  queries now takes none.
* **Query timings show the whole query** (values replaced by `?`) with its last error; admins
  also see the last one as it ran.
* **The pages follow Superset's theme**: the mode chosen in Superset (light, dark or system),
  always light when Superset has no dark theme (`THEME_DARK = None`), and Superset's primary
  color. They followed the operating system only.

## 0.2.1 (2026-09-28)

* Metadata databases that are not UTF-8 (PostgreSQL created with LATIN1, MySQL without
  `?charset=utf8mb4`): answers and learning runs failed with "'latin-1' codec can't encode
  character '\u2013'" as soon as a text held a character the database cannot store (LLMs
  write – — ‑ ’ “ ” … all the time). supagent now finds the encoding of the connection once
  and folds what it writes: – → -, ’ → ', … → ..., blocks → #, letters outside the encoding
  lose their accent, the rest becomes ?; accents the encoding has (é, à, ç) are kept. Nothing
  changes on a UTF-8 database. Names the agent saves in Superset (charts, dashboards, reports)
  are folded the same way, and downloads keep any file name.

## 0.2.0 (2026-09-28)

Built for millions of index rows and billions of metric samples, and for learning from the team.

* **Gentle daily learning**: one run per day (`learn.hour`, `learn.days`), a cheap daily pass
  (metadata and mappings), statistics refreshed on a rolling basis (`learn.profile_every_days`),
  about 3 requests per metric, dated and rolled-over indices learned as one family, batched
  field statistics, a rate limit per database, timeouts, and a stop after repeated overload
  errors. `superset supagent learn --plan` estimates a run without reading data.
* **Catalog in separate entries**: title, classification, category and content, each with its
  history (restore, soft delete), checked before saving (YAML errors with line and column),
  protected against concurrent edits; import (merge or replace) and export. A person's entry
  always wins over one the agent wrote. The 0.1 catalog is split once, kept as a backup.
* **The agent writes catalog entries when the evidence is certain** (`learn.agent_catalog`):
  formulas of answers confirmed as helpful, team rules and facts approved by an admin,
  definitions quoted word for word from the documents. Marked *agent* with their evidence;
  taken back when the evidence breaks; never changed again once a person edits them, never
  written again once a person deletes them. `superset supagent agent-catalog`.
* **Learning from the answers**: the final query of each answer kept as a recipe (confirmed by
  *Helpful*, rejected by *Not helpful*) for similar questions of the team; query timings per
  kind of query; big results summarised for the LLM (the user still gets every row); raw reads
  of metrics with more than 50,000 series refused with advice.
* **Answers from the tools only**: an answer with no tool call, or showing results no query
  returned, goes back to the model once (and is marked if it still does); `SUM` / `AVG` of a
  counter's raw value is refused with the right way (`rate`, `increase`, the catalog's
  formulas); query results reach the model with their column sums, and it must quote them
  rather than add numbers up; `check_health` says when its entities match nothing instead of
  "no breach"; `send_email` does not attach the same extract twice.
* A learning run stopped by a restart is marked *interrupted* and the day's run is tried again
  (3 times a day at most); a metric whose data stopped costs 2 lookups instead of a bisection.
* **Memory**: preferences of a user and rules and facts of the team, learned from explicit
  signals ("always", "from now on", "remember", "toujours", "désormais"...) and *Helpful*
  answers; team memories wait for an admin's approval (`memory.team_approval`). *Memory* drawer
  in the chat, team memory on the settings page.
* **Knowledge search**: words (PostgreSQL full-text search) and meaning (an OpenAI-compatible
  embedding model such as BGE-M3; vectors as float16 in Superset's database, or Qdrant), fused;
  only what the user may see is searched. No database extension (no pgvector).
* **Documents and sites**: uploaded text, Markdown and HTML, web pages and sites fetched again
  every N days, with allowed domains and size limits.
* Data dictionary: a search tab and the learned answers with their timings.
* New dependency: none (numpy and requests come with Superset).

## 0.1.0 (2026-09-27)

First version: the agent inside Superset.

* Chat page: the **Chat** tab of Superset's top bar (the dictionary and the settings are in
  Superset's Settings menu). Answers come from Superset's Celery workers, or from a thread
  of the web server when no worker runs, with the permissions of the user who asks. Tools
  run in-process: SQL, the learned dictionary, Excel extracts, screenshots of charts and
  dashboards, e-mails, scheduled reports, PromQL, health checks, alerts, and Superset's MCP
  tools for charts and dashboards, behind a check that the MCP service acts as the user who
  asks.
* Result views: the rows of every query as a table (sortable) and a chart (lines over time,
  horizontal bars for rankings, figures for one row), with copy, CSV, Excel and PNG. Files
  are kept in Superset's database. Copy buttons on questions, answers, SQL and code blocks.
  Stop button. *Helpful* keeps the SQL as an example for similar questions.
* The data dictionary learned every day. For indices and fields (osagg): type, fill rate,
  values, ranges, time range. For metrics and labels (promagg): type, unit, HELP text,
  series, data range, rates, percentiles, label values. It also keeps the measured relations
  with their evidence, metric families, the catalog (curated, verified), LLM descriptions
  (marked unverified) and the changes from run to run.
* Admin settings: LLM authentication `none`, `token` or `middleware` (OAuth2 client
  credentials with consumer key and secret, a client certificate, and a token renewed before
  it expires). Learning schedule and scope, catalog import, learning runs, test button.
* `superset supagent` commands, and the MCP server mode for other agents.
* Chat details: every question and answer shows its date and time (and a running answer the
  time spent); a tool list opened during an answer stays open.
