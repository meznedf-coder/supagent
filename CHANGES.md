# Changes

## 0.9.0 — 3 Oct 2026

Investigations: a question that asks what is wrong and why ("today the night batch of the billing applications is
far behind, the runs take longer than usual: why?") is answered by finding what is really off, where, what that
depends on and what changed, and by checking the cause before naming it: the tables compared with their usual in
one call (`compare_groups`), the logs too (`compare_logs`: what they say that they do not usually), the System map
followed with each interaction explained (what to do when following it), the cause checked against the same rows
and the same time. See *Investigations* in the README.

* **The system around the question** is given with it, built without the LLM from the team's categories, the
  interactions of the System map and the catalog: the parts the question names, what each consists of, what they
  depend on, run on, read from, send data to and call (one step further for what they wait for), where each kind
  of part is in the data (the fields and metric labels of its category), how the tables join, which field holds
  the usual value of a measure, the health checks. A plain question that names a family gets what the family
  consists of (its applications), so that it is counted on the right values.
* **`compare_groups`** (new tool): what is unusual in a table, and where. One call compares **every measure** of
  the table over the question's scope with the same time of the 8 previous days that have data, in total and value
  by value for each field of few values (the ones asked, then the table's others): the rows, each duration against
  the field that holds its usual, each numeric field; for a business date, **the stages of its rows** (the table's
  time fields in their order: how many had reached each by that time of day, how long after the one before, how
  many were waiting and since when), the first stage clearly off said first; for each measure that is off, the
  field whose values hold most of the change on the fewest rows; and **the latest records of the related tables**
  (the catalog's joins: the changes of that application, the alerts of those servers; else the records that
  mention the value). Noise is kept out (small counts, averages over two rows, a status, a field the runs not
  started do not have yet); a new version is read against the history of the one it replaces. A business-date
  label of the scope (or the date itself) is read for each earlier day as that day's own business date, a weekend
  once. One measure can still be asked alone.
* **The days your team compares with** (`agent.compare_against`: yesterday, 1 week ago, 4 weeks ago; change them
  in the settings: N days, weeks or months ago, 1 year ago): `compare_groups` gives each figure that stands out
  on those days, says when a figure is as usual against the last days but far from an older reference day (a
  change older than a week: where, and since which day to look), and compares in full with the days a question
  names (`against`: "compared with three months ago"); `compare_to_usual` gives each series on them. Weeks and
  months are the same weekday; on a Monday, yesterday is the Friday. A figure far from the last days and the
  same as on the same weekday of the earlier weeks is said to be what that weekday is; and what stands out says
  since when it has been off (the days before that were already so, or new on this day).
* **`compare_groups` follows a lead once by itself**: when the rows wait at a stage on one value of a field (the
  runs of one pool waiting for a slot, the orders of one country not shipped), the same call says **who else is
  there** (`outside_the_scope`: the rows outside the question's scope that share that value during the window,
  against the earlier days and by the scope's own fields: more than usual, and whose) and makes **the same
  comparison over every row that has this value** (`the_rows_there`: what holds it, since when, the related
  records). `agent.compare_follow` (on). A stage that is off on its own after the first late one (the rows take
  longer once started) is said separately: a late start does not explain a longer run.
* **`compare_groups` on any table**: nothing in it is about one kind of system (a table, its time fields, its
  numeric fields, its fields of few values, the catalog's joins): orders or tickets are compared like runs.
  One call costs about a hundred small aggregations (about sixty more when it follows a lead), three at a time
  (`agent.compare_threads`), and stops reading fields after `agent.compare_seconds` (45). A scope written with a
  time (the rows since 01:00 today) moves with each earlier day; `a OR b AND c` is read with the alternatives of
  one field together; a condition that holds only while a row is in progress (a status) is left out and said; a
  reference day the data does not reach is said as such, with no figure of that day; when nothing stands out the
  result says to answer that.
* **What the waiting rows wait on is looked for outside the question's own fields**: rows that wait before their
  last stage (for a slot, a token) and stand out both on the application the question asked about and on a pool
  are followed on the pool (who else holds it), not on the application (outside the scope on it are only its other
  rows: a test campaign holding the production pool was never looked at).
* **The calls of an answer are kept for what is not yet known**: the same query once per day is sent back at the
  third (one query grouped by day, or `compare_groups`), and eight tool calls at most are run from one message.
* **A business date is the one of the day asked about**: `compare_groups` on a past day's batch (a window that
  ended days ago, the scope's label D-1) reads the label as of that day, and each earlier day's own from there.
  A time field given with the call that the connector computes from the business date is not taken for the time
  of the rows (the table's own is used, and said).
* **A column the connector computes is described by the connector**, never by the LLM: a business-date label
  that the LLM, shown a column with no value of its own, had described as "the status of a run" made the agent
  filter the label on states. The descriptions the LLM wrote of such columns are replaced at the next learning
  run (the ones an admin verified stay).
* **Every interaction of the System map explained, twice**: a short explanation (a few words, shown when the line is
  clicked: the map draws no text on its lines any more) and a long one, what to do when an investigation follows
  it (what to check on the other part, what a problem there does here). The LLM writes both with the interactions
  it reads in the texts (the sentence becomes their evidence) and, in the classification's new step *interactions
  explained*, for the others (an admin's, the older ones) from the two parts, the sentence and the Context: only
  what is empty, never over an admin. A click on a line (or its name in a part's panel) opens its panel; admins
  correct both there, in the new-interaction form and in To review. The agent gets the short ones with the system
  around a question, the long ones of the question's parts too, both from `system_links`. Schema 15 (`superset
  supagent init`): the links' long explanation, evidence and author; a sentence kept as an older link's note
  becomes its evidence.
* **Interactions the logs show** (`learn.interactions_logs`, on; no LLM): with the classification, the recent lines
  of each log table are read for the interactions they state ("still waiting for its inputs: A, B", "request to
  X", "commit to Y"): the part a line comes from (its application) tied to the parts it names, the kind read in its
  words; a pair seen on 5 lines and 2 days waits in To review with its evidence (lines, days, an example). A kind
  the map already has ties only the categories it ties there. On a simulated platform, from 14 days of its logs:
  the 18 real dependencies between its applications found, none wrong.
* **The classification right after the Context** (`context.classify_after`, on): each Context build (nightly, from
  the page or `superset supagent context --build`; `--no-classify` to skip) is followed by the classification,
  which reads what the Context wrote with the documents, the metrics and the data; the nightly learning leaves the
  categories to it unless a Context was already built that day.
* **`compare_logs`** (new tool): **what do the logs say that they do not usually?** The lines of a log table
  (application logs, batch logs, events: any table with a time field and a text field) grouped into patterns (the
  numbers, times, ids, names and quoted values left out; a list of names is one name), each counted against the
  same window of the 8 earlier days that have data: the **new** ones (2 lines for an error, 5 for a warning), the
  ones **far above usual**, the **rare** ones (there on fewer than half the earlier days, with the days they were
  there), the ones as frequent as usual **whose numbers are far from their usual** (each number by its place,
  named by the words around it: the same slow write, 40 s tonight against 8 s), what is **gone**, and what comes
  **every day** (no finding). Where the lines come from (the fields of few values; the names they hold, written
  back in the pattern when nearly all hold one), when, an example; the errors first, then the most lines. The
  patterns said are counted line by line in the database, with their first and last line and their places
  counted by field, so that a burst at the end of the window leans nothing; a big table stops counting after 40 s
  and says so. Scope and reference days as for `compare_groups`. The picture of a question names the log tables of
  its parts (*Their logs*), and the investigation steps read the logs of the parts that look wrong.
* **`system_links`** (new tool): what the System map says of any part: what it is part of and consists of, its
  interactions both ways, where its kind is in the data.
* **Five steps** for an investigation (the facts in one call; the reading of the stages: released late, waiting
  for a slot or running longer; why; the check of the cause; the answer with what was ruled out and what could
  not be checked) and **twice the calls** of an answer.
* **What an answer blames is checked**: a part of the system given as the cause must be tied to the question's
  parts by the System map (two steps) or by a result of the answer's own queries on them; else it is sent back
  once, then marked under the answer. A bare word of the map (a category of the catalog kept as a subject, with
  no description, part of nothing and linked to nothing: "memory", "cache") is no part: it is neither checked nor
  counted among the question's parts.
* **An answer thought aloud** ("Let me re-examine... Here's the corrected answer:") keeps what follows the last
  announcement of the answer, when a full answer follows it.
* **Investigation paths**: an investigation marked Helpful keeps its path (the kind of problem and of cause, the
  checks in order, what confirms the cause, what was ruled out), written generic by the LLM. It waits in To review
  with the answers marked Helpful; once an admin confirms it, the next investigations of that kind of problem get
  it, with the other causes found before for the same problem (one symptom, several known causes: the closest and
  the most often found with their steps, the others in a line). The last query of an investigation is no longer
  kept as an example or a learned answer.
* **Every answer marked Helpful keeps its path**, small request or big one (a count, an extract, a check, a
  confirmation): the data it used (the tables with the fields its queries name, the metrics with their labels) and
  its steps in their order, read from the answer itself (no LLM, no value of that one case: the dates, names and
  numbers of its queries are left out, the failed steps too). It is kept with the learned answer, shown in *To
  review* and in *Learned by the agent*, and given to the agent with the query for the next similar question. An
  answer that ran no query to keep (a health check, a comparison with usual, a comparison of groups) used to
  teach nothing: it is now learned as its path. An admin corrects a path in their own words before confirming it
  (*Edit*; emptied, it goes back to what the answer did), and what an admin wrote stays when the same question is
  answered another way. Like every learned answer, a path is given only to who may query its database.
* **The interactions your documents state are proposed for the System map** (`learn.interactions`, on;
  `superset supagent interactions`): the LLM reads documents, guides, Context pages and team notes next to the
  known parts they name and proposes how those interact, each with the sentence that says so, word for word. They
  wait in To review with their sentence; the map draws, and investigations follow, the approved ones only (the map
  says how many wait).
* **What is missing for investigations** (`superset supagent check-system [--question "..."]`, and for admins the
  System map page): what the agent knows of the system (the parts per category and where each is in the data, the
  interactions, the joins, the usual values, the health checks, the paths) and, in words, what it does not find;
  for a question, what the agent is given and the names nothing knows. No LLM, nothing changed.
* **The System map's display**: a click on a category's name folds its parts into one box and opens them again
  (the Open / Fold buttons are gone; any category folds); each viewer chooses the categories shown (a switch per
  category in the legend, a cross in each category's name: only the lines between what is shown are drawn; kept in
  the browser, for that viewer only); **full screen**. In *Edit*, an admin drags a category's name to move it
  with its boxes, and puts categories in **groups** drawn as frames (servers, resources and network under one
  name): display only.
* **Nothing the learning finds changes the categories before an admin approves it** (`categories.review_all`,
  on): the values read in the data's category fields and the categories the learning gave to the data's objects
  now wait in *To review* like the LLM's proposals (each says where it comes from; a category's values found in
  the data are approved together in one click); the values of what people wrote (a category on a catalog entry or
  a document) are used at once, as before. No value in use is merged, retired or changed by the learning: a
  proposed value that has the name of an approved value of another category says so (one click merges it), and a
  name the team already has under another category is not proposed again as a component. A subject may still
  share its name with a part. With `categories.review_all` off, the values of the data are approved at once, as
  before.
* **What you see on the map is chosen in a box where you type**: several categories and parts (a part with what
  it is part of, its parts and what it interacts with), in place of the *Everything* list of one part.
* **What each category is**: a description an admin writes in Categories or on the map, shown with the category
  and given to the agent (the system picture, `system_links`) and to the router with the parts a question names
  (`categories.about`).
* **Parts that look retired are proposed** (*To review*), never retired by the learning: a value read in the data
  that its category's fields and labels have not shown for `categories.retire_days` (14) days, seen so by two
  checks; any value, the ones put by hand too, that a text says was retired or decommissioned. *Retire* keeps the
  value, marked retired; *Keep* declines that reason.
* **"Part of" is chosen in a box where you type** (several values, the matches listed by the server): no list of
  thousands is loaded any more.
* **The classification at the size of a platform**: the values of a category read from the data are read once
  each, with where each comes from (which field of which indices, which label of which metrics), instead of once
  per metric that carries the label with a query per value (more than half an hour with thousands of metrics: now
  a second); such a category is no longer a list the LLM chooses in or adds to. The classification is a run of
  its own (*Classify now*, `superset supagent classify`) listed in the settings with its steps and its LLM use.
* **Backups of the knowledge, restored whole or by part**: one zip a day (`backup.enabled`, `backup.hour`, the
  last `backup.keep` kept, in `backup.dir`) with the categories and the System map, the catalog, the memory, the
  documents, the notes, the Context, the learned answers and paths, the descriptions of the data, the settings
  (never a secret; the vectors with `backup.vectors`). *Settings → Backups*: Back up now, Download, Restore… (the
  parts chosen are put back as they were, after the present state is saved in a backup of its own);
  `superset supagent backup | backups | restore`. Backups and restores are runs listed with the others.
* **The catalog**: a field that holds the usual value of another says `usual_of: <that field>` (index entries).
* **The router**: the *incident* route covers what is wrong now as well as a past period; the *technical* route
  offers `system_links` and gets the whole system picture. Any other question that names parts of the system gets
  what it names only (*What the question names*): a follow-up on a server's load needs no map of the platform
  (given the whole picture, the model answered such follow-ups from it without querying, in the end-to-end suite).
* **A memory that says when it applies applies only then**: the memories given with a question say that one
  written as "when the user says X" (or "for the desk Y") is no filter for a question that does not say X (a saved
  "when the user says BILLING, show only BILLING data" scoped "How many jobs failed yesterday?" to BILLING).
* **A query written in an answer and not run** is sent back to be run when the answer says what it returned ("the
  query returned no rows"), not only when it shows figures.
* **`check_health`: new, or every night?** Each breach says on how many of the seven previous days the same
  series breached the same check in the same window, is marked usual when it does on most of them, and the new
  ones come first: an alert that fires every night is not what changed today.
* **`promql_query`: a level with its usual.** A query of a few series gives each one what it was over the same
  window of the 7 previous days (`usual_avg`, `usual_max`, `against_usual`) and says in words whether these
  levels are their usual: a pool that is full every night is not what changed.
* **System map: the panel of a part with nothing under it** (most applications and servers) stopped halfway since
  0.8.0 (a page error after "Part of"): its interactions, *What touches it* and the admin's edits are shown again.
* **A tool call the server could not read** (a call that ran on to the token limit) is followed by a step of a
  quarter of `llm.max_answer_tokens` (2,048 at least): a second one does not cost as many minutes again.
* **Metrics**: `compare_to_usual` refuses a counter read as it is (it says to compare its rate or its increase)
  and says when a value far from its median is within what the earlier weeks differ by; "now" in the metric tools'
  times follows `agent.now` when an admin pinned it.

From 0.8.x: `pip install` on every host, `superset supagent init` once (schema 14 -> 15: three nullable columns of
`supagent_link`), restart (INSTALL.txt, *From 0.8 to 0.9.0*: what starts by itself, and the three statements to
go back to 0.8.2).

## 0.8.2 — 2 Oct 2026

The Data dictionary, from a first day of review on a real platform:
* **To review: a proposed value is edited whole before it is approved.** *Edit…* on its card (in place of Rename)
  has what the Categories page's Edit has: its category (subject, application, component, your own), its name,
  what it covers, its other names and what it is part of, then *Save and approve*, or *Save* to keep it waiting
  with what was changed. A name that exists already in the category it moves to makes one value of the two, with
  the parts chosen, approved when asked.
* **Saving an edit never takes away a "part of" the admin could not see.** The list of what a value can be part of
  fills after a request and held the first 1,000 values in the order of the categories' names: with a big category
  read from the data (servers), the subjects were not in it, and saving an edit of a value (Categories → Edit, To
  review → Part of…) then removed its subjects without a word. The list now has every category, the wider ones
  first (5,000 names at most); the save buttons wait for it; a part the list does not show stays as it is.
* **The AI-written descriptions of the data are no longer listed in To review** (tens of thousands on a platform:
  nobody approves them one by one). *Data → Browse → AI-written, not approved* shows them, to correct the ones that
  matter; the page's first line counts them without calling them to check.
* **Categories of your own are edited and removed** (Knowledge → Categories → *Categories and where their values
  come from*): *Edit* changes the name of one of yours (its values follow) and the field names of any; *Remove*
  takes one of yours away with its values and everything that names them (the items they were given to, the "part
  of", the interactions of the map, their places), after asking and saying what goes. Each category says how many
  values it has. A name of your own is shown as you wrote it (no "s" added: Infra, Monitoring).
* **The System map follows the categories by itself**: read again when it is shown, when the window comes back and
  every 20 seconds while it is looked at (once a minute for who is not an admin; never while an admin edits it; not
  after 15 minutes with nobody at the page, so that an open map does not keep a session alive); it says what a
  reading brought ("Updated:
  new part ..., 1 new “part of”", *Show* brings it in view) and outlines it for a moment. An admin sees every
  category, the ones with no value yet too (a category just added has its column, with "No value yet: add one in
  Categories"), and how many proposed values wait in To review (the map draws the approved ones). While it reads,
  the page says "Updating…".
* **The map answers fast on a big platform**: it read every metric with its statistics (seconds with tens of
  thousands of metrics) and looked all the documents up again for every value after each change; it now reads the
  two columns it needs, and keeps each value's explanation while the documents do not change.
* **Team memory: "Edit it in the catalog" opens the entry** that replaced the memory (it was a text, not a link).

No schema change: from 0.8.0 or 0.8.1, `pip install` on every host and restart.

## 0.8.1 — 2 Oct 2026

Two refinements of 0.8.0's checks, found by replaying them on the recorded answers of the end-to-end suite:
* **The number check accepts the time units said as written** (60 minutes in an hour, 24 hours in a day, 1,440 minutes
  in a day), as it does 3,600 seconds and the bytes of a GiB: 0.8.0 marked "more than one hour (60 minutes)" or "a
  24-hour target" with a check note when no result held 60 or 24. A number close to them (59, 61, 23) is still a
  figure to find in the results.
* **"its", "their", "son", "leur" refer to the answer before only with nothing before them they could stand for**:
  "What was its failure rate?", "And their notional?" do; "Which servers exceeded their memory limit yesterday?"
  is a question of its own (0.8.0 read it as a follow-up: the previous question's conditions could be pushed onto it).

## 0.8.0 — 2 Oct 2026

The pages:
* **Explanations behind an "i"**: the paragraphs that explained each part of the Data dictionary, the chat's drawers,
  the settings (each setting's text too) and the LLM usage page are behind an **i** next to the title: a click shows
  the text in a small card (a second click, Escape or a click elsewhere closes it). The tabs are bigger.
* **Learned by the agent** is a section of **Knowledge** (the old address `#learned` still opens it).
* **The catalog's "note" is "guide"** (documentation, runbooks, how-tos), so that it is not taken for the users'
  quick notes. The upgrade renames the entries, their history and their search pieces (the knowledge store too); a
  YAML or a call with "note" still works. Search → Kind: Guides or Notes.
* **Learned answers corrected before they are confirmed**: Edit (Learned by the agent, and To review) changes the
  generic question and the query; *Check the query* runs it with the admin's permissions (a few rows); a changed
  query is confirmed only if it runs. The former question and query are kept (Before the last change).
* **Where the data of the questions was: words added by hand** (an index or a metric of a database): stemmed like
  the questions' words, they never fade and a miss or a Not helpful never removes them (dashed chips).
* **To review**: the groups' titles stand out from the text; *Part of…*, *Merge into…*, *Rename*, *Change…* and
  *Edit* open a panel that their button closes again (one panel at a time on a card).
* **The Context as documentation**: the contents (functional, technical) on the left, the page on the right, a box
  that finds the pages by their words, the sources under each page; **Word (.docx) and PDF** of a page or of the
  whole Context (pure Python: nothing to install).
* **The System map** (Knowledge → System map, for every user of the dictionary, each seeing the parts that concern
  the databases they may query): the architecture as the categories describe it, one column per category, each part
  with what it does (its description, else the sentence of a document, a guide or a Context page that names it), a
  grey line to what it is part of, and the **interactions** an admin draws (depends on, sends data to, calls,
  reads from, runs on, triggers, monitors) as arrows; a big category folded into one box; focus on a part (what
  touches it). Admins: Edit to move the boxes (kept), draw an interaction, write what a part is, hide a part.
  Exports: PNG, SVG, PDF.
* **Documents and sites behind a sign-in**: a token (Bearer), a user and a password or app token (Basic), or a
  token in a header of its own, kept encrypted (Superset's SECRET_KEY), sent only to the document's own site (never
  after a redirect elsewhere), never shown nor logged. **Confluence** (a page and the pages under it, or a space;
  Data Center and Cloud) and **Bitbucket** (a repository's text files, documentation first; Data Center and Cloud)
  are read through their APIs; other sites as web pages. Each page is cut in its own pieces for the agent's search
  (words and meanings, in the knowledge store when PostgreSQL has pgvector, pg_textsearch, pg_trgm), with its
  address: the agent names the page. The table says how many pieces each document has in the search.
* **The chat panel on Superset's pages floats**: a button switches between docked (the page narrows beside it; the
  left edge, or the arrow keys on it, set its width) and floating (moved by its title bar, resized from any edge or
  corner, over the page: the charts do not move). Place and size are kept in the browser. Docked, it leaves the page
  900 pixels at least (a dashboard's header needs about 850: below, its charts would go under the panel); dragged
  further, it says to float it; in a window too small for both (under 1,220 pixels) it floats.
* **What cannot be undone is confirmed**: every delete (a chat, a memory, a note, a document, a catalog entry, a
  learned answer, a word of a table, a category, an interaction) asks in a small card next to the button what goes
  with it; Cancel, Escape or a click elsewhere keeps everything.

The agent:
* **The subjects of a chat**: a follow-up ("and on the 23rd?", "which one had the most?", "in hours please", a reply
  to the agent's question) is given its subject's messages from the start (the first exchange, the ones in between
  shortened, the last ones in full); a question about other data starts a new subject without the earlier messages;
  a question about an earlier subject's data goes back to it. Decided from the words, the named values, the tables
  each subject read and the question needs; the LLM is asked in a short call only when that cannot tell; not sure:
  the same subject. The chat shows a thin line where a subject starts or comes back. `agent.subjects=false`: the
  last exchanges, as before.
* **A follow-up for another day is answered for that day**: "And on the 23rd?", "Et le 23 ?", "And the day
  before?", "la veille" are read from the day the questions before named. Such a follow-up gets its own query, with
  the previous question's conditions, and the period check reads the earlier question with the new day in its place
  (its "shipped", its "during the night" kept): the 23rd's query was refused as "not 22 September", and the 22nd's
  figure came back. With no day before ("And the 2nd?" after a ranking), it is no day.
* **A month is a month's name**: "the top 5 markets", "3 decisions", "10 junior" named a day (5 March, 3 December,
  10 June) for the period checks of queries and charts and for the answer checks.
* **A follow-up answered without a query restates the previous answer only**: its numbers must be in that answer;
  one found only in an older answer of the subject ("10,000", a limit) no longer passes for a new figure ("What was
  that book's VaR?" was answered with it): the answer is sent back for a query.
* **The number check no longer takes 59 to 61, 24 or 1 for "found"**: the unit constants (3,600 seconds in an
  hour, 86,400 in a day, 1,024 bytes) ground only themselves, as written; converted, they made such numbers of any
  answer pass ("61 trades booked by voice", made up, was not sent back).
* **A query written in the answer instead of run** (with figures no query gave) is sent back with that reason: "call
  execute_sql with it"; still not run, the answer says no query ran.
* **"No such field" is looked up first**: an answer that says the data has no such field or value while no tool was
  called is sent back once to look it up (describe_data: the fields and their values; a value may be in a field with
  another name), follow-up or not. A wrong "there is no such field" in a chat's earlier answer was repeated by the
  next ones.
* **Metrics: "how many" is a number of events**: a query that adds up `rate` (per-second rates) for a "how many"
  question, or counts the samples of a gauge (a counter exported as a gauge, such as the kernel's OOM kills), is sent
  back once with the right form (`SUM(increase)`, `SUM(INCREASE(value))`, `COUNT(DISTINCT label)`); sent again
  unchanged, it runs.
* **Follow-ups keep their context**: "What was *its* failure rate?", "their", "son", "leur" refer to the answer
  before: a short question with such a word and no value of its own stays in the subject, whatever tables its other
  words make the resolver guess (it was taken for a new subject and lost what "it" was); a question the subjects find
  goes on from the last exchange with nothing of its own ("Which error code came up most often?") is read with the
  previous question and keeps its conditions; "that day", "ce jour-là" are the day named before; a short question with such a day ("Compare with the day
  before.") stays in its subject ("the ones delivered
  that day": the period on the delivery time); "in total", "overall", "au total" take the whole, not the previous
  answer's part.
* **"Back to …" has the scope of the question it goes back to**: the last answer's conditions are no longer pushed
  onto it ("Back to the September returns: how many were refunded?" after a question about one return reason was
  counted for that reason only).
* **The words of a subject**: words that start like a time word ("events", "declined", "market", "main",
  "maintenance") are no longer left out when the subject of a question is decided.
* A value the question names is kept when the query writes its table as a pattern (0.7.1).

Upgrade: `pip install` and `superset supagent init` (schema 13 → 14: new nullable columns, the guides renamed);
see INSTALL.txt. A fresh Superset 6.1.0 installed from PyPI today needs three pins (INSTALL.txt).

## 0.7.1 — 2 Oct 2026

* **A value the question names is kept when the query writes its table as a pattern.** A production answer to
  "the application X's KO errors of D-1, by category" queried `"<index>*"` while the dictionary knows the index as
  `"<index>"` (an alias, a family of dated indices): the checks looked the table's fields up by the exact name,
  found none, and the query that left the application out was not sent back. A query's table written with `*` now
  reads the dictionary's indices it matches (and a dated index the dictionary's pattern that covers it), for every
  check that looks fields up: the values the question names, the team's rules, the periods.
* No table change: `pip install --upgrade` and a restart.

## 0.7.0 — 1 Oct 2026

* **Notes**: any user writes one in seconds (what a meeting decided), for the team or for themselves: the chat's
  Notes button (search, pages, edit), `/note ...` or `/mynote ...` in the question box (saved without the LLM),
  Data dictionary → Knowledge → Notes. The agent finds them (the knowledge search; `search_notes` with words and
  days), always with their author and day, as not verified: never a team rule, never the source of a query's
  condition. An admin pins a team note or makes a catalog entry of it. Table supagent_note.
* **The agent as a notes assistant**: asked in the chat, it reads (`read_note`), writes (`add_note`: the team's,
  or personal when the user says so), adds to or changes (`change_note`, `undo` back to the version before) and
  deletes (`delete_note`, only in its answer to the user's yes: a call in the turn the deletion is asked is never
  run) the notes the user may change, as that user. Each note keeps its earlier versions (`versions`, 20). A
  request about notes gets the notes tools only; an answer that says a note was saved, changed or deleted with no
  such call is sent back, then replaced by what is true (the lab's LLM wrote "has been deleted" without calling
  anything). "Note for the team: ...", "Make a personal note for me: ...", "Add a note: ..." are saved at once,
  without the LLM, like `/note`; the call in the answer to the user's yes is the confirmed deletion.
* **The charts and dashboards looked at every night** (`charts.scan`, with the Context build): each chart's last
  full day against the same weekday of the four weeks before, per series, through Superset's chart data API as
  the learning user (high, low; data that stopped is said as such; a few events are not unusual; charts left out
  say why); what each chart shows in words (LLM, cached); a Context page per dashboard. The agent's
  `chart_anomalies` answers "anything unusual on the X dashboard?" for the charts the user may see (with
  row-level security, a look now as them). Gentle on the databases: `charts.max_requests_per_minute`, a database
  answering it is overloaded is left for the next night. `superset supagent charts`. Table supagent_chart_scan.
* **Where the data of the questions was** (Data dictionary → Learned by the agent): one row per table, the words
  that led there (one that also led to other tables and databases says so), the answers and the Helpful among
  them; a search box, a database filter, pages; Wrong (admins) takes a word away from a table.
* **Data dictionary → Search lists everything found**, not the 12 best: every piece the search finds (at most 500,
  the first ones in the order the agent sees them), 20 per page, with the count. Each name is a link to where the
  item is read or edited: a chart or a dashboard opens in Superset (new tab); an index, a metric, a catalog entry,
  a note, a team memory, a document, a Context page or a learned answer opens in the dictionary's side panel, at
  an address that can be copied or bookmarked (`#open/<item>`, opened only for a user who may search it).
* **Categories by hand** (Data dictionary → Knowledge → Categories, admins): a value is added to a category
  (subject, application, component) with its description and other names, used at once; one that exists is said
  so, a retired one comes back. **Edit changes the category itself**, not only the value's text: a value moves
  between subjects, applications and components with its items (where the other category has that value
  already, the two become one, as a merge; a rename onto an existing value merges too, where it failed before).
  The aspect (functional, technical) stays fixed.
* **A value can be part of several others** (the user's example: a jvm of two applications and four components):
  when it is added or with Edit → Part of (Ctrl or Cmd + click for several). The list says what each value is
  part of; the items of a value count as about each value it is part of (a note on the jvm is found for both
  applications); a merge keeps what each value is part of.
* **The learning says it too**: a new value the LLM proposes (from the metrics, the data, the notes, the
  catalog, the memories) comes with the known values it is part of, approved with it, and, when it names a
  known value, a one-click "Merge into" that value; what the texts say about known values ("the posting engine
  is part of LEDGER") waits in To review → Categories part of others (Approve, Reject).
* **Categories of your own, read from the data** (Categories → Categories and where their values come from):
  besides subject, application and component, an admin adds categories (server, environment, team...) and,
  for any of them, the field names its values are read from (`categories.fields`, e.g. `server:
  ^(host|node|server)$`). The learning makes their values approved values with where they come from ("field
  NODE of jobs-*"); the LLM classifies into them too. Two such fields of one index show which values go
  together (the profile's sample): proposed as "part of" (an application above its servers) in To review, never
  applied without an admin (a rejected one is not proposed again). Settings `categories.custom`,
  `categories.fields`, `categories.max_values`, `categories.relation_min_docs`.
* **System map** (Categories → System map): the system as the categories describe it, each value under what it
  is part of (a value of several under each), with the items about it that exist now by kind; a metric removed
  or a note deleted is no longer counted, and a value whose items are all gone says so.
* **To review**: a decided item leaves the list at once (it stayed, green, until the page was read again); a
  value merged into an approved one counts its items at once (they waited as proposed). Besides Approve and
  Reject, **Change…**: a "part of" suggestion approved with only the values chosen (or others; the rest is not
  proposed again), an item's category moved to another value, a new value approved with what it is part of.
* **Nothing the LLM finds is used before an admin approves it** (`categories.review_all`, on by default, a change
  from 0.6): every category it gives an item and every relation it finds between items waits in To review, even
  the ones it is sure of; until approved, the router and the search do not use them. Values and their "part of"
  relations always wait. `categories.review_all=false` gives the 0.6 behaviour (what it is sure of, on approved
  values, used at once).
* Fields named `component` or `module` now feed the component category (`categories.fields`); before, a field
  named `component` fed the application values.

* **Reliability on a second subject.** A retail lab (orders and payments, deliveries, returns, customer care) and its
  benchmark (25 cases, two wordings, split before any run). What its held-out half found, fixed:
  * **The time the question names is the time of its period**: "parcels shipped on 17 and 18 September" counted on
    the delivery time, "tickets opened" on the first response time. The classic check sends back a query whose
    period is on another date field than the one the question names; the governed plan's period takes that field
    (`Period.field`). Two named days ("17 and 18 September") are a span, not the second day.
  * **An index's time field is its dataset's main time column** when the team chose one in Superset (the learner
    took the first date field by name); indices learned before take it at the next learning run.
  * **A rule's condition is read its way and on the data it names**: "Orders of the channel TEST (CHANNEL =
    'TEST') ... never count them (CHANNEL <> 'TEST')" was applied as CHANNEL = 'TEST' (the governed pipeline counted
    the test orders only); now the operator a rule writes decides, else the words of its sentence before or after
    the value. "Exclude cancelled trades (STATUS = 'CANCELLED')" no longer goes to other tables with a CANCELLED
    status. A condition code added from a rule never stands for one the question asks for ("failed at payment"
    planned without the failed condition is sent back).
  * **A follow-up keeps the previous question's conditions** (classic): "... show me only <app>" after an
    answer counted with a business-date label added the application and dropped the label. The previous
    answer's conditions on a table queried again, that this answer's queries lose and the message neither
    changes nor removes, are sent back once, then said under the answer. "Make it in your memory", "when I say
    X ... you should know" are learned like "remember".
  * **A value the question names is in the query** (classic, as the governed plan already checked): "How many web
    orders did we sell" counted every channel (no 'WEB' in its query): such an answer is sent back once, then
    marked; a value said before the table's subject ("test orders") is named in both pipelines; "UAT included"
    is not a filter.
  * **Governed answers**: the top N asked is the whole answer (it said "the results only show the first rows"); a
    count has no unit ("count of rows, in seconds"), nor a sum one the dictionary does not give ("in percent").
  * **A day said as `DATE '2026-09-22'`** (or a midnight `TIMESTAMP`) on a date field stored with a time finds
    nothing: sent back as `= '2026-09-22'` already was ("LYON orders of 22 September": 0 instead of 136).
  * **No figure for the future**: "Forecast tomorrow's orders" got "~411 orders" (the mean of four Fridays); the
    data tell what happened: past figures as past figures.
  * **Status questions read the nightly look first** (`chart_anomalies`, then the live checks).
  * **Investigations, generic**: the question's own data first, then what people said about that time (the
    team's notes, the documents), the systems' health only for systems' data (the instructions named the jobs
    index and the servers: "why were deliveries late" went there); two calls before the end, the agent answers.
* **A second, independent computation** (test, off: `agent.cross_check`): when the classic agent answers with
  figures from its own queries, the governed pipeline computes the same question its own way (its plan, queries
  built by code); when its figures differ from the answer's headline figures, the answer says so with the other
  figures. Costs the second computation's time (`agent.cross_check_seconds`, 180 at most). Measured on the lab's
  112 labelled answers: it caught 4 of 21 wrong answers and questioned 12 of 91 right ones; it stays off.

Upgrade: `superset supagent init` (schema 13: supagent_note, supagent_chart_scan; columns supagent_facet.parents,
supagent_facet.suggested, supagent_facet.origins).

## 0.6.0 (2026-10-01)

The release of the 0.6.0 test versions: the code of 0.6.0b7 with one fix, found by rehearsing the upgrade from
0.5.4 on a copy of the lab's database owned by an ordinary role (not a superuser, as Superset's database user is
as a rule):

* **The knowledge store with a database user that is not a superuser**: it read `shared_preload_libraries`, which
  PostgreSQL shows only to superusers and `pg_read_all_settings`; the store was then never built (`init` said "not
  used (no pgvector, pg_textsearch or pg_trgm...)" with the three there) and the search of 0.5.4 kept answering.
  Now read where PostgreSQL leaves it out instead of failing; when it is not shown, `store status` says to check
  it as a superuser. `init` prints the store's real error, if any.

What 0.6.0 brings, by test version below:

* **The router (MOA)** (on; `agent.router`): the kind of work each question needs, from its meaning, the knowledge
  it touches and the routes people confirmed; nothing in it is about one business.
* **The Data dictionary redesigned**: To review, Knowledge (the catalog, team memory, Context, documents and sites,
  categories: moved from the Settings page), Data, Learned by the agent, Search; saves answer at once.
* **Categories of the knowledge** (`superset supagent classify`, daily) and **the learned items ranked by the
  discussions**.
* **The knowledge store in PostgreSQL** (optional: pgvector, pg_trgm, pg_textsearch; `superset supagent store`),
  with each user's chats and **a search box for them**.
* **A cross-encoder reranker** (optional, off: `rerank.url`).
* **The governed pipeline** (optional, off: `agent.pipeline = governed`) and other MCP servers as sources
  (`supagent[graph]`).
* The fixes the domain benchmark (PnL, risk, pricing, trades, reports) found: team rules only where their value
  can be, dates said as days on timed fields, NaN results saved, a stuck LLM call not waited for twice, qualified
  counts.

Upgrade from 0.5.4: INSTALL.txt, "Upgrade from 0.5.4 to 0.6.0" (6 new tables, schema 9; checks of PostgreSQL
before; a rollback to 0.5.4 needs no restore).

## 0.6.0b7 (test version, 2026-10-01)

* **An LLM that does not answer in time is not asked again**: a read timeout was retried, so one stuck call of the
  governed plan held the answer 2 x llm.timeout (half an hour on the lab). Quick steps have their own bound: the
  router 60 s, the decider's choice 120 s, the plan 180 s; then the normal way (the classic agent) answers.
* **A count the question qualifies needs that condition**: "How many jobs failed" planned without a condition on
  the field that says FAILED counted every job (the lab, once its glossary term was missing: all 73,769 jobs said
  "failed"); such a plan is sent back with the field and its values.

## 0.6.0b6 (test version, 2026-10-01)

* **An answer whose results hold NaN is saved** (a sum over no row): PostgreSQL's JSON refused the whole row and the
  answer was lost ("The agent could not answer"); NaN and infinities are stored as null. SQLite (the tests) took it.
* A condition the planning model copies from a team rule follows the rule's limits, like the ones code adds: not on
  a step grouped by its field (per BOOK), not where its value cannot be.

## 0.6.0b5 (test version, 2026-10-01)

Found on the domain benchmark's dev half with 0.6.0b4:

* A rule's condition does not concern a table where its value cannot be (BOOK = 'ALL' on an index whose books are
  all known and none is ALL); the words every rule has ("not", "unless"...) no longer pick its tables.
* Governed plans: a day said as an equality on a date field (COB_DATE = 2026-09-23) becomes the step's period or
  that day's range; a code is not said by a part of another code ("FX_OPT_G10" does not say the desk FX_SPOT; both
  pipelines); a question back ends with its question, its options first, so that the reply is read as its answer.

## 0.6.0b4 (test version, 2026-10-01)

Found by the domain benchmark (PnL, risk, pricing, trades, reports) on 0.6.0b3:

* **A team rule applies to the data it is about**: a rule on a field (BOOK = 'ALL', "the VaR of a desk") was
  added to every table with that field (PnL, trades), which then found nothing. A rule now concerns the tables
  carrying its distinguishing subject (VaR: the risk index), and a query or step per that field (per BOOK) never
  gets its filter. Both pipelines.
* **A date alone on a date field with times** (`"TRADE_DATE" = '2026-09-23'` on dates stored at 00:00 UTC, or
  with real times) matches nothing: the classic agent's query is sent back once with the day's range.
* A unit the model puts on a measure that nobody asked for (a PnL "in percent") is removed by code.
* A governed step that fails (a learned count ">=200" read as a number crashed the plan's check) hands the question
  to the classic agent instead of ending the answer in an error; such counts are read as "at least".

* The knowledge given with a question names each piece's application and subject; the categories list the most
  used first; a one-letter code is not an application; no near-spelling search in a store built without pg_trgm.

## 0.6.0b3 (test version, 2026-10-01)

**The router (MOA)**: before each answer one short LLM call chooses the kind of work a question needs
(functional, technical, incident, charts, observability, infrastructure), from its meaning, the knowledge it
touches (with its categories) and the routes people confirmed; the route gives the knowledge of its kind first,
its tools and a short instruction; not sure: the normal way. Nothing in it is about one business. It learns only
from confirmed answers whose execution followed the route; the same question's examples never decide alone
(two agreeing, or an admin's route decision). Lab, held-out half: 82% routed right cold (57% with the first
definitions), 88% with examples, 83% with a wrong example (31%).

**The Data dictionary redesigned**: To review (what waits for an admin, each item with its actions), Knowledge
(catalog, team memory, Context, documents and sites, categories: moved from the Settings page), Data, Learned by
the agent, Search; deep links. **Saves answer at once**: the agent's search follows in the background, the page
says when it is up to date (lab: 0.03-0.5 s instead of 0.8-1.6 s).

**Categories of the knowledge** (`superset supagent classify`, daily): functional or technical, subjects,
applications, components, and the relations between items, by the LLM; an admin reviews only the new values, the
unsure tags and the same-as relations.

**What the agent learned is ranked by the discussions**: the items each answer was given, the tables it read, the
learned queries it ran again (worked or not), and what people said of those answers; the learned answers proposed
follow it; the Learned tab is sorted by it with its reasons.

**A search box for the user's own chats**; **a cross-encoder reranker** for the knowledge search (optional,
`rerank.url`, fails open).

Upgrade: `superset supagent init` (tables of schema 9: categories, tags, relations, uses).

## 0.6.0b2 (test version, 2026-09-30)

**The knowledge store in PostgreSQL** (pgvector, pg_trgm, pg_textsearch; `superset supagent store ...`): the search
runs in PostgreSQL, by words (BM25), near spellings (a name or a value typed with a typo) and meaning (an HNSW index:
no vectors in the processes' memory), the ranks fused, with the user's permissions applied inside the queries. It
also keeps each user's chats (the tool `search_my_chats`, their own only) and the routes people confirmed (the
governed decider's `neighbors` votes; near spellings as `spelling`). Derived and rebuildable: a schema of its own,
built by `init` when the extensions are there, `store rebuild|sync|wipe|status|search`, `search.store = off` to go
back at once; its connections are autocommit and closed after use. pg_textsearch before 0.6.1: see the README (a
ROLLBACK after an error can fail in sessions that loaded it; do not preload 0.5.x).

**The governed plan counts what the question names**: a value of the data it names (BILLING, UAT, a server) is a
condition or a group of the plan, a glossary term it uses is counted as the glossary defines it, a unit it asks
for (in GiB, in minutes) is on a measure and converted by code, and a time bucket only when it asks for one (per
hour, daily, when...); no total of rows a query cut; an explanation that does not say the knowledge gives the
knowledge's own text; explore links, SQL Lab and questions about earlier chats go to the classic agent.

## 0.6.0b1 (test version, 2026-09-30)

**A second way to answer: the governed pipeline** (setting `agent.pipeline = governed`; `classic` stays the
default, so installing this version changes nothing until an admin switches it). In the classic pipeline the
model writes SQL and checks read it afterwards. In the governed one the model never writes a query:

* **the decider** chooses the knowledge a question needs before anything else: candidate tables (an index or a
  metric in one database) from the dictionary itself (names, descriptions, synonyms, associations, the values the
  question names, the database, chart or dashboard it names, the learned answers, the team's charts), ranked by a
  small learned gate (readable weights, learned only from answers a person confirmed: Helpful, an admin's
  confirmation, the reply to a question back; bounded around the defaults); one LLM call then chooses among the
  best ones and says what is ambiguous or missing; the chosen tables are given in full (fields, labels, values,
  units, verified or AI-written descriptions) with the team's rules on their fields, the glossary terms, the notes
  and learned answers;
* **the plan**: one LLM call fills a typed plan (tables, measures, groups, conditions, period, top N). Every
  condition carries its source (the question's own words, the chat, a knowledge item, an earlier step); code checks
  it: a condition nobody gave, a period that is not the question's, a limit nobody asked for, a value the data does
  not have, a counter averaged, a unit it cannot convert, are sent back once; the team's rules are added by code
  (not when the question asks for their value);
* **the queries are built by code** from the plan (OpenSearch and Prometheus), run with the user's permissions
  (execute_sql);
* **the answer** is written from a figures sheet made by code; its numbers are checked against the results; code
  adds how it was counted ("failed jobs in jobs (database 1) · STATUS = FAILED (you said "failed") · ENV ≠ UAT
  (the team's rule ...) · 23 Sep 2026"), a value that is not in the data, a period outside the data;
* questions back, answers from the knowledge alone, and "the data cannot answer" are kinds of plans;
* chart, dashboard, e-mail and export requests, status questions ("is everything normal"), questions for another
  MCP source, and a question no valid plan could be written for go to the classic agent (the steps say which way).

The pipeline runs as a LangGraph graph when `supagent[graph]` is installed, else the same steps in order.

**Other MCP servers as sources** (setting `mcp.servers`, needs `supagent[graph]`): their tools are offered to the
agent (named `<server>_<tool>`), indexed as knowledge (the decider routes questions to them), run with the
server's own headers (`{username}` replaced); a tool that changes something runs only when an admin allowed it
(`allow_write`); `roles` limits a server to some users.

Also: a metrics database whose live list of metrics failed is not asked again for a minute (a Mimir outage no
longer costs every question a timeout); `superset supagent ask --pipeline governed|classic`.

Install and roll back: see INSTALL.txt. The new table (supagent_route: what the decider showed, chose and read for
each answer) is ignored by 0.5.x.

## 0.5.4 (2026-09-30)

* The same code as 0.5.3, published under a new version number. Everything below 0.5.3 applies.

## 0.5.3 (2026-09-30)

**No condition of the model's own.** Every condition of a query (WHERE, FILTER, CASE, HAVING, PromQL
matchers, a saved chart's filters) must come from what was said: the question and the chat, the team's
rules and words (glossary), the memory, the documents, notes and learned answers found for the question,
the formulas of the instructions (`mode <> 'idle'`), or the rows an earlier query of the same answer gave
(the 3 servers with the most failures, then their CPU). In the lab the model counted "late" jobs among
the successful ones only (288 instead of 337) and "jobs over 45 minutes" from the 60-minute bucket; both
passed every check. Now such a query is sent back before it runs, once per condition: sent again, or in
another query of the same answer, it runs and the answer says so ("this answer counts only STATUS =
'SUCCESS', a condition the question did not ask for"); a histogram bucket (`le`) sent back comes with the
reminder that it counts up to its bound only (no estimate between buckets). Words say values in other
words ("failed" FAILED, "errors" FAILED, "production" PROD, "5xx" '5%', "45 minutes" 2700), the
dictionary's lists of values are not a source (every status is in them). Not checked: open questions
(what is happening, why, is it normal), queries that only list values, dates and times (the period
check), IS NULL, comparisons with 0. Replayed over node3's latest 800 stored answers (1,077 queries with
conditions, the lab's rule and SLA note in place), it sends back queries in 5 answers: the two invented
conditions above, position labels used for a calendar date, and 2 answers whose values came from a result
the replay only had cut to 4,000 characters (the live agent reads it whole). Values an earlier query of
the answer found count when that query gave figures or was filtered (the applications that failed today),
not a bare list of values (every status).

**Every way an answer ends gets its checks.** Out of calls, the model's last summary had no check of its
numbers, rules or conditions, and an empty answer's fallback (the last result) none of the rules and
conditions: they now get the same notes as any answer.

**An answer going round in circles.** In the lab a long report ended with the model writing its working
notes again and again ("I will now write the response. One final check: ...") until the server's limit:
174,000 characters shown to the user after 17 minutes. Such an answer (the same block of lines a third
time) is now sent back once with the loop cut out of the chat, then cut where it repeats and said; one
LLM answer has at most `llm.max_answer_tokens` tokens (8192; with `llm.thinking`, four times this; 0: the
server's own limit). A short first paragraph where the model says it will now write the answer ("Now I
have all the numbers. Let me write the summary:"), with no figure in it, is left out.

**A LIMIT read as a count.** A query grouped by four columns with LIMIT 100 gave 100 rows, and the answer
said "100 failed job runs" (358 in fact), unmarked: 100 was "from the query". Such a number (the rows a
query's own LIMIT let through, written as a count, not asked for, not a value of the result) is now sent
back once, then marked. And a small result cut by its own LIMIT kept the column totals but lost the note
saying it was cut (the totals were then read as those of every row): the note stays, and says the totals
are those of the rows shown only.

**A plan instead of the work.** Asked to change a chart, add it to a dashboard and e-mail it, the model
answered "I'll do this in three steps: 1. ... 2. ... 3. ..." and called no tool; the check of announced
steps read only the last sentence. An answer that is only a list of steps it will take is now sent back
once, like an announced step.

Also: the total of the rows a sentence names ("BILLING and PAYROLL account for 871") is read as from the
results when it is the sum of those rows in one column of a result (it was marked as made up when the rows
were not the first ones of the result).

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
