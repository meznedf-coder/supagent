/* The LLM usage page (admins): the report of /supagent/admin/api/usage for a period, drawn with the
   chat's result views (supagentViz): totals, tokens over time by task, per person, task and model, the
   biggest contexts and the slowest answers. */
(function () {
  "use strict";
  var S = window.supagent, V = window.supagentViz, el = S.el;
  var $ = function (id) { return document.getElementById(id); };
  var last = null, measure = "tokens";

  function iso(d) { return d.toISOString().slice(0, 19); }
  function localInput(d) {
    var z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return z.toISOString().slice(0, 16);
  }
  function period() {
    var p = $("preset").value, end = new Date(), start;
    if (p === "custom") {
      start = $("from").value ? new Date($("from").value) : new Date(end.getTime() - 86400000);
      end = $("to").value ? new Date($("to").value) : end;
    } else {
      start = new Date(end.getTime() - Number(p) * 86400000);
    }
    return { start: start, end: end };
  }
  function tile(label, value, hint) {
    return el("div", { class: "tile" }, [el("div", { class: "tile-label", text: label }),
      el("div", { class: "tile-value", text: value }), hint ? el("div", { class: "tile-hint muted", text: hint }) : null]);
  }
  function secs(v) { return v >= 60 ? S.num(Math.round(v / 6) / 10) + " min" : S.num(v) + " s"; }
  function table(id, cols, rows) {
    var t = $(id);
    t.innerHTML = "";
    var head = el("tr", {}, cols.map(function (c) { return el("th", { class: c.num ? "num" : "", text: c.label }); }));
    t.appendChild(el("thead", {}, [head]));
    var body = el("tbody", {});
    rows.forEach(function (r) {
      body.appendChild(el("tr", {}, cols.map(function (c) {
        var v = typeof c.get === "function" ? c.get(r) : r[c.key];
        return el("td", { class: c.num ? "num" : "", text: v === null || v === undefined ? "" : (c.num ? S.num(v) : String(v)) });
      })));
    });
    if (!rows.length) body.appendChild(el("tr", {}, [el("td", { colspan: String(cols.length), class: "muted", text: "Nothing in this period." })]));
    t.appendChild(body);
  }
  function drawSeries() {
    var host = $("series");
    host.innerHTML = "";
    if (!last || !last.series.rows.length) { host.appendChild(el("p", { class: "muted", text: "No LLM call in this period." })); return; }
    var k = measure === "tokens" ? 2 : 3;
    var res = { columns: ["time", "task", measure], rows: last.series.rows.map(function (r) { return [r[0], r[1], r[k]]; }) };
    var p = V.plan(res);
    if (p.kind === "line") V.drawLine(host, res, p, p.nums[0]);
    else if (p.kind === "bar") V.drawBars(host, res, p, p.nums[0]);
    else V.drawTable(host, res);
    $("series-caption").textContent = (measure === "tokens" ? "Tokens (context + answer)" : "LLM calls") + " per " +
      last.grain + ", in your time zone";
  }
  function render(d) {
    last = d;
    var t = d.totals, a = d.answers;
    $("usage-note").textContent = d.truncated ? "Only the first 500,000 calls of this period are counted: choose a shorter one." : "";
    var tiles = $("tiles");
    tiles.innerHTML = "";
    [tile("Tokens", S.num(t.tokens), S.num(t.prompt_tokens) + " sent, " + S.num(t.completion_tokens) + " written"),
     tile("LLM calls", S.num(t.calls), t.errors ? S.num(t.errors) + " failed" : "none failed"),
     tile("Context size", S.num(t.avg_prompt) + " tokens", "on average; 95% under " + S.num(t.p95_prompt) + ", largest " + S.num(t.max_prompt)),
     tile("From the prompt cache", S.num(t.cache_share) + " %", S.num(t.cached_tokens) + " tokens not processed again"),
     tile("Time per call", secs(t.avg_seconds), "95% under " + secs(t.p95_seconds)),
     tile("People", S.num(t.people), "who asked the agent")].forEach(function (x) { tiles.appendChild(x); });
    var at = $("answer-tiles");
    at.innerHTML = "";
    [tile("Answers", S.num(a.count), a.count ? "in " + secs(a.avg_seconds) + " on average, 95% under " + secs(a.p95_seconds) : ""),
     tile("Calls per answer", S.num(a.avg_llm_calls) + " LLM, " + S.num(a.avg_tool_calls) + " tools", S.num(a.failed_tool_calls) + " tool calls failed"),
     tile("Sent back by the checks", S.num(a.sent_back), "no tool, a number or a rule not from the data"),
     tile("Answers marked", S.num(a.marked), "a check note left in the answer")].forEach(function (x) { at.appendChild(x); });
    drawSeries();
    var common = [
      { label: "Tokens", key: "tokens", num: true }, { label: "Calls", key: "calls", num: true },
      { label: "Avg context", key: "avg_prompt", num: true }, { label: "Largest", key: "max_prompt", num: true },
      { label: "Cache %", key: "cache_share", num: true }, { label: "Avg s", key: "avg_seconds", num: true },
      { label: "Failed", key: "errors", num: true }];
    table("by-user", [{ label: "Person", key: "user" }, { label: "Answers", key: "answers", num: true }].concat(common), d.by_user);
    table("by-task", [{ label: "Task", key: "label" }].concat(common), d.by_task);
    table("by-model", [{ label: "Model", key: "model" }].concat(common), d.by_model);
    table("biggest", [{ label: "When", get: function (r) { return S.whenFull(r.at); } }, { label: "Task", key: "task" },
      { label: "For", key: "user" }, { label: "Context", key: "prompt_tokens", num: true },
      { label: "Cached", key: "cached_tokens", num: true }, { label: "s", key: "seconds", num: true }], d.biggest_contexts);
    table("slowest", [{ label: "Question", key: "question" }, { label: "For", key: "user" },
      { label: "s", key: "seconds", num: true }, { label: "LLM calls", key: "llm_calls", num: true },
      { label: "Tool calls", key: "tool_calls", num: true }, { label: "Context", key: "prompt_tokens", num: true }], d.slowest_answers);
  }
  function load() {
    var p = period(), g = $("grain").value;
    var q = "usage?start=" + encodeURIComponent(iso(p.start)) + "&end=" + encodeURIComponent(iso(p.end)) +
      "&tz=" + encodeURIComponent(-new Date().getTimezoneOffset()) + (g === "auto" ? "" : "&grain=" + g);
    $("usage-note").textContent = "Loading...";
    S.admin("GET", q).then(render, function (e) { $("usage-note").textContent = "Could not load the usage: " + (e && e.message || e); });
  }
  $("preset").addEventListener("change", function () {
    var custom = this.value === "custom";
    document.querySelectorAll("#range .custom").forEach(function (x) { x.hidden = !custom; });
    if (custom && !$("from").value) {
      $("from").value = localInput(new Date(Date.now() - 7 * 86400000));
      $("to").value = localInput(new Date());
    }
    if (!custom) load();
  });
  $("range").addEventListener("submit", function (ev) { ev.preventDefault(); load(); });
  document.querySelectorAll("[data-measure]").forEach(function (b) {
    b.addEventListener("click", function () {
      measure = b.dataset.measure;
      document.querySelectorAll("[data-measure]").forEach(function (x) { x.classList.toggle("on", x === b); });
      drawSeries();
    });
  });
  load();
})();
