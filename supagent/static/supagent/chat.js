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

  // focus: a message to show (a chat found by the search), else the end of the conversation
  function open(id, focus) {
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
      var node = focus ? box.querySelector('.msg[data-id="' + focus + '"]') : null;
      if (node) {
        var prev = node.previousElementSibling;
        (node.classList.contains("assistant") && prev && prev.classList.contains("user") ? prev : node).scrollIntoView({ block: "start" });
        node.classList.add("found-flash");
        setTimeout(function () { node.classList.remove("found-flash"); }, 2500);
      } else {
        scrollDown();
      }
      markList();
    });
  }

  // ---------------------------------------------------------------- search in the user's own chats
  var search = document.getElementById("conv-search"), found = document.getElementById("conv-found");
  var searchTimer = null, searchSeq = 0;
  function searchChats() {
    var q = search.value.trim();
    if (q.length < 2) { searchSeq++; found.hidden = true; found.innerHTML = ""; list.hidden = false; return; }
    var seq = ++searchSeq;
    S.chat("GET", "chats/search?q=" + encodeURIComponent(q)).then(function (d) {
      if (seq !== searchSeq) return;                  // a newer search was typed meanwhile
      found.innerHTML = "";
      (d.results || []).forEach(function (r) {
        found.appendChild(el("li", { class: "found" }, [el("a", { href: "#", title: r.title,
          onclick: function (ev) { ev.preventDefault(); open(r.conversation_id, r.message_id); } }, [
            el("span", { class: "found-title", text: r.title }),
            el("span", { class: "found-text", text: r.question && r.question !== r.title ? r.question : r.snippet }),
            el("span", { class: "found-when", text: S.when(r.at) })])]));
      });
      if (!(d.results || []).length) found.appendChild(el("li", { class: "found-none", text: d.error || "No chat found." }));
      list.hidden = true;
      found.hidden = false;
    });
  }
  search.addEventListener("input", function () { clearTimeout(searchTimer); searchTimer = setTimeout(searchChats, 300); });
  search.addEventListener("keydown", function (ev) { if (ev.key === "Escape") { search.value = ""; searchChats(); } });

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
        var rows = S.num(r.row_count) + (r.row_count === 1 ? " row" : " rows");
        tabs.appendChild(el("button", { type: "button", role: "tab", text: (r.title || "Result " + (i + 1)) + " · " + rows,
                                        title: (r.title ? r.title + "\n" : "") + (r.sql || "").slice(0, 300),
                                        onclick: function () { show(i); } }));
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
    var caption = (res.title ? res.title + " · " : "") + S.num(res.row_count) + " row" + (res.row_count === 1 ? "" : "s") +
                  (res.database ? " · " + res.database : "") +
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
    var wrap = el("span", { class: "fb-wrap" });
    var why = el("form", { class: "fb-why", hidden: true });
    var input = el("input", { type: "text", maxlength: "1000", "aria-label": "What was wrong",
      placeholder: "What was wrong? e.g. killed jobs count as failed too (optional)" });
    why.appendChild(input);
    why.appendChild(el("button", { type: "submit", class: "fb", text: "Send" }));
    var said = el("span", { class: "fb-said muted" });
    function showReason() {
      said.textContent = m.feedback === -1 && m.feedback_reason ? "Your note: " + m.feedback_reason : "";
      why.hidden = !(m.feedback === -1 && !m.feedback_reason);
    }
    why.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var reason = input.value.trim();
      if (!reason) { why.hidden = true; return; }
      S.chat("POST", "messages/" + m.id + "/feedback", { value: -1, reason: reason }).then(function (r) {
        m.feedback_reason = r.feedback_reason;
        showReason();
        toast("Thank you: the admins see it, and the agent learns what it says about the data");
      });
    });
    [[1, "Helpful"], [-1, "Not helpful"]].forEach(function (p) {
      var b = el("button", { type: "button", class: "fb" + (m.feedback === p[0] ? " on" : ""), text: p[1],
        title: p[0] === 1 ? "Keep this answer's SQL as an example for similar questions" : "Mark this answer as not helpful",
        onclick: function () {
          var value = m.feedback === p[0] ? 0 : p[0];
          S.chat("POST", "messages/" + m.id + "/feedback", { value: value }).then(function (r) {
            m.feedback = r.feedback;
            m.feedback_reason = r.feedback_reason;
            wrap.querySelectorAll(".fb").forEach(function (x) { x.classList.remove("on"); });
            if (r.feedback === p[0]) b.classList.add("on");
            if (r.example_kept) toast("Kept as an example for similar questions");
            showReason();
            if (r.feedback === -1) input.focus();
          });
        } });
      wrap.appendChild(b);
    });
    wrap.appendChild(why);
    wrap.appendChild(said);
    showReason();
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
    var cmd = /^\s*\/(note|mynote)\b[ \t]*:?\s*/i.exec(q);     // "/note ..." (team), "/mynote ..." (me): saved, no LLM
    if (cmd) {
      var noteText = q.slice(cmd[0].length).trim(), scope = cmd[1].toLowerCase() === "mynote" ? "user" : "team";
      if (!noteText) { openNotes(); return; }
      send.disabled = true;
      S.chat("POST", "notes", { text: noteText, scope: scope, source: "command" }).then(function (r) {
        send.disabled = false;
        var said = r.error ? r.error : (scope === "team" ? "Note saved for the team" : "Note saved for you") +
          ": \u201c" + (r.note.title || "") + "\u201d (Notes, on the left)";
        box.appendChild(el("div", { class: "msg assistant notice" + (r.error ? " error" : "") }, [el("div", { class: "bubble", text: said })]));
        scrollDown();
        if (!r.error) input.value = "";
      });
      return;
    }
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
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { document.getElementById("memory-drawer").hidden = true; document.getElementById("notes-drawer").hidden = true; }
  });

  // ---------------------------------------------------------------- notes (0.7)
  var notes = { q: "", offset: 0, seq: 0, timer: null };
  function noteItem(n, admin) {
    var body = el("div", { class: "note-body" + ((n.text || "").length > 280 ? " folded" : ""), text: n.text || "" });
    body.addEventListener("click", function () { body.classList.remove("folded"); });
    var meta = [n.author, n.day, n.scope === "user" ? "for you only" : "team"];
    if ((n.tags || []).length) meta.push("#" + n.tags.join(" #"));
    if (n.pinned) meta.push("pinned");
    if (n.entry_id) meta.push("made a catalog entry");
    var li = el("li", { class: n.pinned ? "pinned" : "" }, [el("div", { class: "note-title", text: n.title || "Note" }),
      el("div", { class: "note-meta", text: meta.join(" \u00b7 ") }), body]);
    var acts = el("div", { class: "note-acts" });
    if (n.can_change) {
      acts.appendChild(el("button", { type: "button", class: "linkish", text: "Edit", onclick: function () { editNote(li, n); } }));
      acts.appendChild(el("button", { type: "button", class: "linkish", text: "Delete", onclick: function () {
        if (!this.dataset.sure) {                            // a second click deletes (no browser dialog)
          var b = this;
          b.dataset.sure = "1"; b.textContent = "Delete: sure?";
          setTimeout(function () { delete b.dataset.sure; b.textContent = "Delete"; }, 4000);
          return;
        }
        S.chat("DELETE", "notes/" + n.id).then(function () { loadNotes(true); });
      } }));
    }
    if (admin && n.scope === "team") {
      acts.appendChild(el("button", { type: "button", class: "linkish", text: n.pinned ? "Unpin" : "Pin for everyone",
        onclick: function () { S.chat("POST", "notes/" + n.id, { pinned: !n.pinned }).then(function () { loadNotes(true); }); } }));
      if (!n.entry_id) acts.appendChild(el("button", { type: "button", class: "linkish", text: "Make a catalog entry",
        onclick: function () {
          S.chat("POST", "notes/" + n.id + "/promote", {}).then(function (r) {
            if (r.error) { acts.appendChild(el("span", { class: "result bad", text: r.error })); return; }
            loadNotes(true);
          });
        } }));
    }
    if (acts.childNodes.length) li.appendChild(acts);
    return li;
  }
  function editNote(li, n) {
    var title = el("input", { type: "text", maxlength: "300", "aria-label": "Title" });
    var text = el("textarea", { rows: "6", maxlength: "50000", "aria-label": "The note" });
    var tags = el("input", { type: "text", maxlength: "200", "aria-label": "Tags" });
    title.value = n.title || ""; text.value = n.text || ""; tags.value = (n.tags || []).join(", ");
    var res = el("span", { class: "result" });
    li.innerHTML = "";
    [title, text, tags].forEach(function (x) { li.appendChild(x); });
    li.appendChild(el("div", { class: "note-acts" }, [
      el("button", { type: "button", class: "btn primary", text: "Save", onclick: function () {
        S.chat("POST", "notes/" + n.id, { title: title.value, text: text.value, tags: tags.value }).then(function (r) {
          if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
          loadNotes(true);
        });
      } }),
      el("button", { type: "button", class: "btn", text: "Cancel", onclick: function () { loadNotes(true); } }), res]));
    text.focus();
  }
  function loadNotes(reset) {
    if (reset) notes.offset = 0;
    var seq = ++notes.seq, ul = document.getElementById("notes-list");
    var path = "notes?limit=20&offset=" + notes.offset + (notes.q ? "&q=" + encodeURIComponent(notes.q) : "");
    return S.chat("GET", path).then(function (d) {
      if (seq !== notes.seq) return;                       // an older search answering late
      if (reset) ul.innerHTML = "";
      (d.notes || []).forEach(function (n) { ul.appendChild(noteItem(n, d.is_admin)); });
      if (!ul.childNodes.length) ul.appendChild(el("li", { class: "muted", text: notes.q ? "No note with these words." : "No note yet." }));
      document.getElementById("notes-count").textContent = d.total ? "Notes (" + S.num(d.total) + ")" : "Notes";
      notes.offset = (d.offset || 0) + (d.notes || []).length;
      document.getElementById("notes-more").hidden = notes.offset >= (d.total || 0);
      if (d.catalog) {
        var cat = document.getElementById("notes-catalog");
        cat.innerHTML = "";
        d.catalog.forEach(function (e) {
          var b = el("div", { class: "note-body folded", text: e.text || "" });
          b.addEventListener("click", function () { b.classList.remove("folded"); });
          cat.appendChild(el("li", { class: "catalog" }, [el("div", { class: "note-title", text: e.title }),
            el("div", { class: "note-meta", text: (e.category || "catalog") + " \u00b7 " + S.when(e.updated_at) }), b]));
        });
        document.getElementById("notes-catalog-box").hidden = !d.catalog.length;
      }
    });
  }
  function openNotes() {
    document.getElementById("notes-drawer").hidden = false;
    loadNotes(true);
    document.getElementById("note-text").focus();
  }
  function saveNote() {
    var res = document.getElementById("note-result"), text = document.getElementById("note-text");
    var fields = ["note-title", "note-tags", "note-day"].map(function (id) { return document.getElementById(id); });
    S.chat("POST", "notes", { text: text.value, title: fields[0].value, tags: fields[1].value, meeting_on: fields[2].value || null,
                              scope: document.getElementById("note-scope").value }).then(function (r) {
      if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
      res.textContent = r.note.scope === "team" ? "saved for the team" : "saved for you";
      res.className = "result good";
      text.value = "";
      fields.forEach(function (f) { f.value = ""; });
      loadNotes(true);
    });
  }
  document.getElementById("open-notes").addEventListener("click", openNotes);
  document.getElementById("notes-close").addEventListener("click", function () { document.getElementById("notes-drawer").hidden = true; });
  document.getElementById("note-save").addEventListener("click", saveNote);
  document.getElementById("note-text").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); saveNote(); }   // Ctrl+Enter saves
  });
  document.getElementById("notes-more").addEventListener("click", function () { loadNotes(false); });
  document.getElementById("notes-search").addEventListener("input", function () {
    clearTimeout(notes.timer);
    var v = this.value.trim();
    notes.timer = setTimeout(function () { notes.q = v; loadNotes(true); }, 250);
  });

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
    found.addEventListener("click", function (ev) { if (ev.target.closest("a")) fold(false); });
    search.addEventListener("input", function () { if (search.value.trim()) fold(true); });
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
