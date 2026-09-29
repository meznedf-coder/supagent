/* supagent chat page: conversations, answers with their query results (table / chart,
   copy, CSV, Excel, PNG), files (screenshots, Excel extracts), feedback, stop. */
(function () {
  "use strict";
  var S = window.supagent, V = window.supagentViz, el = S.el;
  var list = document.getElementById("conv-list");
  var box = document.getElementById("messages");
  var form = document.getElementById("ask");
  var input = document.getElementById("question");
  var send = document.getElementById("send");
  var current = null;          // conversation id
  var running = null;          // id of the answer being computed
  var polling = null;
  var openSteps = {};          // answer id -> its tool list opened by the user (kept through the refreshes)
  var STORE = "supagent.conversation";

  function remember(id) { try { if (id) localStorage.setItem(STORE, String(id)); else localStorage.removeItem(STORE); } catch (e) { /* private mode */ } }
  function remembered() { try { return parseInt(localStorage.getItem(STORE) || "", 10) || null; } catch (e) { return null; } }

  // ---------------------------------------------------------------- copy (clipboard API needs HTTPS: fallback)
  function toast(msg) {
    var t = el("div", { class: "toast", role: "status", text: msg });
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 1600);
  }
  function copy(text, what) {
    var done = function () { toast((what || "Text") + " copied"); };
    var fallback = function () {
      var ta = el("textarea", { style: "position:fixed;left:-9999px;top:0", "aria-hidden": "true" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      ta.remove();
      if (ok) done(); else toast("Copy is blocked by the browser: select the text and press Ctrl+C");
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }
  function copyButton(label, getText, what) {
    return el("button", { type: "button", class: "linkish", text: label, onclick: function () { copy(getText(), what); } });
  }

  // ---------------------------------------------------------------- conversations
  // select: a conversation id to open, undefined for the welcome page, null to keep the page
  function loadConversations(select) {
    return S.chat("GET", "conversations").then(function (data) {
      var rows = data.conversations || [];
      list.innerHTML = "";
      rows.forEach(function (c) {
        list.appendChild(el("li", { class: c.id === current ? "on" : "", "data-id": c.id }, [
          el("a", { href: "#", title: c.title || "", text: c.title || "Conversation " + c.id,
                    onclick: function (ev) { ev.preventDefault(); open(c.id); } }),
          el("button", { type: "button", title: "Delete this conversation", "aria-label": "Delete", text: "×",
                         onclick: function () { remove(c.id); } })
        ]));
      });
      if (select === null) return;
      if (select && rows.some(function (c) { return c.id === select; })) open(select);
      else welcome();
    });
  }

  function markList() {
    list.querySelectorAll("li").forEach(function (li) { li.classList.toggle("on", parseInt(li.dataset.id, 10) === current); });
  }

  function welcome() {
    current = null;
    remember(null);
    stopPolling();
    busy(null);
    box.innerHTML = "";
    var t = document.getElementById("welcome").content.cloneNode(true);
    t.querySelectorAll(".chip").forEach(function (b) {
      b.addEventListener("click", function () { input.value = b.textContent; submit(); });
    });
    box.appendChild(t);
    markList();
    input.focus();
  }

  function open(id) {
    stopPolling();
    S.chat("GET", "conversations/" + id).then(function (data) {
      if (data.error) { welcome(); return; }
      current = id;
      remember(id);
      box.innerHTML = "";
      var msgs = data.messages || [];
      msgs.forEach(function (m, i) { box.appendChild(render(m, msgs[i - 1])); });
      var last = msgs[msgs.length - 1];
      if (last && last.role === "assistant" && /^(pending|running|cancelling)$/.test(last.status)) poll(last.id);
      else busy(null);
      scrollDown();
      markList();
    });
  }

  function remove(id) {
    S.chat("DELETE", "conversations/" + id).then(function () {
      if (id === current) loadConversations(undefined); else loadConversations(null);
    });
  }

  // ---------------------------------------------------------------- one answer
  function stepsView(steps, key) {
    if (!steps || !steps.length) return null;
    var d = el("details", { class: "steps" });
    if (key !== undefined && openSteps[key]) d.open = true;
    if (key !== undefined) d.addEventListener("toggle", function () { openSteps[key] = d.open; });
    d.appendChild(el("summary", { text: steps.length + (steps.length === 1 ? " tool call: " : " tool calls: ") +
                                        steps.map(function (s) { return s.tool; }).join(", ") }));
    steps.forEach(function (s) {
      var st = el("div", { class: "step " + (s.status || "") }, [
        el("div", {}, [el("span", { class: "name", text: s.tool }),
          document.createTextNode(s.status === "running" ? "  running…" :
            (s.seconds !== undefined && s.seconds !== null ? "  " + s.seconds + " s" : "") + (s.status === "error" ? "  failed" : ""))])
      ]);
      if (s.args && Object.keys(s.args).length) st.appendChild(el("pre", { text: JSON.stringify(s.args, null, 2) }));
      if (s.result) st.appendChild(el("pre", { text: s.result }));
      d.appendChild(st);
    });
    return d;
  }

  function filesView(files) {
    if (!files || !files.length) return null;
    var f = el("div", { class: "files" });
    files.forEach(function (x) {
      var card = el("div", { class: "file-card" });
      var image = x.url && /^image\//.test(x.mime || "");
      var head = el("div", { class: "file-head" }, [el("span", { class: "file-name", text: x.name || "file" })]);
      if (x.rows !== undefined && x.rows !== null) head.appendChild(el("span", { class: "muted", text: S.num(x.rows) + " rows" + (x.truncated ? " (cut at the row limit)" : "") }));
      if (x.size) head.appendChild(el("span", { class: "muted", text: S.bytes(x.size) }));
      if (x.url) {
        head.appendChild(el("a", { class: "btn small primary", href: x.url + "?download=1", download: x.name, text: image ? "Download image" : "Download" }));
        if (image) head.appendChild(el("a", { class: "btn small", href: x.url, target: "_blank", rel: "noopener", text: "Open" }));
      } else {
        head.appendChild(el("span", { class: "muted", text: x.note || "not kept" }));
      }
      card.appendChild(head);
      if (image) {
        card.appendChild(el("a", { href: x.url, target: "_blank", rel: "noopener", title: "Open the full image" },
                            [el("img", { src: x.url, alt: x.name, loading: "lazy" })]));
      }
      f.appendChild(card);
    });
    return f;
  }

  function resultsView(m) {
    var results = m.results || [];
    if (!results.length) return null;
    var card = el("div", { class: "result-card" });
    var tabs = el("div", { class: "result-tabs", role: "tablist" });
    var holder = el("div", {});
    var show = function (n) {
      tabs.querySelectorAll("button").forEach(function (b, i) { b.classList.toggle("on", i === n); });
      holder.innerHTML = "";
      holder.appendChild(resultView(m, n, results[n]));
    };
    if (results.length > 1) {
      results.forEach(function (r, i) {
        tabs.appendChild(el("button", { type: "button", role: "tab", text: "Result " + (i + 1) + " · " + S.num(r.row_count) + (r.row_count === 1 ? " row" : " rows"),
                                        title: (r.sql || "").slice(0, 300), onclick: function () { show(i); } }));
      });
      card.appendChild(tabs);
    }
    card.appendChild(holder);
    show(results.length - 1);
    return card;
  }

  function resultView(m, n, res) {
    var p = V.plan(res);
    var wrap = el("div", {});
    var mode = p.kind === "none" ? "table" : "chart";
    var measure = (p.nums || [])[0];
    var bar = el("div", { class: "viz-toolbar" });
    var body = el("div", { class: "viz-body" });
    var caption = S.num(res.row_count) + " row" + (res.row_count === 1 ? "" : "s") + (res.database ? " · " + res.database : "") +
                  (res.truncated ? " · first " + S.num((res.rows || []).length) + " rows here: ask for an Excel extract for all" : "");
    bar.appendChild(el("span", { class: "caption", text: caption }));
    var seg = el("span", { class: "seg", role: "group", "aria-label": "View" });
    var bTable = el("button", { type: "button", text: "Table" });
    var bChart = el("button", { type: "button", text: "Chart" });
    if (p.kind === "none") { bChart.disabled = true; bChart.title = "No chart: " + p.why; }
    seg.appendChild(bTable);
    seg.appendChild(bChart);
    bar.appendChild(seg);
    var pick = null;
    if (p.kind !== "none" && p.kind !== "figures" && (p.nums || []).length > 1) {
      pick = el("select", { "aria-label": "Value to draw" }, p.nums.map(function (c, i) { return el("option", { value: i, text: c.name }); }));
      pick.addEventListener("change", function () { measure = p.nums[+pick.value]; draw(); });
      bar.appendChild(pick);
    }
    bar.appendChild(copyButton("Copy", function () { return V.csvText(res, "\t"); }, "Table"));
    bar.appendChild(el("button", { type: "button", class: "linkish", text: "CSV", title: "Download the rows as CSV",
      onclick: function () { V.download(new Blob(["﻿" + V.csvText(res, ",")], { type: "text/csv;charset=utf-8" }), "result-" + m.id + "-" + (n + 1) + ".csv"); } }));
    bar.appendChild(el("a", { class: "linkish", href: "api/messages/" + m.id + "/results/" + n + ".xlsx", text: "Excel",
      download: "result-" + m.id + "-" + (n + 1) + ".xlsx", title: "Download the rows as an Excel file" }));
    var bPng = el("button", { type: "button", class: "linkish", text: "PNG", title: "Download the chart as an image",
      onclick: function () {
        var node = body.querySelector("svg.viz-svg");
        if (node) V.png(node, "chart-" + m.id + "-" + (n + 1) + ".png").catch(function (e) { toast(String(e.message || e)); });
      } });
    bar.appendChild(bPng);
    function draw() {
      body.innerHTML = "";
      bTable.classList.toggle("on", mode === "table");
      bChart.classList.toggle("on", mode === "chart");
      if (pick) pick.hidden = mode !== "chart";
      var drawn = false;
      if (mode === "chart") {
        if (p.kind === "line") drawn = V.drawLine(body, res, p, measure);
        else if (p.kind === "bar") drawn = V.drawBars(body, res, p, measure);
        else if (p.kind === "figures") drawn = V.drawFigures(body, res, p);
        if (!drawn) { body.appendChild(el("div", { class: "viz-note", text: "Nothing to draw with these rows: here is the table." })); V.drawTable(body, res); }
      } else {
        V.drawTable(body, res);
      }
      bPng.hidden = !(mode === "chart" && body.querySelector("svg.viz-svg"));
    }
    bTable.addEventListener("click", function () { mode = "table"; draw(); });
    bChart.addEventListener("click", function () { if (!bChart.disabled) { mode = "chart"; draw(); } });
    wrap.appendChild(bar);
    wrap.appendChild(body);
    if (res.sql) {
      var d = el("details", { class: "sql" }, [el("summary", { text: res.tool === "promql_query" ? "PromQL" : "SQL" }),
        el("pre", { text: res.sql }), copyButton(res.tool === "promql_query" ? "Copy the PromQL" : "Copy the SQL", function () { return res.sql; }, "Query")]);
      wrap.appendChild(d);
    }
    // drawn once in the page (its width is known then)
    requestAnimationFrame(draw);
    return wrap;
  }

  function feedbackView(m) {
    var wrap = el("span", {});
    [[1, "Helpful"], [-1, "Not helpful"]].forEach(function (p) {
      var b = el("button", { type: "button", class: "fb" + (m.feedback === p[0] ? " on" : ""), text: p[1],
        title: p[0] === 1 ? "Keep this answer's SQL as an example for similar questions" : "Mark this answer as not helpful",
        onclick: function () {
          var value = m.feedback === p[0] ? 0 : p[0];
          S.chat("POST", "messages/" + m.id + "/feedback", { value: value }).then(function (r) {
            m.feedback = r.feedback;
            wrap.querySelectorAll(".fb").forEach(function (x) { x.classList.remove("on"); });
            if (r.feedback === p[0]) b.classList.add("on");
            if (r.example_kept) toast("Kept as an example for similar questions");
          });
        } });
      wrap.appendChild(b);
    });
    return wrap;
  }

  function codeCopyButtons(answer) {
    answer.querySelectorAll("pre").forEach(function (pre) {
      var code = pre.textContent;
      pre.appendChild(el("button", { type: "button", class: "linkish copy-code", text: "Copy", onclick: function () { copy(code, "Code"); } }));
    });
  }

  function render(m, prev) {
    if (m.role === "user") {
      return el("div", { class: "msg user", "data-id": m.id }, [
        el("div", { class: "bubble", text: m.content }),
        el("div", { class: "stamp", text: "Asked " + S.whenFull(m.created_at || new Date()) }),
        el("div", { class: "tools" }, [copyButton("Copy", function () { return m.content; }, "Question"),
          el("button", { type: "button", class: "linkish", text: "Ask again", onclick: function () { input.value = m.content; submit(); } })])
      ]);
    }
    var node = el("div", { class: "msg assistant" + (m.status === "error" ? " error" : ""), "data-id": m.id });
    node._question = prev && prev.role === "user" ? prev.content : "";
    var bubble = el("div", { class: "bubble" });
    node.appendChild(bubble);
    fill(node, bubble, m);
    return node;
  }

  function elapsed(m) {
    if (!m.created_at) return "";
    var secs = Math.round((Date.now() - new Date(String(m.created_at).replace(" ", "T") + "Z")) / 1000);
    return secs >= 0 ? " (" + (secs < 60 ? secs + " s" : Math.floor(secs / 60) + " min " + (secs % 60) + " s") + ")" : "";
  }

  function fill(node, bubble, m) {
    // redraw only what changed: an opened tool list, a selection, a scroll position stay as they are
    var sig = JSON.stringify([m.status, m.steps, m.content, m.feedback, (m.files || []).length, (m.results || []).length]);
    var ticking = /^(pending|running|cancelling)$/.test(m.status);
    if (node._sig === sig && bubble.childNodes.length) {
      if (ticking) {
        var w = bubble.querySelector(".working .what");
        if (w) w.textContent = w.dataset.base + elapsed(m);
      }
      return;
    }
    node._sig = sig;
    bubble.innerHTML = "";
    if (ticking) {
      var steps = m.steps || [];
      var last = steps[steps.length - 1];
      var what = m.status === "cancelling" ? "Stopping…" : m.status === "pending" ? "Waiting for the agent…" :
        (last && last.status === "running" ? "Running " + last.tool + "…" : "Thinking…");
      var label = el("span", { class: "what", text: what + elapsed(m) });
      label.dataset.base = what;
      bubble.appendChild(el("div", { class: "working" }, [el("span", { class: "dots", "aria-hidden": "true" }, [el("i"), el("i"), el("i")]), label]));
      var sv = stepsView(steps, m.id);
      if (sv) bubble.appendChild(sv);
      return;
    }
    var answer = el("div", { class: "answer", html: m.html || S.esc(m.content) });
    codeCopyButtons(answer);
    bubble.appendChild(answer);
    var rv = resultsView(m);
    if (rv) bubble.appendChild(rv);
    var fv = filesView(m.files);
    if (fv) bubble.appendChild(fv);
    var meta = el("div", { class: "meta" });
    meta.appendChild(copyButton("Copy answer", function () { return m.content || ""; }, "Answer"));
    if (m.status === "done") meta.appendChild(feedbackView(m));
    if (m.status === "error" || m.status === "cancelled") {
      meta.appendChild(el("button", { type: "button", class: "linkish", text: "Ask again", onclick: function () {
        if (node._question) { input.value = node._question; submit(); }
      } }));
    }
    if (m.finished_at) {
      var secs = m.created_at ? Math.round((new Date(String(m.finished_at).replace(" ", "T") + "Z") -
                                            new Date(String(m.created_at).replace(" ", "T") + "Z")) / 1000) : -1;
      meta.appendChild(el("span", { class: "stamp", text: "Answered " + S.whenFull(m.finished_at) + (secs >= 0 ? " \u00b7 " + secs + " s" : "") }));
    }
    bubble.appendChild(meta);
    var sv2 = stepsView(m.steps, m.id);
    if (sv2) bubble.appendChild(sv2);
  }

  // ---------------------------------------------------------------- asking, polling, stopping
  function busy(id) {
    running = id;
    send.textContent = id ? "Stop" : "Send";
    send.classList.toggle("stop", !!id);
    send.classList.toggle("primary", !id);
    send.title = id ? "Stop this answer" : "Send (Enter)";
  }

  /* Each poll belongs to the chat on screen: opening another chat (or a new one) ends it, and a
     poll already on its way is then ignored, so that it never turns Send into Stop elsewhere. */
  var pollRound = 0;
  function stopPolling() {
    pollRound += 1;
    if (polling) { clearTimeout(polling); polling = null; }
  }

  function poll(id) {
    stopPolling();
    var round = pollRound;
    busy(id);
    S.chat("GET", "messages/" + id).then(function (m) {
      if (round !== pollRound) return;
      var node = box.querySelector('.msg.assistant[data-id="' + id + '"]');
      if (m.error && !m.id) { polling = setTimeout(function () { poll(id); }, 4000); return; }
      if (node) {
        var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        node.className = "msg assistant" + (m.status === "error" ? " error" : "");
        fill(node, node.querySelector(".bubble"), m);
        if (nearBottom) scrollDown();
      }
      if (/^(pending|running|cancelling)$/.test(m.status)) {
        polling = setTimeout(function () { poll(id); }, 1500);
      } else {
        busy(null);
        input.focus();
        /* the agent names the chat after its first answer (a short generic title): show it */
        [6000, 20000].forEach(function (ms) { setTimeout(function () { loadConversations(null).then(markList); }, ms); });
      }
    });
  }

  function scrollDown() { box.scrollTop = box.scrollHeight; }

  function submit() {
    if (running) {                        // the button says Stop
      var stopping = running;
      S.chat("POST", "messages/" + stopping + "/cancel", {}).then(function () {
        if (running === stopping) poll(stopping);
      });
      return;
    }
    var q = input.value.trim();
    if (!q) return;
    send.disabled = true;
    S.chat("POST", "ask", { question: q, conversation_id: current }).then(function (r) {
      send.disabled = false;
      if (r.error) {
        box.appendChild(el("div", { class: "msg assistant error" }, [el("div", { class: "bubble", text: r.error })]));
        scrollDown();
        return;
      }
      input.value = "";
      if (current !== r.conversation_id) {
        current = r.conversation_id;
        remember(current);
        box.innerHTML = "";
        loadConversations(null).then(markList);
      }
      var qm = { id: -1, role: "user", content: q };
      box.appendChild(render(qm));
      box.appendChild(render({ id: r.message_id, role: "assistant", status: "pending", steps: [] }, qm));
      scrollDown();
      poll(r.message_id);
    });
  }

  // ---------------------------------------------------------------- memory
  function memoryItem(m, canDelete) {
    var li = el("li", {}, [el("span", { text: m.text }),
      el("span", { class: "muted", text: " \u00b7 " + m.kind + (m.category ? ", " + m.category : "") + (m.source === "chat" ? " \u00b7 learned from a chat" : "") })]);
    if (canDelete) {
      li.appendChild(el("button", { type: "button", class: "linkish", text: "Forget", onclick: function () {
        S.chat("DELETE", "memory/" + m.id).then(loadMemory);
      } }));
    }
    return li;
  }
  function loadMemory() {
    S.chat("GET", "memory").then(function (d) {
      var mine = document.getElementById("memory-mine"), team = document.getElementById("memory-team"),
          prop = document.getElementById("memory-proposed");
      mine.innerHTML = ""; team.innerHTML = ""; prop.innerHTML = "";
      (d.mine || []).forEach(function (m) { mine.appendChild(memoryItem(m, true)); });
      (d.team || []).forEach(function (m) { team.appendChild(memoryItem(m, d.is_admin)); });
      (d.proposed || []).forEach(function (m) { prop.appendChild(memoryItem(m, true)); });
      if (!(d.mine || []).length) mine.appendChild(el("li", { class: "muted", text: "Nothing yet." }));
      if (!(d.team || []).length) team.appendChild(el("li", { class: "muted", text: "Nothing yet." }));
      document.getElementById("memory-proposed-box").hidden = !(d.proposed || []).length;
    });
  }
  document.getElementById("open-memory").addEventListener("click", function () {
    document.getElementById("memory-drawer").hidden = false;
    loadMemory();
  });
  document.getElementById("memory-close").addEventListener("click", function () { document.getElementById("memory-drawer").hidden = true; });
  document.getElementById("memory-save").addEventListener("click", function () {
    var res = document.getElementById("memory-result"), text = document.getElementById("memory-text");
    S.chat("POST", "memory", { text: text.value, scope: document.getElementById("memory-scope").value }).then(function (r) {
      if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
      res.textContent = r.memory.status === "proposed" ? "saved: waiting for an admin's approval" : "remembered";
      res.className = "result good";
      text.value = "";
      loadMemory();
    });
  });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") document.getElementById("memory-drawer").hidden = true; });

  form.addEventListener("submit", function (ev) { ev.preventDefault(); submit(); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && !running) { ev.preventDefault(); submit(); }
  });
  document.getElementById("new-chat").addEventListener("click", welcome);

  // in the panel docked on Superset's pages: the conversations behind a button; Superset's pages
  // open in the main page (base target _top), other sites in a new tab
  if (document.body.dataset.embed === "yes") {
    input.placeholder = "Ask about your data (Enter: send, Shift+Enter: new line)";
    var convs = document.querySelector(".convs"), toggle = document.getElementById("toggle-convs");
    var fold = function (open) { convs.classList.toggle("open", open); toggle.setAttribute("aria-expanded", open ? "true" : "false"); };
    toggle.addEventListener("click", function () { fold(!convs.classList.contains("open")); });
    list.addEventListener("click", function (ev) { if (ev.target.closest("a")) fold(false); });
    document.getElementById("new-chat").addEventListener("click", function () { fold(false); });
    document.addEventListener("click", function (ev) {
      var a = ev.target.closest && ev.target.closest("a[href]");
      if (!a || a.target || a.hasAttribute("download")) return;
      var url;
      try { url = new URL(a.getAttribute("href"), location.href); } catch (e) { return; }
      if (url.origin !== location.origin) a.target = "_blank";
    }, true);
  }
  loadConversations(remembered() || undefined);
})();
