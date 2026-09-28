# Changes

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
