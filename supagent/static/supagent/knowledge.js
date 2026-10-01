/* supagent data dictionary: what the team gives the agent and what waits for an admin. The review (To review),
   the catalog, the team memory, the documents and sites, the categories; for admins with their actions, for the
   others read only. A save answers at once; the agent's search follows in the background: the line next to the
   tabs says when it is up to date. */
(function () {
  "use strict";
  var S = window.supagent, el = S.el, D = S.dictionary;
  var $ = function (id) { return document.getElementById(id); };
  var admin = S.isAdmin;
  if (!D) return;

  function result(node, r, good) {
    node.textContent = r && r.error ? r.error : good;
    node.className = "result " + (r && r.error ? "bad" : "good");
  }
  function badge(text, cls) { return el("span", { class: "badge" + (cls ? " " + cls : ""), text: text }); }
  function sureButton(label, fn) {            // a second click confirms (no browser dialogs)
    return el("button", { type: "button", class: "linkish", text: label, onclick: function () {
      if (this.dataset.sure) { fn(); return; }
      this.dataset.sure = "1";
      this.textContent = label + ": sure?";
      var b = this;
      setTimeout(function () { delete b.dataset.sure; b.textContent = label; }, 4000);
    } });
  }

  // ------------------------------------------------------------------ the agent's search after a save
  var applyBox = $("apply-state"), applyTimer = null, applyUntil = 0;
  function applyShow(cls, text) {
    applyBox.hidden = false;
    applyBox.className = "apply-state " + cls;
    applyBox.textContent = text;
  }
  function pollApply() {
    S.admin("GET", "apply").then(function (st) {
      clearTimeout(applyTimer);
      if (st.error && st._status) { applyBox.hidden = true; return; }
      if (st.pending && !st.stale && Date.now() < applyUntil) {
        applyShow("busy", "Saved · updating the agent’s search…");
        applyTimer = setTimeout(pollApply, 1000);
        return;
      }
      if (st.pending) {
        applyShow("warn", "Still updating the agent’s search; if the server restarted, the hourly indexing catches up");
        return;
      }
      if (st.error) {
        applyShow("warn", "Saved, but the agent’s search was not updated (" + st.error + "); the hourly indexing catches up");
        return;
      }
      applyShow("ok", "Saved · the agent’s search is up to date");
      applyTimer = setTimeout(function () { applyBox.hidden = true; }, 6000);
    });
  }
  function saved() {
    if (!admin) return;
    applyUntil = Date.now() + 120000;
    applyShow("busy", "Saved · updating the agent’s search…");
    clearTimeout(applyTimer);
    applyTimer = setTimeout(pollApply, 600);
  }
  D.saved = saved;
  if (admin) {
    S.admin("GET", "apply").then(function (st) { if (st.pending && !st.error) { applyUntil = Date.now() + 120000; pollApply(); } });
  }

  // ------------------------------------------------------------------ To review (admins)
  var counts = {};
  function setWaiting(n) {
    var c = $("review-count");
    c.textContent = n > 999 ? "999+" : String(n);
    c.hidden = !n;
  }
  D.reviewWaiting = function () {
    return S.admin("GET", "review?limit=1").then(function (d) { setWaiting(d.waiting || 0); return d.waiting || 0; });
  };

  function card(main, meta, actions) {
    var acts = el("div", { class: "ractions" }, actions);
    var c = el("div", { class: "rcard" }, [el("div", { class: "rbody" }, main.concat([meta ? el("div", { class: "rmeta", text: meta }) : null])), acts]);
    c._acts = acts;
    return c;
  }
  /* an action on a card: the card shows what was done; the counts follow */
  function act(label, cls, group, fn) {
    return el("button", { type: "button", class: "btn small" + (cls ? " " + cls : ""), text: label, onclick: function () {
      var c = this.closest(".rcard"), buttons = c.querySelectorAll("button");
      buttons.forEach(function (b) { b.disabled = true; });
      Promise.resolve(fn(c)).then(function (r) {
        if (r && r.error) {
          buttons.forEach(function (b) { b.disabled = false; });
          var err = c.querySelector(".rerr") || c.querySelector(".rbody").appendChild(el("div", { class: "rerr result bad" }));
          err.textContent = r.error;
          return;
        }
        if (r && r.keep) { buttons.forEach(function (b) { b.disabled = false; }); return; }
        c.classList.add("done");
        c._acts.innerHTML = "";
        c._acts.appendChild(el("span", { class: "rdone", text: (r && r.done) || "done" }));
        done(group);
      });
    } });
  }
  function done(group, n) {                    // n items of a group were decided: the counts follow
    counts[group] = Math.max(0, (counts[group] || 0) - (n === undefined ? 1 : n));
    var c = $("rg-count-" + group);
    if (c) c.textContent = S.num(counts[group]);
    var waiting = ["memory", "recipes", "values", "tags", "links"].reduce(function (s, k) { return s + (counts[k] || 0); }, 0);
    setWaiting(waiting);
    summaryLine(waiting);
    saved();
  }

  var GROUPS = [
    ["memory", "Team memory proposed from the chats", "Used for everyone once approved. Correct the wording if needed."],
    ["recipes", "Answers marked Helpful", "Confirmed ones are proposed first for similar questions; rejected ones never."],
    ["values", "New categories proposed by the LLM", "A value is used once approved; merge it into an existing one when it is the same thing."],
    ["tags", "Categories given to items", "The LLM was not sure enough to give these alone."],
    ["links", "Relations between items", "What the LLM found related (explains, about, depends on)."],
    ["descriptions", "AI-written descriptions of the data", "Already used by the agent; approve or correct the ones you know."],
    ["routes", "Kinds of work learned from the chats", "Already used as examples by the router; set the right kind, or remove a wrong one."]
  ];
  var TITLES = {};
  GROUPS.forEach(function (g) { TITLES[g[0]] = g[1]; });

  function summaryLine(waiting) {
    var box = $("review-summary");
    box.innerHTML = "";
    if (!waiting && !(counts.descriptions || counts.routes)) {
      box.appendChild(el("p", { class: "lead", text: "Nothing waits for you. What the chats and the daily learning propose shows here, each with its actions." }));
      return;
    }
    box.appendChild(el("p", { class: "lead", text: waiting ? S.num(waiting) + " item" + (waiting > 1 ? "s wait" : " waits") + " for you. Nothing here is used by the agent until approved, except the two last groups, which it already uses." :
      "Nothing waits for you. The agent already uses the items below; check them when you have time." }));
    var chips = el("div", { class: "rchips" });
    GROUPS.forEach(function (g) {
      if (!counts[g[0]]) return;
      chips.appendChild(el("button", { type: "button", class: "chip", text: g[1] + " · " + S.num(counts[g[0]]), onclick: function () {
        var s = $("rg-" + g[0]);
        if (s) s.scrollIntoView({ behavior: "smooth", block: "start" });
      } }));
    });
    box.appendChild(chips);
  }

  function group(key, items, render, extra) {
    var g = GROUPS.filter(function (x) { return x[0] === key; })[0];
    if (!counts[key]) return null;
    var head = el("div", { class: "rgroup-head" }, [el("h2", { text: g[1] }), el("span", { class: "count", id: "rg-count-" + key, text: S.num(counts[key]) })]);
    if (extra && extra.bulk) head.appendChild(extra.bulk);
    var list = el("div", { class: "rlist" }, items.map(render));
    var sec = el("section", { class: "rgroup", id: "rg-" + key }, [head, el("p", { class: "muted", text: g[2] }), list]);
    if (extra && extra.fold && items.length > extra.fold) {          // a long optional group: the first ones
      var hidden = Array.prototype.slice.call(list.children, extra.fold);
      hidden.forEach(function (c) { c.hidden = true; });
      sec.appendChild(el("button", { type: "button", class: "btn small", text: "Show " + hidden.length + " more", onclick: function () {
        hidden.forEach(function (c) { c.hidden = false; });
        this.remove();
      } }));
    }
    if (counts[key] > items.length) {
      sec.appendChild(el("p", { class: "muted", text: "The first " + items.length + " of " + S.num(counts[key]) + (extra && extra.more ? "; " : ".") },
        extra && extra.more ? [extra.more] : []));
    }
    return sec;
  }

  function bulk(label, key, items, fn) {        // "Approve all shown": a second click confirms
    var box = el("span", { class: "rbulk" });
    var go = el("button", { type: "button", class: "btn small", text: label, onclick: function () {
      if (!go.dataset.sure) { go.dataset.sure = "1"; go.textContent = label + " (" + items.length + "): sure?"; return; }
      go.disabled = true;
      var cards = $("rg-" + key).querySelectorAll(".rcard");
      var chain = Promise.resolve(), n = 0;
      items.forEach(function (it, i) {
        var c = cards[i];
        if (!c || c.classList.contains("done")) return;           // already decided one by one
        chain = chain.then(function () { return fn(it); }).then(function (r) {
          if (r && r.error) return;
          c.classList.add("done"); c._acts.innerHTML = ""; c._acts.appendChild(el("span", { class: "rdone", text: "approved" }));
          n++;
        });
      });
      chain.then(function () { go.remove(); done(key, n); });
    } });
    box.appendChild(go);
    return box;
  }

  function reviewLoad() {
    var box = $("review");
    box.innerHTML = "";
    box.appendChild(el("p", { class: "muted", text: "Loading…" }));
    return S.admin("GET", "review?limit=50").then(function (d) {
      box.innerHTML = "";
      if (d.error) { box.appendChild(el("p", { class: "result bad", text: d.error })); return; }
      counts = Object.assign({}, d.counts || {});
      setWaiting(d.waiting || 0);
      summaryLine(d.waiting || 0);
      var parts = [
        group("memory", d.memory || [], function (m) {
          var text = el("div", { class: "rtext", text: m.text });
          return card([text], [m.kind, m.category, m.source === "chat" ? "from a chat" : "written by a user", S.when(m.created_at)].filter(Boolean).join(" · "), [
            act("Approve", "primary", "memory", function () { return S.admin("POST", "memory/" + m.id, { status: "active" }).then(function (r) { return r.error ? r : { done: "approved" }; }); }),
            el("button", { type: "button", class: "btn small", text: "Edit", onclick: function () {
              var c = this.closest(".rcard");
              if (c.querySelector("textarea")) return;
              var ta = el("textarea", { rows: "2", "aria-label": "Memory" });
              ta.value = m.text;
              text.replaceWith(ta);
              ta.focus();
              c._acts.insertBefore(act("Save and approve", "primary", "memory", function () {
                return S.admin("POST", "memory/" + m.id, { text: ta.value, status: "active" }).then(function (r) { return r.error ? r : { done: "corrected and approved" }; });
              }), c._acts.firstChild);
            } }),
            act("Reject", "", "memory", function () { return S.admin("POST", "memory/" + m.id, { status: "disabled" }).then(function (r) { return r.error ? r : { done: "rejected (kept, not used)" }; }); })
          ]);
        }),
        group("recipes", d.recipes || [], function (r) {
          return card([el("div", { class: "rtext", text: r.question }),
                       el("details", { class: "query" }, [el("summary", { text: (r.tool || "") + (r.target ? " · " + r.target : "") }), el("pre", { text: r.query || "" })])],
            "used " + S.num(r.uses || 1) + " time" + ((r.uses || 1) > 1 ? "s" : "") + " · " + S.when(r.created_at), [
              act("Confirm", "primary", "recipes", function () { return S.dict("POST", "recipes/" + r.id, { status: "confirmed" }).then(function (x) { return x.error ? x : { done: "confirmed" }; }); }),
              act("Reject", "", "recipes", function () { return S.dict("POST", "recipes/" + r.id, { status: "rejected" }).then(function (x) { return x.error ? x : { done: "rejected" }; }); })
            ]);
        }),
        group("values", d.values || [], function (v) {
          var name = el("div", { class: "rtext" }, [el("span", { class: "facet-chip", text: v.facet + ": " + v.value })]);
          var desc = v.description ? el("div", { class: "muted", text: v.description }) : null;
          return card([name, desc], S.num(v.items) + " item" + (v.items === 1 ? "" : "s") + " · proposed by the LLM", [
            act("Approve", "primary", "values", function () { return S.admin("POST", "facets/" + v.id, { status: "approved" }).then(function (r) { return r.error ? r : { done: "approved" }; }); }),
            el("button", { type: "button", class: "btn small", text: "Rename", onclick: function () {
              var c = this.closest(".rcard");
              if (c.querySelector("input.rename")) return;
              var inp = el("input", { type: "text", class: "rename", "aria-label": "New name" });
              inp.value = v.value;
              var ok = el("button", { type: "button", class: "btn small", text: "Save the name", onclick: function () {
                S.admin("POST", "facets/" + v.id, { value: inp.value }).then(function (r) {
                  if (r.error) return;
                  v.value = r.value;
                  name.firstChild.textContent = v.facet + ": " + v.value;
                  inp.remove(); ok.remove();
                });
              } });
              c.querySelector(".rbody").appendChild(el("div", { class: "actions" }, [inp, ok]));
              inp.focus();
            } }),
            el("button", { type: "button", class: "btn small", text: "Merge into…", onclick: function () {
              var c = this.closest(".rcard");
              if (c.querySelector("select.merge")) return;
              var sel = el("select", { class: "merge", "aria-label": "Merge into" }, [el("option", { value: "", text: "Loading…" })]);
              var line = el("div", { class: "actions" }, [sel]);
              c.querySelector(".rbody").appendChild(line);
              S.admin("GET", "facets?facet=" + encodeURIComponent(v.facet) + "&status=approved").then(function (f) {
                sel.innerHTML = "";
                sel.appendChild(el("option", { value: "", text: "Merge into…" }));
                (f.facets || []).forEach(function (x) { sel.appendChild(el("option", { value: x.id, text: x.value + " (" + x.items + ")" })); });
                line.appendChild(act("Merge", "primary", "values", function () {
                  if (!sel.value) return { keep: true };
                  return S.admin("POST", "facets/" + v.id, { merge_into: +sel.value }).then(function (r) { return r.error ? r : { done: "merged into " + sel.options[sel.selectedIndex].text }; });
                }));
              });
            } }),
            act("Reject", "", "values", function () { return S.admin("POST", "facets/" + v.id, { status: "rejected" }).then(function (r) { return r.error ? r : { done: "rejected" }; }); })
          ]);
        }),
        group("tags", d.tags || [], function (t) {
          return card([el("div", { class: "rtext" }, [document.createTextNode(t.title + " "), el("span", { class: "facet-chip", text: t.facet + ": " + t.value })])],
            t.ref + (t.confidence !== null && t.confidence !== undefined ? " · confidence " + Math.round(t.confidence * 100) + "%" : ""), [
              act("Approve", "primary", "tags", function () { return S.admin("POST", "tags/" + t.id, { status: "approved" }).then(function (r) { return r.error ? r : { done: "approved" }; }); }),
              act("Reject", "", "tags", function () { return S.admin("POST", "tags/" + t.id, { status: "rejected" }).then(function (r) { return r.error ? r : { done: "rejected" }; }); })
            ]);
        }, { bulk: (d.tags || []).length > 1 ? bulk("Approve all shown", "tags", d.tags, function (t) { return S.admin("POST", "tags/" + t.id, { status: "approved" }); }) : null }),
        group("links", d.links || [], function (x) {
          return card([el("div", { class: "rtext" }, [document.createTextNode(x.a_title + " "), el("span", { class: "link-kind", text: x.kind }),
                                                    document.createTextNode(" " + x.b_title)])],
            x.a + " → " + x.b + (x.confidence !== null && x.confidence !== undefined ? " · confidence " + Math.round(x.confidence * 100) + "%" : ""), [
              act("Approve", "primary", "links", function () { return S.admin("POST", "links/" + x.id, { status: "approved" }).then(function (r) { return r.error ? r : { done: "approved" }; }); }),
              act("Reject", "", "links", function () { return S.admin("POST", "links/" + x.id, { status: "rejected" }).then(function (r) { return r.error ? r : { done: "rejected" }; }); })
            ]);
        }, { bulk: (d.links || []).length > 1 ? bulk("Approve all shown", "links", d.links, function (x) { return S.admin("POST", "links/" + x.id, { status: "approved" }); }) : null }),
        (counts.descriptions || counts.routes) ? el("h2", { class: "section-title later", text: "When you have time (already used by the agent)" }) : null,
        group("descriptions", d.descriptions || [], function (o) {
          return card([el("div", { class: "rtext nm", text: (o.parent ? o.parent + " › " : "") + o.name }), el("div", { class: "muted", text: o.description })],
            o.kind + (o.database ? " \u00b7 " + o.database : "") + " · AI-written", [
              act("Approve", "primary", "descriptions", function () { return S.dict("POST", "objects/" + o.id, { approve: true }).then(function (r) { return r.error ? r : { done: "approved" }; }); }),
              el("button", { type: "button", class: "btn small", text: "Correct", onclick: function () { D.detail(o.id, reviewLoad); } })
            ]);
        }, { fold: 10, more: el("button", { type: "button", class: "linkish", text: "check them all in Data → Browse", onclick: function () { D.browseUnverified(null); } }) }),
        group("routes", d.routes || [], function (r) {
          var sel = el("select", { "aria-label": "Kind of work" }, ROUTES.map(function (k) { return el("option", { value: k, text: k, selected: k === r.route ? "selected" : null }); }));
          return card([el("div", { class: "rtext", text: r.question }), el("div", {}, [el("span", { class: "facet-chip", text: r.route })])],
            "routed by " + (r.by || "?") + " · marked " + (r.signal || "") + " · " + S.when(r.at), [
              sel,
              act("Set", "primary", "routes", function () { return S.admin("POST", "routes/" + r.id, { route: sel.value }).then(function (x) { return x.error ? x : { done: sel.value === r.route ? "kept: " + r.route : "set to " + sel.value }; }); }),
              act("Not an example", "", "routes", function () { return S.admin("POST", "routes/" + r.id, { remove: true }).then(function (x) { return x.error ? x : { done: "removed" }; }); })
            ]);
        }, { fold: 10 })
      ];
      parts.forEach(function (p) { if (p) box.appendChild(p); });
    });
  }
  var ROUTES = ["functional", "technical", "incident", "charts", "observability", "infrastructure"];

  // ------------------------------------------------------------------ Catalog
  var cat = { entries: [], classifications: {}, current: null, loaded: false };
  function catLoad(keep) {
    if (!admin) {
      return S.dict("GET", "knowledge").then(function (d) {
        cat.entries = (d.entries || []).map(function (e, i) {
          return { id: e.id || -1 - i, title: e.title, classification: e.classification, category: e.category, enabled: true,
                   updated_at: e.updated_at, updated_by: e.by, content: e.content, agent: e.by === "agent" };
        });
        catFilters(Object.keys(cat.entries.reduce(function (o, e) { o[e.classification] = 1; return o; }, {})),
                   Object.keys(cat.entries.reduce(function (o, e) { if (e.category) o[e.category] = 1; return o; }, {})));
        catRender();
      });
    }
    return S.admin("GET", "entries").then(function (d) {
      cat.entries = d.entries || [];
      cat.classifications = d.classifications || {};
      var ec = $("e-class"), dl = $("e-categories"), ve = ec.value;
      if (!ec.options.length) {          // built once: rebuilding it would reset an open editor's classification
        Object.keys(cat.classifications).forEach(function (k) { ec.appendChild(el("option", { value: k, text: k })); });
      } else if (ve) {
        ec.value = ve;
      }
      dl.innerHTML = "";
      (d.categories || []).forEach(function (c) { dl.appendChild(el("option", { value: c })); });
      catFilters(Object.keys(cat.classifications), d.categories || []);
      var w = $("cat-warnings");
      w.innerHTML = "";
      (d.errors || []).forEach(function (e) { w.appendChild(el("div", { class: "warn", text: "Skipped (invalid): “" + e.entry + "” — " + e.error })); });
      (d.conflicts || []).forEach(function (c) { w.appendChild(el("div", { class: "warn", text: "Defined twice: " + c.kind + " " + c.name + " in “" + c.entries.join("” and “") + "”; the most recent change is used (“" + c.kept + "”)" })); });
      catRender();
      if (keep && cat.current) { var e = cat.entries.filter(function (x) { return x.id === cat.current.id; })[0]; if (e) catOpen(e); }
    });
  }
  function catFilters(classes, categories) {
    var sc = $("cat-class"), sg = $("cat-category"), vc = sc.value, vg = sg.value;
    sc.innerHTML = '<option value="">All classifications</option>';
    classes.forEach(function (k) { sc.appendChild(el("option", { value: k, text: k })); });
    sg.innerHTML = '<option value="">All categories</option>';
    categories.forEach(function (c) { sg.appendChild(el("option", { value: c, text: c })); });
    sc.value = vc; sg.value = vg;
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
      var open = function () { catOpen(e); };
      tb.appendChild(el("tr", { class: "link", tabindex: "0", onclick: open, onkeydown: function (ev) { if (ev.key === "Enter") open(); } }, [
        title, el("td", {}, [badge(e.classification)]),
        el("td", { text: e.category || "" }),
        el("td", {}, [e.enabled ? badge("enabled", "ok") : badge("disabled", "off")]),
        el("td", { class: "muted when", text: S.when(e.updated_at) + (e.updated_by ? " · " + e.updated_by : "") + (e.version ? " · v" + e.version : "") })
      ]));
    });
    if (!rows.length) tb.appendChild(D.emptyRow(5, cat.entries.length ? "No entry matches." : admin ? "No entry yet: add one, or import a catalog (YAML)." : "No catalog entry yet."));
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
    if (!admin) {
      D.textDrawer(e.title, e.classification + (e.category ? " · " + e.category : "") + " · by " + (e.updated_by || "?"), e.content);
      return;
    }
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
                cat.current = r.entry; saved(); catLoad(true);
              });
            } })]), pre]));
      });
    });
  }
  D.openEntry = function (id) {
    var find = function () { return cat.entries.filter(function (x) { return x.id === id; })[0]; };
    var e = find();
    if (e) { catOpen(e); return; }
    catLoad(false).then(function () { var x = find(); if (x) catOpen(x); });
  };

  if (admin) {
    $("cat-export").href = document.body.dataset.adminApi + "catalog/export";
    $("e-history-box").addEventListener("toggle", function () { if ($("e-history-box").open) catHistory(); });
    $("e-class").addEventListener("change", function () {
      catHelp();
      if (!cat.current) $("e-fmt").value = ["rule", "note", "formula"].indexOf($("e-class").value) >= 0 ? "text" : "yaml";
    });
    $("e-save").addEventListener("click", function () {
      var body = { title: $("e-title").value, classification: $("e-class").value, category: $("e-category").value,
                   fmt: $("e-fmt").value, enabled: $("e-enabled").checked, content: $("e-content").value };
      if (cat.current) body.version = cat.current.version;
      var res = $("e-result"), btn = $("e-save");
      btn.disabled = true;
      res.textContent = "saving…"; res.className = "result";
      S.admin(cat.current ? "PUT" : "POST", cat.current ? "entries/" + cat.current.id : "entries", body).then(function (r) {
        btn.disabled = false;
        if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
        cat.current = r.entry;
        res.textContent = "saved (v" + r.entry.version + ")" + (r.applied && r.applied.curated ? ", applied to " + r.applied.curated + " objects" : "");
        res.className = "result good";
        $("entry-title-h").textContent = r.entry.title; $("e-delete").hidden = false; $("e-history-box").hidden = false;
        saved();
        catLoad(false);
      });
    });
    $("e-delete").addEventListener("click", function () { $("e-confirm").hidden = false; $("e-delete").hidden = true; });
    $("e-delete-no").addEventListener("click", function () { $("e-confirm").hidden = true; $("e-delete").hidden = false; });
    $("e-delete-yes").addEventListener("click", function () {
      S.admin("DELETE", "entries/" + cat.current.id + "?version=" + cat.current.version).then(function (r) {
        if (r.error) { $("e-result").textContent = r.error; $("e-result").className = "result bad"; return; }
        $("entry-drawer").hidden = true; cat.current = null; saved(); catLoad(false);
      });
    });
    $("entry-close").addEventListener("click", function () { $("entry-drawer").hidden = true; });
    $("cat-new").addEventListener("click", function () { catOpen(null); });
    $("cat-import").addEventListener("click", function () { $("import-result").textContent = ""; $("import-drawer").hidden = false; });
    $("import-close").addEventListener("click", function () { $("import-drawer").hidden = true; });
    $("import-go").addEventListener("click", function () {
      var res = $("import-result"), btn = $("import-go");
      btn.disabled = true;
      res.textContent = "importing…"; res.className = "result";
      S.admin("POST", "catalog", { content: $("import-text").value, mode: $("import-replace").checked ? "replace" : "merge" }).then(function (r) {
        btn.disabled = false;
        if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
        var i = r.imported || {};
        res.textContent = "added " + (i.added || 0) + ", updated " + (i.updated || 0) + ", unchanged " + (i.unchanged || 0) +
                          (i.deleted ? ", deleted " + i.deleted : "") + " entries";
        res.className = "result good";
        saved();
        catLoad(false);
      });
    });
  }
  ["cat-class", "cat-category", "cat-author"].forEach(function (id) { $(id).addEventListener("change", catRender); });
  $("cat-q").addEventListener("input", catRender);

  // ------------------------------------------------------------------ Team memory
  var memRows = [];
  function memRender() {
    var tb = $("team-memory").querySelector("tbody"), st = $("mem-status").value, q = $("mem-q").value.trim().toLowerCase();
    tb.innerHTML = "";
    var rows = memRows.filter(function (m) { return (!st || m.status === st) && (!q || (m.text + " " + (m.category || "")).toLowerCase().indexOf(q) >= 0); });
    rows.forEach(function (m) {
      var text = el("td", { text: m.text });
      var acts = el("td", { class: "row-actions" });
      if (admin) {
        var post = function (body) { return S.admin("POST", "memory/" + m.id, body).then(function (r) { if (!r.error) saved(); memLoad(); }); };
        if (m.status === "catalog") {
          acts.appendChild(el("span", { class: "muted", text: "edit it in the catalog" }));
        } else {
          if (m.status !== "active") acts.appendChild(el("button", { type: "button", class: "linkish", text: "Approve", onclick: function () { post({ status: "active" }); } }));
          if (m.status !== "disabled") acts.appendChild(el("button", { type: "button", class: "linkish", text: "Disable", onclick: function () { post({ status: "disabled" }); } }));
          acts.appendChild(el("button", { type: "button", class: "linkish", text: "Edit", onclick: function () {
            if (text.querySelector("textarea")) return;
            var ta = el("textarea", { rows: "2", "aria-label": "Memory" }); ta.value = m.text;
            text.innerHTML = ""; text.appendChild(ta);
            text.appendChild(el("div", { class: "actions" }, [el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () { post({ text: ta.value }); } }),
              el("button", { type: "button", class: "btn small", text: "Cancel", onclick: memRender })]));
            ta.focus();
          } }));
          acts.appendChild(sureButton("Delete", function () { S.chat("DELETE", "memory/" + m.id).then(function (r) { if (!r.error) saved(); memLoad(); }); }));
        }
      }
      tb.appendChild(el("tr", {}, [text, el("td", { text: m.kind + (m.category ? ", " + m.category : "") }),
        el("td", { text: (m.by || "") + (m.source === "chat" ? " (from a chat)" : "") }),
        el("td", {}, [badge(m.status === "catalog" ? "in the catalog" : m.status === "active" ? "used" : m.status,
                            m.status === "active" ? "ok" : m.status === "disabled" ? "off" : m.status === "catalog" ? "agent" : "warn")]),
        acts]));
    });
    if (!rows.length) tb.appendChild(D.emptyRow(5, memRows.length ? "No memory matches." : "No team memory yet."));
  }
  function memLoad() {
    if (!admin) {
      return S.dict("GET", "knowledge").then(function (d) {
        memRows = (d.team_memory || []).map(function (m) { return { text: m.text, kind: m.kind, category: m.category, status: "active", created_at: m.created_at }; });
        memRender();
      });
    }
    return S.admin("GET", "memory").then(function (d) { memRows = d.memory || []; memRender(); });
  }
  $("mem-status").addEventListener("change", memRender);
  $("mem-q").addEventListener("input", memRender);
  if (admin) {
    $("mem-add").addEventListener("click", function () {
      var res = $("mem-result"), btn = $("mem-add");
      if (!$("mem-text").value.trim()) { res.textContent = "write the memory first"; res.className = "result bad"; return; }
      btn.disabled = true;
      S.chat("POST", "memory", { text: $("mem-text").value, scope: "team", kind: $("mem-kind").value, category: $("mem-category").value }).then(function (r) {
        btn.disabled = false;
        result(res, r, "added for the team");
        if (!r.error) { $("mem-text").value = ""; saved(); memLoad(); }
      });
    });
  }

  // ------------------------------------------------------------------ Documents and sites
  function docsLoad() {
    var tb = $("docs").querySelector("tbody");
    if (!admin) {
      return S.dict("GET", "knowledge").then(function (d) {
        tb.innerHTML = "";
        (d.docs || []).forEach(function (x) {
          tb.appendChild(D.row(function () {
            D.textDrawer(x.title, (x.url || "uploaded file") + (x.category ? " · " + x.category : ""),
              x.excerpt + (x.chars > (x.excerpt || "").length ? "\n… (" + S.num(x.chars) + " characters in all)" : ""));
          }, [el("td", {}, [el("div", { text: x.title || x.url || "document" }), x.url ? el("div", { class: "muted nm", text: x.url }) : null]),
              el("td", { text: x.category || "" }), el("td", { class: "num", text: S.num(x.pages || (x.kind === "url" ? 0 : 1)) }),
              el("td", { class: "num", text: S.bytes(x.chars) }), el("td", {}, [badge(x.status || "", x.status === "ok" ? "ok" : x.status === "error" ? "bad" : "")]),
              el("td", { class: "muted", text: S.when(x.fetched_at) }), el("td", {})]));
        });
        if (!(d.docs || []).length) tb.appendChild(D.emptyRow(7, "No document or site yet."));
      });
    }
    return S.admin("GET", "docs").then(function (d) {
      tb.innerHTML = "";
      (d.docs || []).forEach(function (x) {
        var acts = el("td", { class: "row-actions" });
        if (x.kind === "url") acts.appendChild(el("button", { type: "button", class: "linkish", text: "Read again", onclick: function () {
          this.disabled = true;
          S.admin("POST", "docs/" + x.id + "/refresh", {}).then(function () { setTimeout(docsLoad, 3000); });
        } }));
        acts.appendChild(sureButton("Delete", function () { S.admin("DELETE", "docs/" + x.id).then(function (r) { if (!r.error) saved(); docsLoad(); }); }));
        tb.appendChild(el("tr", {}, [el("td", {}, [el("div", { text: x.title || x.url || "document" }),
            x.url ? el("div", { class: "muted nm", text: x.url }) : null]),
          el("td", { text: x.category || "" }), el("td", { class: "num", text: S.num((x.pages || []).length || (x.kind === "upload" ? 1 : 0)) }),
          el("td", { class: "num", text: S.bytes(x.chars) }),
          el("td", {}, [badge(x.status || "", x.status === "ok" ? "ok" : x.status === "error" ? "bad" : ""), x.error ? el("div", { class: "muted", text: x.error }) : null]),
          el("td", { class: "muted", text: x.fetched_at ? S.when(x.fetched_at) : "" }), acts]));
      });
      if (!(d.docs || []).length) tb.appendChild(D.emptyRow(7, "No document yet."));
    });
  }
  if (admin) {
    $("doc-add").addEventListener("click", function () {
      var res = $("doc-result"), btn = $("doc-add");
      btn.disabled = true;
      S.admin("POST", "docs", { url: $("doc-url").value, category: $("doc-category").value,
                                max_pages: +$("doc-pages").value, refresh_days: +$("doc-days").value }).then(function (r) {
        btn.disabled = false;
        result(res, r, "added: reading it now");
        if (!r.error) { $("doc-url").value = ""; saved(); setTimeout(docsLoad, 3000); }
        docsLoad();
      });
    });
    $("doc-file").addEventListener("change", function () {
      var f = this.files && this.files[0], res = $("doc-result");
      if (!f) return;
      if (f.size > 20 * 1024 * 1024) { res.textContent = "file too large (20 MB at most)"; res.className = "result bad"; return; }
      var reader = new FileReader();
      reader.onload = function () {
        S.admin("POST", "docs", { name: f.name, title: f.name, category: $("doc-category").value, content: String(reader.result) }).then(function (r) {
          result(res, r, "uploaded: " + f.name);
          if (!r.error) saved();
          docsLoad();
        });
      };
      reader.readAsText(f);
      this.value = "";
    });
  }

  // ------------------------------------------------------------------ Categories (admins)
  function facetsLoad() {
    var p = [];
    if ($("fac-facet").value) p.push("facet=" + encodeURIComponent($("fac-facet").value));
    if ($("fac-status").value) p.push("status=" + encodeURIComponent($("fac-status").value));
    if ($("fac-q").value.trim()) p.push("q=" + encodeURIComponent($("fac-q").value.trim()));
    return S.admin("GET", "facets" + (p.length ? "?" + p.join("&") : "")).then(function (d) {
      var tb = $("facets").querySelector("tbody");
      tb.innerHTML = "";
      var all = d.facets || [];
      all.forEach(function (f) {
        var post = function (body) { return S.admin("POST", "facets/" + f.id, body).then(function (r) { if (!r.error) saved(); facetsLoad(); return r; }); };
        var valueCell = el("td", {}, [el("div", { class: "facet-value", text: f.value }),
          f.description ? el("div", { class: "muted", text: f.description }) : null,
          (f.synonyms || []).length ? el("div", { class: "muted", text: "also: " + f.synonyms.join(", ") }) : null]);
        var acts = el("td", { class: "row-actions" });
        if (f.status !== "approved") acts.appendChild(el("button", { type: "button", class: "linkish", text: "Approve", onclick: function () { post({ status: "approved" }); } }));
        acts.appendChild(el("button", { type: "button", class: "linkish", text: "Edit", onclick: function () {
          if (valueCell.querySelector("input")) return;
          var v = el("input", { type: "text", "aria-label": "Value" }); v.value = f.value;
          var ds = el("input", { type: "text", "aria-label": "Description", placeholder: "what it covers" }); ds.value = f.description || "";
          var sy = el("input", { type: "text", "aria-label": "Also called", placeholder: "also called (comma separated)" }); sy.value = (f.synonyms || []).join(", ");
          valueCell.innerHTML = "";
          valueCell.appendChild(el("div", { class: "facet-edit" }, [v, ds, sy, el("div", { class: "actions" }, [
            el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () { post({ value: v.value, description: ds.value, synonyms: sy.value }); } }),
            el("button", { type: "button", class: "btn small", text: "Cancel", onclick: facetsLoad })])]));
          v.focus();
        } }));
        var same = all.filter(function (x) { return x.facet === f.facet && x.id !== f.id && x.status === "approved"; });
        if (same.length) {
          acts.appendChild(el("button", { type: "button", class: "linkish", text: "Merge…", onclick: function () {
            if (acts.querySelector("select")) return;
            var sel = el("select", { "aria-label": "Merge into" }, [el("option", { value: "", text: "into…" })].concat(
              same.map(function (x) { return el("option", { value: x.id, text: x.value }); })));
            acts.appendChild(sel);
            sel.addEventListener("change", function () { if (sel.value) post({ merge_into: +sel.value }); });
          } }));
        }
        if (f.source !== "seed") acts.appendChild(sureButton(f.status === "approved" ? "Retire" : "Reject", function () { post({ status: "rejected" }); }));
        tb.appendChild(el("tr", {}, [valueCell, el("td", { text: f.facet }), el("td", { class: "num", text: S.num(f.items) }),
          el("td", { class: "muted", text: (f.examples || []).join(" · ") }),
          el("td", {}, [badge(f.status === "approved" ? "used" : "proposed", f.status === "approved" ? "ok" : "warn"),
                        el("div", { class: "muted", text: { seed: "from the start", data: "from the data", llm: "by the LLM", admin: "by an admin" }[f.source] || f.source || "" })]),
          acts]));
      });
      if (!all.length) tb.appendChild(D.emptyRow(6, "No category yet: the daily learning classifies the knowledge (or: superset supagent classify)."));
    });
  }
  var facTimer = null;
  ["fac-facet", "fac-status"].forEach(function (id) { $(id).addEventListener("change", facetsLoad); });
  $("fac-q").addEventListener("input", function () { clearTimeout(facTimer); facTimer = setTimeout(facetsLoad, 250); });

  // ------------------------------------------------------------------ the tabs of this file
  D.register("review", reviewLoad);
  D.register("catalog", function () { catLoad(false); });
  D.register("memory", memLoad);
  D.register("docs", docsLoad);
  D.register("categories", facetsLoad);
})();
