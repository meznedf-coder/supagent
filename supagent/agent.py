"""The agent: the LLM chooses the tools, the tools run in-process as the user who asks.

Tools: supagent's own (SQL, the learned data dictionary, files, e-mails, reports, metrics,
see supagent.tools and supagent.tools_superset) and Superset's MCP tools for charts and
dashboards (supagent.superset_mcp). The chart guard checks chart configs before Superset's MCP
service sees them (it answers only "An error occurred" for an invalid one and saves one more
chart at every accepted retry): Superset's own schema, the dataset's columns and saved
metrics, a preview without saving (a failing or empty chart is not saved), a second save of
the same name becomes an update, and date filters on the time column become the chart's time
range (dashboards ignore plain filters on it).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import re
import time
from typing import Any, Callable

from supagent import settings
from supagent.llm import LLM, EmptyAnswer, add_usage

log = logging.getLogger(__name__)
MAX_TOOL_CHARS = 8000
TOOL_CHARS = {"describe_data": 16000}
HISTORY_MESSAGES = 6
RESULT_TOOLS = ("execute_sql", "promql_query")      # their full result is kept for the page's views


class Cancelled(Exception):
    pass


CORE = """You are the data assistant of this Superset. You act with the permissions of the user who asks: the
tools only show and do what this user may see and do in Superset.

Rules:
1. Facts come from the tools: never invent a number, an id or a name, and never add numbers up yourself:
   quote the column_sums of a result, or run a query. Answer in the language of the question, even when
   the data, the dictionary or earlier answers use another language.
2. When "Where the data is" is given below, start from it: it names the metrics, indices and fields that
   match the question, with their database id and a SQL to adapt. Use them as they are. Call
   describe_data (topic = the words of the question) only for what it lacks, and search_knowledge at most
   once.
3. One query per answer when you can: execute_sql with the database id given. Metrics (Prometheus /
   Mimir, promagg): SQL on the metric's own table, or on all_metrics with metric_name = '...'. Jobs, logs
   and other documents (OpenSearch, osagg): SQL on the index. Other databases only when the user names
   them.
4. A tool error says what to fix: fix it and try once more; never repeat a call that failed; after two
   failures on the same step, answer with what you have and say what did not work.
5. Say exactly what the tools did, never claim what a tool result does not show. If the data ends before
   "now", say so and use its last days. Timestamps are local time (never call them UTC); now is given
   with the question."""

RICH = """
6. The chat page shows the rows of every query you run (execute_sql, promql_query) under your answer as a
   table and a chart that the user can switch, copy and download: to show a chart or a time series, run
   the query; never draw charts in text and never make an image unless asked. Give the key figures in one
   or two sentences, at most a 10-row Markdown table, and the SQL you ran. Files and images you make
   appear under your answer by themselves: never write their paths, links or Markdown images."""

PLAIN = """
6. Report requests: run the SQL, then answer with one or two sentences and a Markdown table (at most 30
   rows); for a ranking or a time series also call show_chart and put its output in the answer. Give the
   SQL you ran."""

OSAGG_RULES = """

SQL on OpenSearch (osagg), not a full SQL engine: OpenSearch runs the filters and the GROUP BY /
aggregates, and only their small result is post-processed. Table = index name; field names are case
sensitive and double-quoted ("APPLICATION", "@timestamp_date"). Write only queries it can push down:
  * one index, WHERE filters (=, IN, <, >, BETWEEN, LIKE, IS NULL) on single fields, joined with AND / OR,
    with a time range; pairs of values: ("A" = 'x' AND "B" = 'y') OR (...), never ("A", "B") IN (...) nor
    an expression over several fields;
  * aggregates (COUNT, SUM, AVG, MIN, MAX, COUNT(DISTINCT), percentiles) with GROUP BY on fields or on
    DATE_TRUNC of the timestamp; the latest of each key = GROUP BY the key with MAX of the timestamp (not
    ROW_NUMBER() and not a self-join);
  * a row list: filtered, with ORDER BY and a LIMIT (at most 1000);
  * a JOIN only in an aggregating query, on equal fields, each index filtered to at most 100,000
    documents; ORDER BY, HAVING, window functions, WITH and UNION only on top of aggregated rows.
Never put a row list of an index in a subquery, a WITH or a join: work in steps (one query for the few
keys you need, then WHERE key IN (those values)). An error saying the query "could not be fully pushed
down" means: rewrite it that way, do not run it again. Business dates: "POSITION_DATE" (yyyymmdd),
"POSITION_LABEL" (D, D-1, W-1, Y-1...) and "POSITION_TIME" (execution time moved onto the D-1 position
date) are columns when the index has them; filter labels with "POSITION_LABEL" IN ('D-1', 'W-1')."""

PROMAGG_RULES = """

SQL on metrics (promagg), turned into PromQL, not a full SQL engine: one table per metric; columns ts, one
column per label, value, and for counters rate / increase. One metric table per query, no JOIN of metric
tables row by row, no WITH, window function or subquery over raw samples, no quantile of raw samples
(QUANTILE_OVER_TIME or a histogram), no GROUP BY or WHERE on the sample value (HAVING). Always filter ts on
a time range and GROUP BY a time bucket (DATE_TRUNC('hour', ts)). Counters: SUM(rate) = per second,
SUM(increase) = count, never SUM or AVG of value; gauges: AVG(value), MIN(value), MAX(value); histograms
(*_bucket tables): HISTOGRAM_QUANTILE(0.95, SUM(RATE(value))); a condition on one label:
SUM(rate) FILTER (WHERE mode <> 'idle'). Which servers had samples in a window: GROUP BY node with COUNT(*).
A database over several tenants has a column __tenant_id__. Two metrics together: aggregate each one in
its own query, or promql_query (arithmetic between metrics, offsets, label_replace)."""

SECTIONS = {
    "charts": """

