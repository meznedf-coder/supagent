/* supagent admin page: settings, LLM test, learning runs, catalog */
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
    "agent.disabled_tools": "Disabled tools", "agent.executor": "Where answers run", "agent.celery_queue": "Celery queue",
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
    "agent.queue_keep_days": "Keep delivered queue messages (days, 0: always)"
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

  function runs() {
    return S.admin("GET", "runs").then(function (d) {
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
          parts.unshift("now: " + st.now + (next.length ? "; next: " + next.join(", ") : ""));
        }
        if (r.progress) parts.push("so far: " + S.num(r.progress.objects) + " objects learned or updated, " +
          S.num(r.progress.ai_descriptions) + " AI descriptions");
        Object.keys(st.skipped || {}).forEach(function (name) { parts.push(name + ": not learned: " + st.skipped[name]); });
        if (st.relations) parts.push("relations: " + (st.relations.same_values || 0) + " measured");
        if (st.llm) parts.push("AI descriptions: " + (st.llm.written || 0) + (st.llm.left ? ", " + st.llm.left +
          " left (the next run goes on)" : "") + (st.llm.error ? " (" + st.llm.error + ")" : ""));
        if (r.error && !/^stop asked at /.test(r.error)) parts.push(r.error);
        if (r.status === "running" || r.status === "stopping") running = true;
        tb.appendChild(el("tr", {}, [el("td", { text: "#" + r.id }), el("td", { text: r.reason }), el("td", { text: S.when(r.started_at) }),
          el("td", { text: secs }), el("td", { html: '<span class="badge ' + (r.status === "done" ? "ok" : r.status === "error" ? "bad" : "") + '">' + S.esc(r.status === "stopping" ? "stopping…" : r.status) + "</span>" }),
          el("td", { class: "num", text: S.num(r.changes) }), el("td", { text: parts.join(" · ") })]));
      });
      if (!(d.runs || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "7", class: "muted", text: "No learning run yet." })]));
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
      res.textContent = r.error || "stopping run #" + r.stopping + ": it keeps what it learned; Learn now starts a new one once it stopped";
      res.className = "result " + (r.error ? "bad" : "good");
      setTimeout(runs, 1000);
    });
  });
  // ---------------------------------------------------------------- catalog entries
  var cat = { entries: [], classifications: {}, current: null };
  function catLoad(keep) {
    return S.admin("GET", "entries").then(function (d) {
      cat.entries = d.entries || [];
      cat.classifications = d.classifications || {};
      var sc = $("cat-class"), sg = $("cat-category"), ec = $("e-class"), dl = $("e-categories");
      var vc = sc.value, vg = sg.value, ve = ec.value;
      sc.innerHTML = '<option value="">All classifications</option>';
      Object.keys(cat.classifications).forEach(function (k) { sc.appendChild(el("option", { value: k, text: k })); });
      if (!ec.options.length) {          // built once: rebuilding it would reset an open editor's classification
        Object.keys(cat.classifications).forEach(function (k) { ec.appendChild(el("option", { value: k, text: k })); });
      } else if (ve) {
        ec.value = ve;
      }
      sg.innerHTML = '<option value="">All categories</option>';
      dl.innerHTML = "";
      (d.categories || []).forEach(function (c) { sg.appendChild(el("option", { value: c, text: c })); dl.appendChild(el("option", { value: c })); });
      sc.value = vc; sg.value = vg;
      var w = $("cat-warnings");
      w.innerHTML = "";
      (d.errors || []).forEach(function (e) { w.appendChild(el("div", { class: "warn", text: "Skipped (invalid): “" + e.entry + "” — " + e.error })); });
      (d.conflicts || []).forEach(function (c) { w.appendChild(el("div", { class: "warn", text: "Defined twice: " + c.kind + " " + c.name + " in “" + c.entries.join("” and “") + "”; the most recent change is used (“" + c.kept + "”)" })); });
      catRender();
      if (keep && cat.current) { var e = cat.entries.filter(function (x) { return x.id === cat.current.id; })[0]; if (e) catOpen(e); }
    });
  }
  function catRender() {
    var tb = $("cat-list").querySelector("tbody"), c = $("cat-class").value, g = $("cat-category").value, q = $("cat-q").value.trim().toLowerCase();
    var a = $("cat-author").value;
    tb.innerHTML = "";
    var rows = cat.entries.filter(function (e) {
      return (!c || e.classification === c) && (!g || e.category === g) &&
        (!a || (a === "agent") === !!e.agent) &&
        (!q || (e.title + " " + (e.content || "")).toLowerCase().indexOf(q) >= 0);
    });
    rows.forEach(function (e) {
      var title = el("td", { text: e.title + " " });
      if (e.agent) title.appendChild(el("span", { class: "badge agent", text: "agent", title: "Written by the agent" }));
      else if (e.origin) title.appendChild(el("span", { class: "badge", text: "learned, taken over", title: "Learned by the agent, then changed by a person: the agent no longer changes it" }));
      tb.appendChild(el("tr", { class: "link", tabindex: "0", onclick: function () { catOpen(e); },
                                onkeydown: function (ev) { if (ev.key === "Enter") catOpen(e); } }, [
        title, el("td", { html: '<span class="badge">' + S.esc(e.classification) + "</span>" }),
        el("td", { text: e.category || "" }),
        el("td", { html: e.enabled ? '<span class="badge ok">enabled</span>' : '<span class="badge off">disabled</span>' }),
        el("td", { class: "muted", text: S.when(e.updated_at) + (e.updated_by ? " · " + e.updated_by : "") + " · v" + e.version })
      ]));
    });
    if (!rows.length) tb.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: cat.entries.length ? "No entry matches." : "No entry yet: add one, or import a catalog (YAML)." })]));
  }
  function catHelp() { $("e-help").textContent = cat.classifications[$("e-class").value] || ""; }
  function evidenceText(e) {
    var v = e.evidence || {}, why;
    if (v.source === "answers") {
      why = "the same calculation in " + v.confirmed + " answer" + (v.confirmed > 1 ? "s" : "") + " confirmed as helpful (used " +
        v.uses + " time" + (v.uses > 1 ? "s" : "") + ")" + (v.database ? ", on database " + v.database : "") +
        (v.messages && v.messages.length ? "; answers #" + v.messages.slice(-5).join(", #") : "");
    } else if (v.source === "team memory") {
      why = "a team " + (e.classification === "rule" ? "rule" : "fact") + " approved by " + (v.approved_by || "an admin");
    } else if (v.source === "document") {
      why = v.terms + " definition" + (v.terms > 1 ? "s" : "") + " quoted word for word from “" + (v.title || v.url || "a document") + "”";
    } else {
      why = "its evidence";
    }
    return (e.agent ? "Written by the agent from " + why + ". Edit and save it to take it over (the agent will not change it any more); delete it and the agent will not write it again."
                    : "Learned by the agent from " + why + ", then changed by " + (e.updated_by || "a person") + ": the agent no longer changes it.");
  }
  function catOpen(e) {
    cat.current = e;
    $("entry-title-h").textContent = e ? e.title : "New entry";
    $("e-title").value = e ? e.title : "";
    $("e-class").value = e ? e.classification : "rule";
    $("e-category").value = e ? (e.category || "") : "";
    $("e-fmt").value = e ? (e.fmt || "yaml") : "text";
    $("e-enabled").checked = e ? e.enabled : true;
    $("e-content").value = e ? (e.content || "") : "";
    $("e-result").textContent = ""; $("e-result").className = "result";
    $("e-confirm").hidden = true; $("e-delete").hidden = !e;
    $("e-history-box").hidden = !e; $("e-history").innerHTML = ""; $("e-history-box").open = false;
    $("e-evidence").hidden = !(e && e.origin);
    $("e-evidence").textContent = e && e.origin ? evidenceText(e) : "";
    catHelp();
    $("entry-drawer").hidden = false;
    $("e-title").focus();
  }
  function catHistory() {
    if (!cat.current) return;
    S.admin("GET", "entries/" + cat.current.id + "/history").then(function (d) {
      var box = $("e-history"); box.innerHTML = "";
      (d.versions || []).forEach(function (v) {
        var pre = el("pre", { text: v.content, hidden: true });
        box.appendChild(el("div", { class: "version" }, [
          el("div", {}, [el("strong", { text: "v" + v.version }), document.createTextNode(" · " + S.when(v.changed_at) + " · " + (v.changed_by || "") + (v.deleted ? " · deleted" : "") + (v.enabled === false ? " · disabled" : "") + " "),
            el("button", { type: "button", class: "linkish", text: "view", onclick: function () { pre.hidden = !pre.hidden; } }),
            v.deleted || v.version === cat.current.version ? null : el("button", { type: "button", class: "linkish", text: "restore this version", onclick: function () {
              S.admin("POST", "entries/" + cat.current.id + "/restore", { version: v.version }).then(function (r) {
                if (r.error) { $("e-result").textContent = r.error; $("e-result").className = "result bad"; return; }
                cat.current = r.entry; catLoad(true);
              });
            } })]), pre]));
      });
    });
  }
  $("e-history-box").addEventListener("toggle", function () { if ($("e-history-box").open) catHistory(); });
  $("e-class").addEventListener("change", function () {
    catHelp();
    if (!cat.current) $("e-fmt").value = ["rule", "note", "formula"].indexOf($("e-class").value) >= 0 ? "text" : "yaml";
  });
  $("e-save").addEventListener("click", function () {
    var body = { title: $("e-title").value, classification: $("e-class").value, category: $("e-category").value,
                 fmt: $("e-fmt").value, enabled: $("e-enabled").checked, content: $("e-content").value };
    if (cat.current) body.version = cat.current.version;
    var res = $("e-result");
    S.admin(cat.current ? "PUT" : "POST", cat.current ? "entries/" + cat.current.id : "entries", body).then(function (r) {
      if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
      cat.current = r.entry;
      res.textContent = "saved (v" + r.entry.version + ")" + (r.applied && r.applied.curated ? ", applied to " + r.applied.curated + " objects" : "");
      res.className = "result good";
      $("entry-title-h").textContent = r.entry.title; $("e-delete").hidden = false; $("e-history-box").hidden = false;
      catLoad(false);
    });
  });
  $("e-delete").addEventListener("click", function () { $("e-confirm").hidden = false; $("e-delete").hidden = true; });
  $("e-delete-no").addEventListener("click", function () { $("e-confirm").hidden = true; $("e-delete").hidden = false; });
  $("e-delete-yes").addEventListener("click", function () {
    S.admin("DELETE", "entries/" + cat.current.id + "?version=" + cat.current.version).then(function (r) {
      if (r.error) { $("e-result").textContent = r.error; $("e-result").className = "result bad"; return; }
      $("entry-drawer").hidden = true; cat.current = null; catLoad(false);
    });
  });
  $("entry-close").addEventListener("click", function () { $("entry-drawer").hidden = true; });
  $("cat-new").addEventListener("click", function () { catOpen(null); });
  ["cat-class", "cat-category", "cat-author"].forEach(function (id) { $(id).addEventListener("change", catRender); });
  $("cat-q").addEventListener("input", catRender);
  $("cat-import").addEventListener("click", function () { $("import-result").textContent = ""; $("import-drawer").hidden = false; });
  $("import-close").addEventListener("click", function () { $("import-drawer").hidden = true; });
  $("import-go").addEventListener("click", function () {
    var res = $("import-result");
    S.admin("POST", "catalog", { content: $("import-text").value, mode: $("import-replace").checked ? "replace" : "merge" }).then(function (r) {
      if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
      var i = r.imported || {};
      res.textContent = "added " + (i.added || 0) + ", updated " + (i.updated || 0) + ", unchanged " + (i.unchanged || 0) +
                        (i.deleted ? ", deleted " + i.deleted : "") + " entries";
      res.className = "result good";
      catLoad(false);
    });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { $("entry-drawer").hidden = true; $("import-drawer").hidden = true; }
  });
  catLoad(false);

  // ---------------------------------------------------------------- team memory
  function teamMemory() {
    S.admin("GET", "memory").then(function (d) {
      var tb = $("team-memory").querySelector("tbody");
      tb.innerHTML = "";
      (d.memory || []).forEach(function (m) {
        var text = el("td", { text: m.text });
        var acts = el("td", {});
        var act = function (label, body) {
          acts.appendChild(el("button", { type: "button", class: "linkish", text: label, onclick: function () {
            S.admin("POST", "memory/" + m.id, body).then(teamMemory);
          } }));
        };
        if (m.status === "catalog") {
          acts.appendChild(el("span", { class: "muted", text: "edit it in the catalog" }));
        } else {
        if (m.status !== "active") act("Approve", { status: "active" });
        if (m.status !== "disabled") act("Disable", { status: "disabled" });
        acts.appendChild(el("button", { type: "button", class: "linkish", text: "Edit", onclick: function () {
          var ta = el("textarea", { rows: "2" }); ta.value = m.text;
          text.innerHTML = ""; text.appendChild(ta);
          text.appendChild(el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () {
            S.admin("POST", "memory/" + m.id, { text: ta.value }).then(teamMemory);
          } }));
        } }));
        }
        tb.appendChild(el("tr", {}, [text, el("td", { text: m.kind + (m.category ? ", " + m.category : "") }),
          el("td", { text: m.by + (m.source === "chat" ? " (from a chat)" : "") }),
          el("td", { html: '<span class="badge ' + (m.status === "active" ? "ok" : m.status === "disabled" ? "off" : m.status === "catalog" ? "agent" : "") + '">' + S.esc(m.status === "catalog" ? "in the catalog" : m.status) + "</span>" }),
          acts]));
      });
      if (!(d.memory || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "No team memory yet." })]));
    });
  }
  teamMemory();

  // ---------------------------------------------------------------- documents and sites
  function docs() {
    S.admin("GET", "docs").then(function (d) {
      var tb = $("docs").querySelector("tbody");
      tb.innerHTML = "";
      (d.docs || []).forEach(function (x) {
        var acts = el("td", {});
        if (x.kind === "url") acts.appendChild(el("button", { type: "button", class: "linkish", text: "Read again", onclick: function () {
          S.admin("POST", "docs/" + x.id + "/refresh", {}).then(function () { setTimeout(docs, 3000); });
        } }));
        acts.appendChild(el("button", { type: "button", class: "linkish", text: "Delete", onclick: function () {
          if (acts.dataset.sure) { S.admin("DELETE", "docs/" + x.id).then(docs); }
          else { acts.dataset.sure = "1"; this.textContent = "Delete: sure?"; }
        } }));
        tb.appendChild(el("tr", {}, [el("td", {}, [el("div", { text: x.title || x.url || "document" }),
            x.url ? el("div", { class: "muted nm", text: x.url }) : null]),
          el("td", { text: x.category || "" }), el("td", { class: "num", text: S.num((x.pages || []).length || (x.kind === "upload" ? 1 : 0)) }),
          el("td", { class: "num", text: S.bytes(x.chars) }),
          el("td", { html: '<span class="badge ' + (x.status === "ok" ? "ok" : x.status === "error" ? "bad" : "") + '">' + S.esc(x.status) + "</span>" + (x.error ? " " + S.esc(x.error) : "") }),
          el("td", { class: "muted", text: x.fetched_at ? S.when(x.fetched_at) : "" }), acts]));
      });
      if (!(d.docs || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "7", class: "muted", text: "No document yet." })]));
    });
  }
  $("doc-add").addEventListener("click", function () {
    var res = $("doc-result");
    S.admin("POST", "docs", { url: $("doc-url").value, category: $("doc-category").value,
                              max_pages: +$("doc-pages").value, refresh_days: +$("doc-days").value }).then(function (r) {
      res.textContent = r.error || "added: reading it now"; res.className = "result " + (r.error ? "bad" : "good");
      if (!r.error) { $("doc-url").value = ""; setTimeout(docs, 3000); }
      docs();
    });
  });
  $("doc-file").addEventListener("change", function () {
    var f = this.files && this.files[0], res = $("doc-result");
    if (!f) return;
    if (f.size > 20 * 1024 * 1024) { res.textContent = "file too large (20 MB at most)"; res.className = "result bad"; return; }
    var reader = new FileReader();
    reader.onload = function () {
      S.admin("POST", "docs", { name: f.name, title: f.name, category: $("doc-category").value, content: String(reader.result) }).then(function (r) {
        res.textContent = r.error || "uploaded: " + f.name; res.className = "result " + (r.error ? "bad" : "good");
        docs();
      });
    };
    reader.readAsText(f);
    this.value = "";
  });
  docs();
  status();
  loadSettings();
  runs();
})();
