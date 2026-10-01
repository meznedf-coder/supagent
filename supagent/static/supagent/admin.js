/* supagent admin page: settings, LLM test, learning runs (the knowledge is edited in the Data dictionary) */
(function () {
  "use strict";
  var S = window.supagent, el = S.el;
  var $ = function (id) { return document.getElementById(id); };
  var specs = {};
  var LABELS = {
    "llm.base_url": "API base URL", "llm.model": "Model", "llm.auth": "Authentication", "llm.token": "Access token",
    "llm.middleware.token_url": "Middleware token URL", "llm.middleware.consumer_key": "Consumer key",
    "llm.middleware.consumer_secret": "Consumer secret", "llm.middleware.cert_path": "Client certificate (PEM file)",
    "llm.middleware.key_path": "Client key (PEM file)", "llm.middleware.scope": "Scope",
    "llm.middleware.renew_before": "Renew the token before expiry (s)", "llm.ca_bundle": "CA bundle (PEM file)",
    "llm.verify_tls": "Check TLS certificates", "llm.timeout": "Timeout (s)", "llm.temperature": "Temperature",
    "llm.thinking": "Reasoning (thinking) mode", "llm.extra_headers": "Extra HTTP headers (JSON)",
    "agent.max_steps": "Tool calls per question", "agent.now": "Fixed “now”", "agent.extra_instructions": "Extra instructions",
    "agent.disabled_tools": "Disabled tools", "agent.check_numbers": "Check the numbers of the answers",
    "usage.keep_days": "Keep the LLM calls (days)", "agent.executor": "Where answers run", "agent.celery_queue": "Celery queue",
    "learn.enabled": "Learn once a day", "learn.hour": "Daily at (hour)", "learn.days": "On these days",
    "learn.user": "Learn as user", "learn.profile_every_days": "Statistics refreshed every (days)",
    "learn.max_requests_per_minute": "Requests per minute (each database)", "learn.request_timeout": "Request timeout (s)",
    "learn.stop_after_errors": "Stop after failures in a row", "learn.stats_max_series": "Statistics up to (series)",
    "learn.series_sample": "Series sampled per metric", "learn.sample_docs": "Documents sampled per index",
    "learn.fields_per_request": "Field statistics per request", "learn.group_rollover": "Group dated / rolled-over indices",
    "learn.databases": "Databases", "learn.indices": "Only these indices", "learn.indices_exclude": "Never these indices",
    "learn.metrics": "Only these metrics", "learn.metrics_exclude": "Never these metrics",
    "learn.max_minutes": "Time limit (minutes)", "learn.max_objects": "Metrics or indices per database at most",
    "learn.profile_hours": "Statistics window (hours)", "learn.llm_descriptions": "LLM descriptions",
    "mcp.user": "User of the MCP server mode",
    "tools.export_dir": "Export directory", "tools.export_max_rows": "Excel rows at most",
    "tools.email_allowed_domains": "Allowed e-mail domains", "tools.keep_days": "Keep answer files (days)",
    "tools.max_file_mb": "Largest file kept (MB)", "agent.celery_queue": "Celery queue",
    "search.enabled": "Give the relevant knowledge with each question", "search.top_k": "Pieces of knowledge per question",
    "search.prompt_chars": "Characters of knowledge per question", "embed.model": "Embedding model",
    "embed.base_url": "Embedding API base", "embed.batch": "Texts per embedding request",
    "embed.per_run": "Pieces embedded per run", "search.vector_store": "Vectors kept in",
    "qdrant.url": "Qdrant server", "qdrant.api_key": "Qdrant API key", "qdrant.collection": "Qdrant collection",
    "docs.allowed_domains": "Allowed domains for documents", "docs.max_kb": "Largest page or file (KB)",
    "memory.enabled": "Learn from the chats", "memory.team_approval": "Team memories need approval",
    "memory.prompt_chars": "Characters of memories per question", "chats.keep_days": "Keep chats (days, 0: always)",
    "agent.queue_keep_days": "Keep delivered queue messages (days, 0: always)",
    "context.enabled": "Context every night", "context.hour": "Context hour",
    "context.max_llm_calls": "LLM calls per Context build"
  };

  function status() {
    S.admin("GET", "status").then(function (s) {
      var box = $("status");
      box.innerHTML = "";
      [["supagent " + (s.version || "?") + " (tables v" + (s.schema_version || "?") + ")", !!s.schema_version],
       [s.executor === "thread" ? "answers run in the web server (agent.executor = thread)" :
        s.celery_workers ? "Celery workers answer" + (s.workers ? " (" + s.workers + ")" : "") :
        s.executor === "celery" ? "no Celery worker: questions wait for one (agent.executor = celery)" :
        "no Celery worker: answers run in the web server", !!s.celery_workers || s.executor === "thread"],
       [s.superset_mcp ? "Superset MCP tools (charts, dashboards)" : "Superset MCP service not installed: no chart saving", !!s.superset_mcp],
       [s.daily_tick_scheduled ? "daily learning in the beat schedule" : "daily learning not scheduled", !!s.daily_tick_scheduled]
      ].forEach(function (x) { box.appendChild(el("span", { class: x[1] ? "ok" : "no", text: x[0] })); });
      var last = s.last_scheduled_run ? new Date(String(s.last_scheduled_run).replace(" ", "T") + "Z") : null;
      var fresh = last && (Date.now() - last.getTime()) < 26 * 3600 * 1000;
      box.appendChild(el("span", { class: fresh ? "ok" : "no",
        text: last ? "last daily run " + S.when(s.last_scheduled_run) + " (" + s.last_scheduled_status + ")" +
                     (fresh ? "" : ": none in the last day, is Celery beat running?")
                   : "no daily run yet (Celery beat starts it at the chosen hour)" }));
    });
  }

  function input(spec) {
    var id = "s-" + spec.key.replace(/\./g, "-");
    var v = spec.value;
    var node;
    if (spec.kind === "bool") {
      node = el("input", { type: "checkbox", id: id });
      node.checked = !!v;
    } else if (spec.kind === "choice") {
      node = el("select", { id: id }, spec.choices.map(function (c) { return el("option", { value: c, text: c }); }));
      node.value = v;
    } else if (spec.secret) {
      node = el("input", { type: "password", id: id, autocomplete: "new-password",
                           placeholder: v ? "set — leave empty to keep it" : "not set" });
    } else if (spec.kind === "json" || spec.key === "agent.extra_instructions") {
      node = el("textarea", { id: id, rows: "3", spellcheck: "false" });
      node.value = spec.kind === "json" ? JSON.stringify(v || {}, null, 0) : (v || "");
    } else if (spec.kind === "int" || spec.kind === "float") {
      node = el("input", { type: "number", id: id, step: spec.kind === "float" ? "0.05" : "1" });
      node.value = v;
    } else {
      node = el("input", { type: "text", id: id });
      node.value = spec.kind === "list" ? (v || []).join(", ") : (v === null || v === undefined ? "" : v);
    }
    node.dataset.key = spec.key;
    return node;
  }

  function fieldRow(spec) {
    var inp = input(spec);
    var label = el("label", { for: inp.id }, [document.createTextNode(LABELS[spec.key] || spec.key), el("span", { class: "key", text: spec.key })]);
    if (spec.kind === "bool") {
      return el("div", { class: "field check", "data-key": spec.key }, [inp, label, el("div", { class: "help", text: spec.help })]);
    }
    return el("div", { class: "field", "data-key": spec.key }, [label, inp, el("div", { class: "help", text: spec.help })]);
  }

  function renderSettings(list) {
    specs = {};
    list.forEach(function (s) { specs[s.key] = s; });
    document.querySelectorAll(".fields[data-group]").forEach(function (box) {
      var groups = box.dataset.group.split(" ");
      box.innerHTML = "";
      list.forEach(function (s) {
        if (groups.indexOf(s.key.split(".")[0]) >= 0) box.appendChild(fieldRow(s));
      });
    });
    authVisibility();
  }

  function authVisibility() {
    var mode = ($("s-llm-auth") || {}).value;
    document.querySelectorAll('.field[data-key^="llm.middleware."]').forEach(function (f) { f.hidden = mode !== "middleware"; });
    var tok = document.querySelector('.field[data-key="llm.token"]');
    if (tok) tok.hidden = mode !== "token";
  }

  function values(groups) {
    var out = {};
    Object.keys(specs).forEach(function (k) {
      if (groups.indexOf(k.split(".")[0]) < 0) return;
      var node = document.querySelector('[data-key="' + k + '"]:not(.field)');
      if (!node) return;
      var spec = specs[k];
      var v;
      if (spec.kind === "bool") v = node.checked;
      else if (spec.kind === "list") v = node.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      else if (spec.kind === "json") { try { v = JSON.parse(node.value || "{}"); } catch (e) { v = node.value; } }
      else v = node.value;
      if (spec.secret && !v) return;
      if (!spec.secret && JSON.stringify(v) === JSON.stringify(spec.kind === "int" || spec.kind === "float" ? String(spec.value) : spec.value)) return;
      out[k] = v;
    });
    return out;
  }

  function loadSettings() { return S.admin("GET", "settings").then(function (d) { renderSettings(d.settings || []); }); }

  var STEP_WORDS = { listed: "listed", "new": "new", due: "due", profiled: "profiled", not_due: "not due", metrics: "metrics",
    indices: "indices", labels: "labels", fields: "fields", families: "families", requests: "requests", errors: "errors",
    history: "history done", history_pending: "history left", gone: "gone", written: "written by the LLM",
    copied: "copied from the same name", tokens: "LLM tokens", left: "left", same_values: "relations",
    categories: "categories", added: "added", changed: "changed", removed: "removed", unchanged: "unchanged",
    "sync.added": "search pieces added", "sync.changed": "search pieces changed", "sync.removed": "search pieces removed",
    "sync.unchanged": "search pieces unchanged", "embed.embedded": "embedded", "embed.left": "left to embed",
    answers: "Helpful answers checked", hit_at_1: "share found first", hit_at_3: "share found in the first 3",
    mrr: "mean reciprocal rank", curated: "catalog descriptions", relations: "catalog relations",
    "documents.documents_read": "documents read", "documents.refused": "definitions refused" };
  var STEP_SKIP = ["step", "at", "database", "seconds", "phase", "fallbacks", "changes", "interrupted", "last_error",
    "complete", "error", "stopped"];
  function stepText(x) {
    var bits = [];
    Object.keys(x).forEach(function (k) {
      var v = x[k];
      if (STEP_SKIP.indexOf(k) >= 0 || v === null || v === undefined || typeof v === "object" || v === 0) return;
      bits.push((typeof v === "number" ? S.num(v) : String(v)) + " " + (STEP_WORDS[k] || k.replace(/_/g, " ")));
    });
    Object.keys(x.fallbacks || {}).forEach(function (k) {
      var f = x.fallbacks[k];
      bits.push(k + ": " + S.num(f.batches) + " batches read one metric at a time (" + f.first_error + ")");
    });
    if (x.changes && Object.keys(x.changes).length) bits.push("changes: " + Object.keys(x.changes).map(function (k) {
      return S.num(x.changes[k]) + " " + k; }).join(", "));
    ["error", "stopped", "last_error", "interrupted"].forEach(function (k) { if (x[k]) bits.push(k.replace("_", " ") + ": " + x[k]); });
    return bits.join(", ");
  }
  var openSteps = {};                                  // runs whose steps are shown (kept across refreshes)
  function stepsBox(r) {
    var steps = (r.stats || {}).steps || [];
    if (!steps.length) return null;
    var box = el("details", { class: "steps", open: openSteps[r.id] ? "open" : null,
      ontoggle: function () { openSteps[r.id] = box.open; } }, [el("summary", { text: "steps (" + steps.length + ")" })]);
    var list = el("ol", {});
    steps.forEach(function (x) {
      var when = x.at ? new Date(x.at + "Z").toLocaleTimeString() : "";
      var took = x.seconds !== undefined ? S.num(Math.round(x.seconds)) + " s" : "running" + (x.phase ? " (" + x.phase + ")" : "");
      list.appendChild(el("li", { text: [when, x.step + (x.database ? ": " + x.database : ""), took, stepText(x)]
        .filter(Boolean).join(" · ") }));
    });
    box.appendChild(list);
    return box;
  }

  var runsPage = { page: 0, size: 15, total: 0 };
  function runs() {
    return S.admin("GET", "runs?page=" + runsPage.page + "&size=" + runsPage.size).then(function (d) {
      runsPage.total = d.total || 0;
      S.pager($("runs-pager"), runsPage, runs);
      var tb = $("runs").querySelector("tbody");
      tb.innerHTML = "";
      var running = false;
      (d.runs || []).forEach(function (r) {
        var st = r.stats || {};
        var secs = st.seconds !== undefined ? Math.round(st.seconds) + " s" : "";
        var parts = [];
        Object.keys(st.databases || {}).forEach(function (name) {
          var x = st.databases[name];
          var bits = [];
          ["metrics", "labels", "indices", "fields"].forEach(function (k) { if (x[k]) bits.push(S.num(x[k]) + " " + k); });
          if (x.due) bits.push(S.num(x.profiled || 0) + " of " + S.num(x.due) + " due today profiled");
          if (x.complete === false) bits.push("stopped at its share of the time (learn.max_minutes): the next run continues");
          if (x.history_pending) bits.push(S.num(x.history_pending) + " history lookups left for the next runs");
          if (x.skipped_unchanged) bits.push(S.num(x.skipped_unchanged) + " unchanged today");
          if (x.error) bits.push("error: " + x.error);
          if (x.llm && (x.llm.written || x.llm.left)) bits.push(S.num(x.llm.written || 0) + " AI descriptions" + (x.llm.left ? " (" + S.num(x.llm.left) + " left)" : ""));
          parts.push(name + ": " + (bits.join(", ") || "nothing"));
        });
        if (st.now && (r.status === "running" || r.status === "stopping")) {       // the run in progress
          var next = (st.plan || []).filter(function (n) { return n !== st.now && !(st.databases || {})[n]; });
          var cur = (st.steps || []).filter(function (x) { return x.seconds === undefined; })[0];
          var doing = cur ? " (" + (cur.phase || cur.step) + (stepText(cur) ? ": " + stepText(cur) : "") + ")" : "";
          parts.unshift("now: " + st.now + doing + (next.length ? "; next: " + next.join(", ") : ""));
        }
        if (r.progress) parts.push("so far: " + S.num(r.progress.new_objects) + " new objects");
        if (st.during) parts.push("AI descriptions while reading: " + S.num(st.during.written) + " written by the LLM in " +
          S.num(st.during.requests) + " calls (" + S.num(st.during.tokens) + " tokens), " + S.num(st.during.copied) +
          " copied from the same name");
        Object.keys(st.skipped || {}).forEach(function (name) { parts.push(name + ": not learned: " + st.skipped[name]); });
        if (st.relations) parts.push("relations: " + (st.relations.same_values || 0) + " measured");
        if (st.llm) parts.push("AI descriptions: " + (st.llm.written || 0) + (st.llm.left ? ", " + st.llm.left +
          " left (the next run goes on)" : "") + (st.llm.error ? " (" + st.llm.error + ")" : ""));
        if (r.error && !/^stop asked at /.test(r.error)) parts.push(r.error);
        if (r.status === "running" || r.status === "stopping") running = true;
        tb.appendChild(el("tr", {}, [el("td", { text: "#" + r.id }), el("td", { text: (r.kind === "context" ? "context · " : "") + r.reason }), el("td", { text: S.when(r.started_at) }),
          el("td", { text: secs }), el("td", { html: '<span class="badge ' + (r.status === "done" ? "ok" : r.status === "error" ? "bad" : "") + '">' + S.esc(r.status === "stopping" ? "stopping…" : r.status) + "</span>" }),
          el("td", { class: "num", text: S.num(r.changes) }), el("td", {}, [el("span", { text: parts.join(" · ") }), stepsBox(r)])]));
      });
      if (!(d.runs || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "7", class: "muted", text: "No learning run yet." })]));
      running = running || !!d.running;                    // on another page too
      $("learn-stop").hidden = !running;                   // Stop while a run is running
      if (running) setTimeout(runs, 5000);
    });
  }

  document.addEventListener("change", function (ev) { if (ev.target && ev.target.id === "s-llm-auth") authVisibility(); });
  document.querySelectorAll("[data-save]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var groups = btn.dataset.save.split(" ");
      var payload = values(groups);
      var res = btn.parentNode.querySelector(".result") || btn.parentNode.appendChild(el("span", { class: "result" }));
      if (!Object.keys(payload).length) { res.textContent = "nothing changed"; res.className = "result"; return; }
      btn.disabled = true;
      S.admin("POST", "settings", { settings: payload }).then(function (r) {
        btn.disabled = false;
        var errs = Object.keys(r.errors || {}).map(function (k) { return k + ": " + r.errors[k]; });
        res.textContent = errs.length ? errs.join("; ") : "saved " + (r.saved || []).length + " setting(s)";
        res.className = "result " + (errs.length ? "bad" : "good");
        if (r.settings) renderSettings(r.settings);
      });
    });
  });
  $("test-llm").addEventListener("click", function () {
    var res = $("llm-result");
    res.textContent = "asking the LLM…";
    res.className = "result";
    S.admin("POST", "test-llm", {}).then(function (r) {
      if (r.ok) {
        res.textContent = "OK: model " + r.model + (r.token ? ", middleware token " + r.token : "") + ", answer “" + r.answer + "” in " + r.seconds + " s";
        res.className = "result good";
      } else {
        res.textContent = r.error || "failed";
        res.className = "result bad";
      }
    });
  });
  $("learn-now").addEventListener("click", function () {
    var res = $("learn-result");
    S.admin("POST", "learn", {}).then(function (r) {
      res.textContent = r.error || "started (" + r.started + ")";
      res.className = "result " + (r.error ? "bad" : "good");
      setTimeout(runs, 1500);
    });
  });
  $("learn-stop").addEventListener("click", function () {
    var res = $("learn-result"), btn = $("learn-stop");
    btn.disabled = true;
    S.admin("POST", "learn/stop", {}).then(function (r) {
      btn.disabled = false;
      res.textContent = r.error || "run #" + r.stopped + " stopped: it keeps what it learned; Learn now starts a new one";
      res.className = "result " + (r.error ? "bad" : "good");
      setTimeout(runs, 1000);
    });
  });
  // ---------------------------------------------------------------- the knowledge: in the Data dictionary
  var dictUrl = document.body.dataset.dictionaryUrl || "";
  [["go-review", "review"], ["go-catalog", "catalog"], ["go-memory", "memory"], ["go-docs", "docs"]].forEach(function (x) {
    $(x[0]).href = dictUrl + "#" + x[1];
  });
  S.admin("GET", "review?limit=1").then(function (d) {
    if (d.waiting) $("go-review").textContent = "What waits for review (" + S.num(d.waiting) + ")";
  });
  status();
  loadSettings();
  runs();
})();