Saving Superset charts and dashboards (asked here): check the fields with get_chart_type_schema, then call
generate_chart once with save_chart=true and the requested chart_name; a tool error tells you exactly what
to fix. Use only the dataset's column names. COUNT(*) is the dataset's saved metric "count":
{"name": "count", "saved_metric": true}. Ratios, percentiles and conditional counts cannot be written in a
chart: use the dataset's saved metrics (get_dataset_info lists them), or say it is not possible. To change a
saved chart call update_chart with its id instead of creating another one. Chart filters are fixed values
(no rolling time range): turn "last 7 days" into dates from "now"; a POSITION_LABEL filter and a date filter
must not contradict each other. Charts cannot compare with an earlier period: to compare position dates use
X = POSITION_TIME grouped by POSITION_LABEL. The chart under your answer in the chat is not saved in
Superset: save with generate_chart / generate_dashboard.""",
    "investigation": """

Investigations ("why did the jobs fail", "was a server saturated"): 1) find where and when on the jobs index
(failed jobs by "NODE" or "APPLICATION" and hour), 2) call check_health for that time window with
entities = the servers / applications found (CPU, memory, disk, OOM kills, outages, queues, HTTP errors,
latency, licences with from-to and worst value), 3) answer with the problems that match the failures and
say what was not found. list_alerts shows the alerts firing now; compare_to_usual says whether a metric
was unusual for that time (the same window on earlier weeks).""",
    "usual": """

Unusual or not (asked here): compare_to_usual(promql, start, end) compares the window with the same window
of the previous weeks and gives a verdict per series; use it rather than judging a number alone.""",
    "files": """

Files, e-mails and reports (asked here): a file or Excel extract -> export_excel (every matching row: no
LIMIT unless the user asks for the first N; only there a row list may join the big index with small ones,
e.g. jobs with their application's TEAM); an e-mail now -> send_email
(body_markdown, sql for a table, chart_sqls or image_paths for images, excel_sql or attach_paths for the
Excel file); a recurring e-mail -> create_report. JSON asked -> answer with the rows as a ```json block
only.""",
    "images": """

Images (asked here): an image of data -> chart_from_sql (a SELECT: label column then value columns; bar =
ranking, line = time series); an image of a saved Superset chart or dashboard -> chart_image(chart_id or
dashboard_id).""",
}

INTENTS = {
    "charts": re.compile(r"\bdashboards?\b|tableau de bord|\bsuperset\b[^.?!\n]{0,40}\b(chart|graph|graphique)|"
                         r"\b(saved?|create|creer|cr[ée]e|build|update|modif\w*|enregistr\w*|edit)\b[^.?!\n]{0,50}"
                         r"\b(chart|graph|graphique)|\bchart (id|#)\s*\d+|\bexisting (chart|dashboard)", re.I),
    "investigation": re.compile(r"\b(why|cause|reason|root cause|saturat\w*|incident|slow\w*|degrad\w*|outage|"
                                r"pourquoi|raison|lent\w*|panne)\b", re.I),
    "files": re.compile(r"\b(excel|xlsx|csv|extract\w*|export\w*|file|fichier|download|t[ée]l[ée]charg\w*|mail\w*|"
                        r"e-mail|envoi\w*|send|report\w*|rapport|schedul\w*|every (day|week|morning|monday)|"
                        r"chaque|tous les|quotidien|hebdo\w*|json)\b", re.I),
    "images": re.compile(r"\b(image|images|png|picture|photo|screenshot|capture|mail\w*|e-mail)\b", re.I),
    "usual": re.compile(r"\b(usual|unusual|abnormal\w*|anomal\w*|normal|baseline|habitu\w*|inhabituel\w*|"
                        r"anormal\w*)\b|\bcompared? (to|with) (last|previous|the same)|\bthan (usual|normal|last week)",
                        re.I),
}
TOOLS_OF = {
    "charts": {"get_chart_type_schema", "generate_chart", "update_chart", "update_chart_preview", "list_charts",
               "get_chart_info", "generate_dashboard", "add_chart_to_existing_dashboard", "list_dashboards",
               "get_dashboard_info", "fix_chart_time_range", "chart_image"},
    "files": {"export_excel", "send_email", "create_report", "list_reports"},
    "images": {"chart_from_sql", "chart_image"},
    "usual": {"compare_to_usual"},
    "investigation": {"compare_to_usual"},
}
INTENT_TOOLS = set().union(*TOOLS_OF.values())


def intents(question: str) -> set[str]:
    return {k for k, rx in INTENTS.items() if rx.search(question or "")}

CHART_TOOLS = ("generate_chart", "update_chart", "update_chart_preview")
REF_FIELDS = {"name", "column_name", "label", "dtype", "aggregate", "saved_metric"}
RUNNABLE_AGGREGATES = {"SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT"}


LANGS = {
    "English": set("the and of to in is are what which how many much per by give show me for with from today "
                   "yesterday last this that were was did does do there any all top most least between".split()),
    "French": set("le la les des du de et est sont quel quelle quels quelles combien par pour avec aujourd hui "
                  "hier dernier derniers dernière cette ces qui que quoi donne donnez moi montre montrez entre "
                  "au aux sur dans une un plus moins".split()),
}
ANSWER_IN = {"English": "Answer in English.", "French": "Réponds en français."}


def question_language(text: str) -> str | None:
    """English or French from the common words of the question (None when unclear)."""
    ws = re.findall(r"[a-zàâçéèêëîïôûùüÿœ]+", (text or "").lower())
    scores = {lang: sum(1 for w in ws if w in vocab) for lang, vocab in LANGS.items()}
    best = max(scores, key=scores.get)
    others = [v for k, v in scores.items() if k != best]
    return best if scores[best] >= 2 and scores[best] > max(others, default=0) else None


def now() -> dt.datetime:
    fixed = (settings.get("agent.now") or "").strip()
    return dt.datetime.fromisoformat(fixed) if fixed else dt.datetime.now()


def _short_error(ex: Exception) -> str:
    lines = [ln.strip() for ln in str(ex).splitlines()[1:]]
    return "\n".join(ln for ln in lines if ln and not ln.startswith("For further information"))[:1500]


def _sort_entries(values: list) -> list:
    """Table sort_by "col DESC" / "-col" / "col" -> Superset's ["col", ascending] entries."""
    out = []
    for v in values:
        if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], str):
            out.append(json.dumps([v[0], bool(v[1])]))
            continue
        if not isinstance(v, str) or v.lstrip().startswith("["):
            out.append(v)
            continue
        text = v.strip()
        desc = text.startswith("-") or text.upper().endswith(" DESC")
        col = text.lstrip("-").strip()
        for suffix in (" DESC", " ASC", " desc", " asc"):
            if col.endswith(suffix):
                col = col[: -len(suffix)].strip()
        out.append(json.dumps([col, not desc]))
    return out


def _refs(node: Any, path: str = "") -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if isinstance(node.get("name"), str) or isinstance(node.get("column"), str):
            out.append((path or "config", node))
        for k, v in node.items():
            if k not in ("x_axis", "y_axis", "legend"):
                out.extend(_refs(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_refs(v, f"{path}[{i}]"))
    return out


class ChartGuard:
    def __init__(self, agent: "Agent") -> None:
        self.agent = agent
        self.datasets: dict[str, tuple[set[str], set[str]]] = {}
        self.saved: dict[str, int] = {}
        self.dataset_of: dict[int, Any] = {}
        try:
            from superset.mcp_service.chart.schemas import parse_chart_config

            self.parse: Callable[[dict], Any] | None = parse_chart_config
        except Exception:  # pylint: disable=broad-except
            self.parse = None

    def _dataset(self, ident: Any) -> tuple[set[str], set[str]] | None:
        from supagent.tools_superset import DatasetInfoRequest, get_dataset_info

        key = str(ident)
        if key not in self.datasets:
            try:
                info = get_dataset_info(DatasetInfoRequest(identifier=int(ident)))
            except Exception:  # pylint: disable=broad-except
                return None
            if info.get("error"):
                return None
            self.datasets[key] = ({c["column_name"] for c in info.get("columns") or []},
                                  {m["metric_name"] for m in info.get("metrics") or []})
        return self.datasets[key]

    def _check(self, config: dict, cols: set[str] | None, metrics: set[str] | None) -> list[str]:
        errors = []
        for where, ref in _refs(config):
            if isinstance(ref.get("column"), str) and "name" not in ref:
                if cols is not None and ref["column"] not in cols:
                    errors.append(f"{where}: unknown column '{ref['column']}'")
                continue
            name = ref["name"]
            extra = sorted(set(ref) - REF_FIELDS)
            if extra:
                errors.append(f"{where}: {', '.join(extra)} is not a chart field (a column takes only "
                              "name, label, aggregate or saved_metric; SQL expressions are not possible)")
            agg = str(ref.get("aggregate") or "").upper()
            if agg and agg not in RUNNABLE_AGGREGATES:
                errors.append(f"{where}: Superset cannot run {agg} in these charts (only "
                              f"{', '.join(sorted(RUNNABLE_AGGREGATES))}); use a saved metric of the "
                              f"dataset if one fits ({', '.join(sorted(metrics or [])) or 'none'}), "
                              "otherwise say it is not possible")
            if cols is None:
                continue
            if ref.get("saved_metric"):
                if name not in (metrics or set()):
                    errors.append(f"{where}: '{name}' is not a saved metric (saved metrics: "
                                  f"{', '.join(sorted(metrics or []))})")
            elif name in cols:
                continue
            elif name in (metrics or set()):
                errors.append(f'{where}: "{name}" is a saved metric: use {{"name": "{name}", "saved_metric": true}}')
            elif name.lower() in ("count", "*", "count(*)", "rows", "jobs"):
                errors.append(f"{where}: there is no column '{name}'; COUNT(*) is "
                              + ('{"name": "count", "saved_metric": true}' if "count" in (metrics or set())
                                 else "COUNT of a column that is never empty"))
            else:
                errors.append(f"{where}: unknown column '{name}' (columns: {', '.join(sorted(cols))[:800]})")
        return errors

    def _dry_run(self, dataset_id: Any, config: dict) -> str | None:
        text = self.agent.superset.call("generate_chart", {"request": {
            "dataset_id": dataset_id, "config": config, "save_chart": False, "preview_formats": ["table"]}})
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if data.get("success") is False or data.get("error"):
            return json.dumps({"success": False, "error": "the chart query fails, nothing was saved",
                               "details": data.get("error")}, ensure_ascii=False, default=str)[:3000]
        if ((data.get("previews") or {}).get("table") or {}).get("row_count") == 0:
            return json.dumps({"success": False, "error": "the chart returns NO ROWS with these filters, "
                               "nothing was saved: use dates that exist in the data, and labels "
                               "that agree with the dates, then call again"})
        return None

    def before(self, name: str, args: dict) -> tuple[str, dict, str | None]:
        req = args.get("request", args)
        if name not in CHART_TOOLS or not isinstance(req, dict):
            return name, args, None
        config = req.get("config")
        if isinstance(config, dict) and config.get("chart_type") == "table":
            for key in ("sort_by", "order_by_cols", "order_by"):
                if isinstance(config.get(key), list):
                    config[key] = _sort_entries(config[key])
        if isinstance(config, dict):
            errors = []
            if self.parse is not None:
                try:
                    self.parse(copy.deepcopy(config))
                except Exception as ex:  # pylint: disable=broad-except
                    errors.append(_short_error(ex))
            ds = req.get("dataset_id") if name == "generate_chart" else self.dataset_of.get(req.get("identifier"))
            known = self._dataset(ds) if ds is not None and not errors else None
            x = config.get("x") if isinstance(config.get("x"), dict) else {}
            if known and x.get("name") == "ts" and {"ts", "value"} <= known[0] and not config.get("time_grain"):
                config["time_grain"] = "PT1H"      # metrics (promagg): time buckets, never raw samples
            if not errors:
                errors += self._check(config, *(known or (None, None)))
            if errors:
                return name, args, json.dumps({"success": False, "error": "invalid chart config: fix these points "
                                               "and call the tool again", "details": errors}, ensure_ascii=False)
            saving = (name == "generate_chart" and req.get("save_chart")) or (
                name == "update_chart" and req.get("generate_preview") is False)
            if saving and ds is not None:
                problem = self._dry_run(ds, config)
                if problem:
                    return name, args, problem
        chart_name = req.get("chart_name")
        if name == "generate_chart" and req.get("save_chart") and chart_name in self.saved:
            return "update_chart", {"request": {"identifier": self.saved[chart_name], "config": config,
                                                "chart_name": chart_name, "generate_preview": False}}, None
        return name, args, None

    def after(self, name: str, args: dict, content: str) -> str:
        from supagent.tools import fix_chart_time_range

        if name not in CHART_TOOLS:
            return content
        try:
            data = json.loads(content)
        except ValueError:
            return content
        chart = data.get("chart") or {}
        if chart.get("id") and chart.get("slice_name"):
            self.saved[chart["slice_name"]] = chart["id"]
            ds = args.get("request", args).get("dataset_id")
            if ds is not None:
                self.dataset_of[chart["id"]] = ds
            try:
                fixed = fix_chart_time_range(int(chart["id"]))
            except Exception:  # pylint: disable=broad-except
                fixed = {}
            if fixed.get("time_range"):
                content += f"\n(the date filters were saved as the chart's time range: {fixed['time_range']})"
        return content


def show_chart(title: str, labels: list[str], values: list[float], unit: str = "") -> str:
    """Bar chart as text, for the chat."""
    pairs = [(str(lb), float(v)) for lb, v in zip(labels, values) if v is not None][:40]
    if not pairs:
        return "(no data)"
    top = max(abs(v) for _, v in pairs) or 1.0
    width = max(len(lb) for lb, _ in pairs)
    lines = [title]
    for lb, v in pairs:
        bar = "█" * max(1, round(abs(v) / top * 40)) if v else ""
        lines.append(f"{lb.ljust(width)} {bar} {v:,.6g}{(' ' + unit) if unit else ''}")
    return "\n".join(lines)


SHOW_CHART_SPEC = {"type": "function", "function": {
    "name": "show_chart",
    "description": "Draw a bar chart as text for the chat answer (labels and values of a ranking or a time "
                   "series). Put the returned text in the answer inside a ``` block.",
    "parameters": {"type": "object", "required": ["title", "labels", "values"], "properties": {
        "title": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}},
        "values": {"type": "array", "items": {"type": "number"}}, "unit": {"type": "string"}}}}}


def _cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 1e15 else str(v)
    if isinstance(v, int):
        return f"{v:,}"
    return "" if v is None else str(v).replace("|", "/")


def _sql_rows(t: dict) -> list | None:
    try:
        rows = json.loads(t["result"]).get("rows") or []
    except (ValueError, TypeError, AttributeError):
        return None
    return rows if rows and isinstance(rows[0], dict) else None


def table_if_missing(answer: str, trace: list[dict]) -> str:
    if re.search(r"^\s*\|.+\|\s*$", answer, re.M):
        return ""
    if not any(t.get("called", t["tool"]) == "show_chart" for t in trace):
        return ""
    for t in reversed(trace):
        if t.get("called", t["tool"]) != "execute_sql":
            continue
        rows = _sql_rows(t)
        if not rows or len(rows) > 30:
            continue
        cols = list(rows[0])
        out = ["", "| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
        out += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
        return "\n".join(out)
    return ""


def chart_if_missing(answer: str, trace: list[dict]) -> str:
    if "█" in answer or not re.search(r"^\s*\|.+\|\s*$", answer, re.M) or "```json" in answer.lower():
        return ""
    for t in reversed(trace):
        if t.get("called", t["tool"]) != "execute_sql":
            continue
        rows = _sql_rows(t)
        if not rows or len(rows) > 30 or len(rows[0]) != 2:
            return ""
        label_col, value_col = list(rows[0])
        if not all(isinstance(r.get(value_col), (int, float)) and r.get(value_col) is not None for r in rows):
            return ""
        chart = show_chart(value_col, [str(r.get(label_col)) for r in rows], [r[value_col] for r in rows])
        return "\n\n```\n" + chart + "\n```"
    return ""


def local_links(answer: str) -> str:
    """Markdown images and links to files of the server (the chat page shows the files)."""
    answer = re.sub(r"!\[[^\]]*\]\((?!https?://)[^)]*\)\s*", "", answer)
    return re.sub(r"\[([^\]]+)\]\((?:/|file:|~)[^)]*\)", r"\1", answer)


def trim_tables(answer: str, keep: int = 10) -> str:
    """Markdown tables longer than `keep` rows cut to their first rows (the chat page shows
    every row of the query results under the answer)."""
    lines, out, i = answer.split("\n"), [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) \
                and "-" in lines[i + 1]:
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            rows = j - i - 2
            out += lines[i:i + 2 + min(rows, keep)]
            if rows > keep + 2:
                out.append("")
                out.append(f"*First {keep} of {rows} rows: every row is in the result below.*")
            else:
                out += lines[i + 2 + keep:j]
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


SAVED_CHART_ASK = re.compile(
    r"\bsuperset\b[^.?!\n]{0,40}\b(chart|graph|dashboard)|\b(create|save|make|build|add)\b[^.?!\n]{0,60}"
    r"\b(chart|dashboard)\b[^.?!\n]{0,40}\b(named|called|titled|in superset|(on|to|in) (a|the|my) dashboard)|"
    r"\bgraphique superset|\b(cr[ée]e[rz]?|enregistre[rz]?)\b[^.?!\n]{0,60}\b(graphique|tableau de bord)", re.I)
CHART_CLAIM = re.compile(r"\b(chart|graph)\b[^.\n]{0,40}\b(has been|was|is)\s+(created|saved)|"
                         r"\bgraphique\b[^.\n]{0,40}\b(a [ée]t[ée]|est)\s+(cr[ée][ée]|enregistr[ée])", re.I)


def saved_chart(trace: list[dict]) -> bool:
    return any((t.get("called") or t["tool"]) in ("generate_chart", "update_chart", "generate_dashboard")
               and t.get("status") == "done" for t in trace)


def claims_check(answer: str, trace: list[dict]) -> str:
    notes = []
    sent = []
    for t in trace:
        if t.get("called", t["tool"]) != "send_email":
            continue
        try:
            res = json.loads(t["result"])
        except (ValueError, TypeError):
            continue
        if res.get("sent_to"):
            sent.append(res)
    for res in sent[-1:]:
        low = answer.lower()
        if not res.get("images") and re.search(r"\b(image|chart|graph)", low):
            notes.append("the e-mail was sent without an image (send_email: images 0)")
        if not res.get("attached") and re.search(r"attach|excel|xlsx", low):
            notes.append("the e-mail was sent without an attachment")
    if CHART_CLAIM.search(answer) and not saved_chart(trace):
        notes.append("no Superset chart was saved (only an image or the result shown here); ask again to save one")
    return ("\n\n(Check: " + "; ".join(dict.fromkeys(notes)) + ".)") if notes else ""


QUERY_TOOLS = ("execute_sql", "promql_query", "export_excel", "check_health", "chart_from_sql", "get_chart_data")
LOOKUP_TOOLS = QUERY_TOOLS + ("describe_data", "data_changes", "search_knowledge", "get_dataset_info",
                              "list_datasets", "list_charts", "get_chart_info", "list_dashboards", "get_dashboard_info")
RESULT_CLAIM = re.compile(r"```json|\b(sql|query|requ[êe]te|promql)\s+(run|ran|executed|ex[ée]cut[ée]e?)\b|"
                          r"\bI (ran|executed|queried)\b", re.I)
TABLE_WITH_NUMBERS = re.compile(r"^\s*\|[^\n]*\d[^\n]*\|\s*$", re.M)
NO_TOOL_NUDGE = ("(Check before answering: you called no tool. The knowledge given with the question is only a "
                 "summary, not an answer. Call describe_data for what the data contains and its fields, and run "
                 "the query (execute_sql, promql_query...) for any number or list. If the question really needs "
                 "no data, give the same answer again. Either way write the whole answer for the user: they did "
                 "not see the one above.)")
NO_QUERY_NUDGE = ("(Check before answering: your answer shows results or says that a query ran, but no query ran "
                  "in this answer. Run it now (execute_sql, promql_query...) and answer from its result: never "
                  "show numbers or rows that no tool returned. Then write the whole answer for the user: they did "
                  "not see the one above.)")


def unsupported_answer(answer: str, trace: list[dict]) -> str | None:
    """A reminder when an answer was written without the tools: no tool called at all, or
    results shown (a JSON block, "SQL run", a table of numbers) with no query run."""
    done = {t.get("called") or t["tool"] for t in trace if t.get("status") == "done"}
    if not trace:
        return NO_TOOL_NUDGE
    if RESULT_CLAIM.search(answer or "") and not done & set(QUERY_TOOLS):
        return NO_QUERY_NUDGE
    if len(TABLE_WITH_NUMBERS.findall(answer or "")) >= 2 and not done & set(LOOKUP_TOOLS):
        return NO_QUERY_NUDGE
    return None


EMPTY_RICH = "(The model wrote no text for this answer, even when asked again: the result of its last query is below.)"
EMPTY_PLAIN = "(The model wrote no text for this answer, even when asked again: here is the result of its last query.)"
UNSUPPORTED_NOTE = ("\n\n(Check: no query ran in this answer: numbers or rows shown here do not come from the "
                    "data. Ask again to have them read from the data.)")


def unsupported_note(answer: str, trace: list[dict]) -> str:
    """After the reminder: an answer that still shows results no query returned is marked."""
    nudge = unsupported_answer(answer, trace)
    if nudge == NO_QUERY_NUDGE or (nudge == NO_TOOL_NUDGE and (RESULT_CLAIM.search(answer or "") or len(
            TABLE_WITH_NUMBERS.findall(answer or "")) >= 2)):
        return UNSUPPORTED_NOTE
    return ""


SAVING_TOOLS = {"generate_chart", "update_chart", "generate_dashboard", "add_chart_to_existing_dashboard",
                "save_sql_query", "create_virtual_dataset", "create_report", "update_chart_preview"}
REPEAT_NOTE = ("You already made exactly this call in this answer and its result is above: the same call gives the "
               "same result. Use it: answer the user, or make a different call.")
# an answer that ends by announcing a step instead of taking it ("Let me run the query.")
ANNOUNCE = re.compile(
    r"(?:\b(?:I(?:'ll| will| am going to| shall)|let me|let's|now,? I(?:'ll| will)|next,? I(?:'ll| will))\b"
    r"[^.!?\n]{0,60}\b(?:run|query|check|call|create|fetch|look up|search|execute|generate|export|send|build|"
    r"compute|calculate|retrieve|pull|save|draw|plot)\b"
    r"|\b(?:je vais|laissez-moi|je lance|lan[çc]ons)\b[^.!?\n]{0,60}\b(?:lancer|ex[ée]cuter|v[ée]rifier|chercher|"
    r"cr[ée]er|interroger|calculer|r[ée]cup[ée]rer|envoyer|exporter|enregistrer|tracer)\b)[^.!?\n]*[.:!…]?\s*$", re.I)
ANNOUNCE_NUDGE = ("(Check before answering: your text ends by announcing a step (\"{step}\") but you called no "
                  "tool. Take that step now with the tool, or, if it is not needed, write the final answer without "
                  "announcing anything. The user did not see the text above.)")


def announces_action(answer: str) -> str | None:
    """The announced step at the end of an answer that called no tool for it, or None. Offers
    ("if you want, I can...", "let me know...") and questions are not announcements."""
    tail = (answer or "").strip()[-300:]
    last = re.split(r"(?<=[.!?])\s+|\n+", tail)[-1] if tail else ""
    if not last or last.rstrip().endswith("?") or re.search(r"\b(if you|let me know|would you|do you want|shall I|"
                                                            r"si vous|souhaitez|voulez|dites-moi)\b", last, re.I):
        return None
    m = ANNOUNCE.search(last)
    return last.strip()[:160] if m else None


# the answer says all is well while a check or a query of this answer could not run
ALL_CLEAR = re.compile(
    r"\bno (?:breach|problem|issue|error|anomal\w*|incident|saturation|alert|failure)s?\b"
    r"|\b(?:everything|all) (?:is |looks |was |seems )?(?:fine|ok|okay|normal|healthy|good)\b"
    r"|\b(?:is|are|was|were|looks?|seems?|remained|stayed) (?:healthy|normal|fine|stable)\b"
    r"|\baucun(?:e)? (?:probl[èe]me|anomalie|incident|erreur|d[ée]passement|saturation|alerte|[ée]chec)\b"
    r"|\btout (?:est|va|semble|était) (?:bien|normal|ok|correct)\b", re.I)
NEGATION = re.compile(r"\b(?:cannot|can't|could not|couldn't|unable|not possible|impossible|does not mean|doesn't "
                      r"mean|not necessarily|whether|if|without|unknown|unclear|n'a pas pu|ne peut|impossible de|"
                      r"sans|si)\b", re.I)
CHECK_TOOLS = ("execute_sql", "promql_query", "check_health", "list_alerts", "compare_to_usual")


def _unchecked(trace: list[dict]) -> list[str]:
    """What could not be checked in this answer: checks of check_health that failed, and data
    tools whose last call failed (a failure fixed by a later successful call does not count)."""
    out: list[str] = []
    last: dict[str, str] = {}
    for t in trace:
        tool = t.get("called") or t["tool"]
        if tool not in CHECK_TOOLS:
            continue
        last[tool] = t.get("status") or ""
        if tool == "check_health" and t.get("status") == "done":
            try:
                errors = (json.loads(t.get("result") or "{}") or {}).get("errors") or {}
            except (ValueError, TypeError, AttributeError):
                errors = {}
            out += [f"check {name}" for name in errors if f"check {name}" not in out]
    out += [tool for tool, status in last.items() if status == "error"]
    return out


def claims_all_clear(answer: str) -> bool:
    for m in ALL_CLEAR.finditer(answer or ""):
        start = max((answer or "").rfind(".", 0, m.start()), (answer or "").rfind("\n", 0, m.start()), m.start() - 80)
        if not NEGATION.search(answer[max(0, start):m.start()]):
            return True
    return False


def honesty_note(answer: str, trace: list[dict]) -> str:
    """A short correction when the answer says all is well while something it relies on could
    not run (the model sometimes reads "could not reach" as "found nothing")."""
    missing = _unchecked(trace)
    if not missing or not claims_all_clear(answer):
        return ""
    return f"\n\n(Check: {', '.join(missing[:6])} could not run in this answer: what it covers was not checked.)"


def rule_line(rule: dict[str, str]) -> str:
    """A catalog rule for the prompt; its title is not repeated when the text begins with it."""
    body = rule["text"][:600]
    return body if body.startswith(rule["title"].rstrip("\u2026")) else f"{rule['title']}: {body}"


class Agent:
    """Answers one question at a time for one user (inside supagent.security.acting_as)."""

    def __init__(self, username: str, on_step: Callable[[list[dict]], None] | None = None,
                 llm: LLM | None = None, rich_results: bool = False,
                 should_stop: Callable[[], bool] | None = None) -> None:
        from supagent import tools, tools_superset  # noqa: F401  (registers the tools)
        from supagent.superset_mcp import SupersetMCP

        self.username = username
        self.on_step = on_step
        self.should_stop = should_stop
        self.rich = rich_results
        self.llm = llm or LLM()
        disabled = set(settings.get("agent.disabled_tools") or [])
        if rich_results:
            disabled.add("show_chart")           # the page draws real charts
        from supagent.tools import chromium_binary, screenshots_possible

        if chromium_binary() is None:            # tools that cannot work on this host are not offered
            disabled.add("chart_from_sql")
        if not screenshots_possible():
            disabled.add("chart_image")
        self.registry = tools.mcp
        self.local = {n: t for n, t in self.registry.tools.items() if n not in disabled}
        self.superset = SupersetMCP(username)
        self.specs = [t.spec() for t in self.local.values()]
        if "show_chart" not in disabled:
            self.specs.append(SHOW_CHART_SPEC)
        self.specs += [s for n, s in self.superset.specs.items() if n not in disabled]
        self.names = {s["function"]["name"] for s in self.specs}
        self.guard = ChartGuard(self)
        self.max_steps = int(settings.get("agent.max_steps"))

    def close(self) -> None:
        self.superset.close()

    def _specs_for(self, question: str) -> list[dict]:
        """The tools offered for this question. In the chat page, the tools that save charts, make
        files, e-mails, reports or images are offered only when the question asks for them (every
        query result is already shown as a table and a chart): fewer tools, fewer wrong calls."""
        if not self.rich:
            return self.specs
        wanted = set().union(*(TOOLS_OF.get(k, set()) for k in intents(question))) if intents(question) else set()
        if self.wants_saved_chart:
            wanted |= TOOLS_OF["charts"]
        return [s for s in self.specs if s["function"]["name"] not in INTENT_TOOLS or s["function"]["name"] in wanted]

    def _system(self, question: str) -> str:
        """The instructions: the same for every user and every question of a kind, so that the LLM
        server keeps them and the tools (which follow them) in its prompt cache from one question
        to the next; what is found for this question and user goes with the question
        (_question_blocks)."""
        text = CORE + (RICH if self.rich else PLAIN) + OSAGG_RULES + PROMAGG_RULES
        found = intents(question) | ({"charts"} if self.wants_saved_chart else set())
        if not self.rich:
            found |= {"files", "images"}
        for k in ("charts", "investigation", "files", "images", "usual"):
            if k in found and not (k == "usual" and "investigation" in found):
                text += SECTIONS[k]
        text += "\n\nReminder: answer in the language of the question below."
        if not self.superset.available:
            text += ("\n- Saving charts and dashboards is not available here (" + (self.superset.error or "") +
                     "): answer with the query results instead.")
        extra = (settings.get("agent.extra_instructions") or "").strip()
        if extra:
            text += "\n" + extra
        try:
            from supagent.knowledge.catalog import rules

            team_rules = rules()[:30]
        except Exception:  # pylint: disable=broad-except
            team_rules = []
        if team_rules:
            text += "\n\nRules of the team (from the catalog: always follow them):"
            for r in team_rules:
                text += "\n- " + rule_line(r)
        return text

    def _question_blocks(self, question: str) -> str:
        """What is found for this question and this user, given with the question: their memories,
        where the data is, the knowledge that matches, the learned answers."""
        text = ""
        try:
            from flask import g

            from supagent.knowledge.memory import prompt_block

            text += prompt_block(getattr(getattr(g, "user", None), "id", None))
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            from supagent.knowledge.resolve import where_block

            text += where_block(question)            # where the data is, found without the LLM
        except Exception:  # pylint: disable=broad-except
            log.warning("supagent: where the data is: not found", exc_info=True)
        if settings.get("search.enabled"):
            try:
                from supagent.knowledge.search import knowledge_block

                text += knowledge_block(question)
            except Exception:  # pylint: disable=broad-except
                pass
        try:
            from supagent.knowledge.experience import recipes_for

            recipes = recipes_for(question)
        except Exception:  # pylint: disable=broad-except
            recipes = []
        if recipes:
            text += ("\n\nWays that answered similar questions before (helpful = marked Helpful by a user, "
                     "confirmed = also approved by an admin). Start "
                     "from them, adapting dates, filters and names to this question, and run the query again: "
                     "their results are not kept and are not the answer:")
            for r in recipes:
                where = f" on database id {r['database_id']}" if r.get("database_id") else ""
                speed = f", {r['seconds']:.1f} s" if r.get("seconds") is not None else ""
                text += (f"\n- Q: {r['question'][:240]}\n  {r['tool']}{where} ({r['status']}, used {r['uses']} "
                         f"time(s){speed}): {r['query'][:900]}")
        return text.strip()

    def _call(self, name: str, args: dict) -> tuple[str, str]:
        """(tool really called, its result text)."""
        if name in SAVING_TOOLS:                  # names and texts saved in Superset's own tables
            from supagent.textsafe import db_codec, fold_all

            args = fold_all(args, db_codec())
        if name == "chart_from_sql" and self.wants_saved_chart and not self.redirected_chart \
                and "generate_chart" in self.names:
            self.redirected_chart = True
            return name, ("tool error: the user asked for a chart saved in Superset: chart_from_sql only makes an "
                          "image. Find the dataset (list_datasets / get_dataset_info), check the fields with "
                          "get_chart_type_schema, then call generate_chart with save_chart=true and the requested "
                          "chart_name.")
        if name == "show_chart":
            try:
                return name, show_chart(**args)
            except Exception as ex:  # pylint: disable=broad-except
                return name, f"tool error: {ex}"
        if name in self.local:
            try:
                return name, self.registry.call_text(name, args)
            except Exception as ex:  # pylint: disable=broad-except
                return name, f"tool error: {type(ex).__name__}: {str(ex)[:1500]}"
        called, call_args, content = self.guard.before(name, args)
        if content is not None:
            return called, content
        content = self.superset.call(called, call_args)
        content = self.guard.after(called, call_args, content)
        if called != name:
            content = f"(a chart with this name was already saved: {called} was used)\n" + content
        return called, content

    def ask(self, question: str, history: list[dict] | None = None) -> tuple[str, list[dict]]:
        self.guard.saved = {}
        self.wants_saved_chart = bool(SAVED_CHART_ASK.search(question or ""))
        self.redirected_chart = False
        failed: dict[str, str] = {}
        charts: list[str] = []
        emailed: list[str] = []
        messages: list[dict] = [{"role": "system", "content": self._system(question)}]
        for h in (history or [])[-HISTORY_MESSAGES:]:
            if h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:3000]})
        lang = question_language(question)
        hint = f" {ANSWER_IN[lang]}" if lang else ""
        blocks = self._question_blocks(question)
        messages.append({"role": "user", "content": (blocks + "\n\n" if blocks else "") +
                         f"(Now: {now():%A %Y-%m-%d %H:%M}.{hint})\n{question}"})
        trace: list[dict] = []
        nudged = announced = False
        done: set[str] = set()                         # identical successful calls: not run twice
        self.usage = {}
        specs = self._specs_for(question)
        for _ in range(self.max_steps):
            self._check_stop()
            try:
                msg = self.llm.chat(messages, tools=specs)
            except EmptyAnswer:
                add_usage(self.usage, self.llm.last_usage)
                fallback = self._empty_fallback(trace)
                if fallback is None:
                    raise
                return fallback, trace
            add_usage(self.usage, self.llm.last_usage)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
                nudge = None if nudged else unsupported_answer(answer, trace)
                if nudge:                              # once: an answer from the tools, not from the summary
                    nudged = True
                    self.usage["nudges"] = self.usage.get("nudges", 0) + 1
                    log.info("supagent: answer sent back (no tool / no query): %r", answer[-200:])
                    messages.append({"role": "user", "content": nudge})
                    continue
                step = None if announced else announces_action(answer)
                if step:                               # once: "Let me run the query." with no tool call
                    announced = True
                    self.usage["nudges"] = self.usage.get("nudges", 0) + 1
                    log.info("supagent: answer sent back (announced step): %r", step)
                    messages.append({"role": "user", "content": ANNOUNCE_NUDGE.format(step=step)})
                    continue
                missing = [c for c in charts if c.splitlines()[-1].strip() not in answer]
                if missing:
                    answer += "".join(f"\n\n```\n{c}\n```" for c in missing)
                if not self.rich:
                    answer += table_if_missing(answer, trace)
                    answer += chart_if_missing(answer, trace)
                else:
                    answer = local_links(answer)       # the page shows the files itself
                    if any(t.get("full") for t in trace):
                        answer = trim_tables(answer)   # every row is in the page's result view
                note = unsupported_note(answer, trace) if nudged else ""
                return answer + claims_check(answer, trace) + honesty_note(answer, trace) + note, trace
            for tc in calls:
                self._check_stop()
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                props = (self.superset.schema(name) or {}).get("properties", {})
                if list(props) == ["request"] and "request" not in args:
                    args = {"request": args}
                step = {"tool": name, "args": args, "status": "running", "started": time.time()}
                trace.append(step)
                self._report(trace)
                t0 = time.time()
                call_key = name + json.dumps(args, sort_keys=True, default=str)
                called = name
                if name not in self.names:
                    content = f"unknown tool {name}"
                elif name == "send_email" and emailed:
                    content = (f"An e-mail was already sent for this request ({emailed[0]}): do not send another "
                               "one; tell the user what it contained.")
                elif call_key in failed:
                    content = (f"You already made exactly this call and it failed: {failed[call_key][:600]}. "
                               "Change the arguments as the error says, or answer the user.")
                elif call_key in done:
                    content = REPEAT_NOTE
                else:
                    called, content = self._call(name, args)
                    if name == "show_chart" and not content.startswith("tool error"):
                        charts.append(content)
                if re.search(r'"(error|success)":\s*("|false)|^(error|tool error|unknown tool)', content[:300]):
                    failed[call_key] = content
                    step["status"] = "error"
                else:
                    step["status"] = "done"
                    if called == "send_email" and '"sent_to"' in content:
                        emailed.append(content[:300])
                    if content is not REPEAT_NOTE:
                        if called in SAVING_TOOLS:     # something changed: reading it again is not a repeat
                            done.clear()
                        else:
                            done.add(call_key)
                if called in RESULT_TOOLS and step["status"] == "done" and content is not REPEAT_NOTE:
                    step["full"] = content               # for the page, never sent to the model whole
                    if called == "execute_sql":
                        from supagent.knowledge.experience import compact_for_llm

                        content = compact_for_llm(content, question)
                limit = TOOL_CHARS.get(name, MAX_TOOL_CHARS)
                if len(content) > limit:
                    content = content[:limit] + "\n...[truncated]"
                step.update(called=called, seconds=round(time.time() - t0, 1), result=content[:4000])
                self._report(trace)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", name), "content": content})
        return "(stopped after too many tool calls: ask a narrower question)", trace

    def _empty_fallback(self, trace: list[dict]) -> str | None:
        """The LLM wrote nothing, even when asked again: the last query result, said plainly (None
        when no query succeeded: the answer fails as before)."""
        last = next((t for t in reversed(trace) if t.get("status") == "done"
                     and (t.get("called") or t["tool"]) in RESULT_TOOLS), None)
        if last is None:
            return None
        if self.rich:
            return EMPTY_RICH
        rows = _sql_rows(last)
        if rows and len(rows) <= 30:
            cols = list(rows[0])
            table = "\n".join(["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
                              + ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows])
        else:
            table = "```\n" + (last.get("result") or "")[:3000] + "\n```"
        return EMPTY_PLAIN + "\n\n" + table

    def _check_stop(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise Cancelled("stopped by the user")

    def _report(self, trace: list[dict]) -> None:
        if self.on_step is not None:
            try:
                self.on_step(trace)
            except Cancelled:
                raise
            except Exception:  # pylint: disable=broad-except
                log.exception("supagent: step report failed")
