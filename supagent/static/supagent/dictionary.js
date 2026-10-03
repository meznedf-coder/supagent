/* supagent data dictionary page: the tabs (one controller, deep links #catalog, #review, ...), the data learned
   (browse, relations, changes), the Context, what the agent learned, the search. The knowledge editors and the
   review are in knowledge.js, which registers its tabs here. */
(function () {
  "use strict";
  var S = window.supagent, el = S.el, esc = S.esc;
  var state = { page: 0, size: 50, total: 0, admin: S.isAdmin };
  var $ = function (id) { return document.getElementById(id); };
  var KIND = { metric: "metric", label: "label", family: "family", index: "index", field: "field" };

  // ------------------------------------------------------------------ tabs: one controller for the page
  var MAIN = ["review", "knowledge", "data", "search"];
  var SUBS = { knowledge: ["catalog", "memory", "docs", "notes", "context", "learned", "categories", "map"],
               data: ["browse", "relations", "changes"] };
  var ADMIN_ONLY = ["review", "categories"];
  var loaders = {}, current = {}, active = null;

  function mainOf(name) {
    if (MAIN.indexOf(name) >= 0) return name;
    return Object.keys(SUBS).filter(function (k) { return SUBS[k].indexOf(name) >= 0; })[0] || null;
  }
  function allowed(name) { return !!mainOf(name) && (state.admin || ADMIN_ONLY.indexOf(name) < 0); }

  function show(name, quiet) {
    var redirected = !allowed(name);              // an admin's tab asked by someone else: the catalog, said so
    if (redirected) name = "catalog";
    var main = mainOf(name);
    var sub = SUBS[main] ? (SUBS[main].indexOf(name) >= 0 ? name : current[main] || SUBS[main][0]) : null;
    if (sub && !allowed(sub)) sub = SUBS[main][0];
    MAIN.forEach(function (t) { $("tab-" + t).hidden = t !== main; });
    document.querySelectorAll(".maintabs button[data-tab]").forEach(function (b) {
      var on = b.dataset.tab === main;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (SUBS[main]) {
      SUBS[main].forEach(function (s) { $("sub-" + s).hidden = s !== sub; });
      $("tab-" + main).querySelectorAll(".subnav button").forEach(function (b) { b.classList.toggle("on", b.dataset.sub === sub); });
      current[main] = sub;
    }
    active = sub || main;
    if (!quiet || redirected) {
      try { history.replaceState(null, "", "#" + active); } catch (e) { /* not in a browser that allows it */ }
    }
    if (loaders[active]) loaders[active]();
  }

  document.querySelectorAll(".maintabs button[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.dataset.tab); });
  });
  document.querySelectorAll(".subnav button[data-sub]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.dataset.sub); });
  });
  if (!state.admin) document.querySelectorAll(".admin-only").forEach(function (x) { x.hidden = true; });

  /* a "Copy" link; where the browser blocks the clipboard (plain http), the text is selected */
  function copyLink(label, getText) {
    return el("button", { type: "button", class: "linkish", text: label, onclick: function (ev) {
      var btn = ev.currentTarget, text = getText();
      var done = function () { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = label; }, 1500); };
      var fallback = function () {
        var ta = el("textarea", {});
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { if (document.execCommand("copy")) done(); } catch (e) { /* selected: Ctrl+C */ }
        ta.remove();
      };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
      else fallback();
    } });
  }

  function summary() {
    return S.dict("GET", "summary").then(function (data) {
      var box = $("sources");
      box.innerHTML = "";
      var sel = $("f-source");
      (data.sources || []).forEach(function (s) {
        var c = s.counts || {};
        var parts = [];
        if (s.backend === "promagg") parts = [["metrics", c.metric], ["labels", c.label], ["families", c.family]];
        else parts = [["indices", c.index], ["fields", c.field]];
        var card = el("div", { class: "source" }, [
          el("div", { class: "name", text: s.database }),
          el("div", { class: "muted", text: (s.backend === "promagg" ? "Prometheus / Mimir metrics" : s.backend === "osagg" ? "OpenSearch indices" : s.backend) +
                                             (s.last_learned_at ? " · learned " + S.when(s.last_learned_at) : " · not learned yet") }),
          el("div", { class: "counts" }, parts.filter(function (p) { return p[1]; }).map(function (p) {
            return el("span", { text: S.num(p[1]) + " " + p[0] });
          }).concat([el("span", { text: S.num(s.described) + " described" }),
                     s.unverified ? el("button", { type: "button", class: "linkish", html: S.num(s.unverified) + ' <span class="badge llm">AI-written</span>',
                       title: "Browse them in Data, to correct the ones that matter", onclick: function () { browseUnverified(s.id); } }) : null]))
        ]);
        box.appendChild(card);
        if (!sel.querySelector('option[value="' + s.id + '"]')) sel.appendChild(el("option", { value: s.id, text: s.database }));
      });
      if (!(data.sources || []).length) {
        box.appendChild(el("div", { class: "source muted", text: "Nothing learned yet for the databases you may query. An admin can start a learning run in Settings → Chat settings." }));
      }
      var src = data.sources || [], learned = src.filter(function (s) { return s.last_learned_at; }).length;
      var described = src.reduce(function (n, s) { return n + (s.described || 0); }, 0);
      var unverified = src.reduce(function (n, s) { return n + (s.unverified || 0); }, 0);
      $("sources-line").textContent = src.length ? S.num(src.length) + " database" + (src.length > 1 ? "s" : "") + " you may query · " +
        S.num(learned) + " learned · " + S.num(described) + " objects described" +
        (unverified ? " · " + S.num(unverified) + " of them AI-written" : "") : "No database learned yet";
      if (!learned) $("sources-box").open = true;
    });
  }

  // ------------------------------------------------------------------ Data: browse
  function query() {
    var p = ["page=" + state.page, "size=" + state.size];
    [["source", "f-source"], ["kind", "f-kind"], ["show", "f-show"], ["q", "f-q"]].forEach(function (x) {
      var v = $(x[1]).value.trim();
      if (v) p.push(x[0] + "=" + encodeURIComponent(v));
    });
    return p.join("&");
  }

  function nameCell(o) {
    return el("td", { class: "nm" }, [o.parent ? el("span", { class: "parent", text: o.parent + " › " }) : null,
                                      document.createTextNode(o.name)]);
  }

  function size(o) {
    if (o.series !== null && o.series !== undefined) return S.num(o.series) + " series";
    if (o.docs !== null && o.docs !== undefined) return S.num(o.docs) + " docs";
    if (o.cardinality !== null && o.cardinality !== undefined) return S.num(o.cardinality) + " values";
    return "";
  }

  function load() {
    return S.dict("GET", "objects?" + query()).then(function (data) {
      var tb = $("objects").querySelector("tbody");
      tb.innerHTML = "";
      state.total = data.total || 0;
      (data.objects || []).forEach(function (o) {
        var desc = el("td", { class: "desc" });
        desc.innerHTML = (o.description ? esc(o.description.length > 220 ? o.description.slice(0, 220) + "…" : o.description) + " " : '<span class="muted">no description</span> ') +
                         S.sourceBadge(o.description_source, o.verified) + (o.gone ? ' <span class="badge gone">gone</span>' : "");
        tb.appendChild(el("tr", { class: "link", tabindex: "0", onclick: function () { detail(o.id); },
                                  onkeydown: function (ev) { if (ev.key === "Enter") detail(o.id); } }, [
          nameCell(o), el("td", { text: KIND[o.kind] || o.kind }),
          el("td", { text: o.metric_type || o.data_type || "" }), el("td", { text: o.unit || "" }), desc,
          el("td", { class: "num", text: size(o) })
        ]));
      });
      if (!(data.objects || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "Nothing matches." })]));
      var first = state.total ? state.page * state.size + 1 : 0;
      $("page-info").textContent = first + "–" + Math.min(state.total, (state.page + 1) * state.size) + " of " + S.num(state.total);
      $("prev").disabled = state.page === 0;
      $("next").disabled = (state.page + 1) * state.size >= state.total;
    });
  }

  function kv(obj, keys) {
    var dl = el("dl", { class: "kv" });
    keys.forEach(function (k) {
      var v = obj[k[0]];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
      if (Array.isArray(v)) v = v.map(function (x) { return Array.isArray(x) ? x.join(": ") : x; }).join(", ");
      else if (typeof v === "object") v = JSON.stringify(v);
      else if (typeof v === "number") v = S.num(v);
      else if (/^(first_seen|last_seen)$/.test(k[0])) v = S.when(v);
      dl.appendChild(el("dt", { text: k[1] }));
      dl.appendChild(el("dd", { text: String(v) }));
    });
    return dl;
  }

  var STAT_KEYS = [["series", "Series"], ["docs", "Documents"], ["data_from", "Data from"], ["data_to", "Data to"],
    ["time_field", "Time field"], ["time_range", "Time range"], ["window", "Statistics over"], ["rate_total", "Rate, all series (/s)"],
    ["rate_max_series", "Rate, busiest series (/s)"], ["min", "Min"], ["max", "Max"], ["avg", "Average"], ["p50", "p50"], ["p95", "p95"],
    ["cardinality", "Distinct values"], ["filled_pct", "Filled in (% of documents)"], ["values", "Values"], ["sample", "Sample values"],
    ["top", "Most frequent"], ["labels", "Labels"], ["parts", "Parts"], ["sampled_docs", "Documents sampled"], ["note", "Note"],
    ["profiled_at", "Profiled"]];

  function openDrawer() {
    $("drawer").hidden = false;
    $("d-close").focus();
  }

  /* an object of the dictionary in the side panel; after: called when an admin saved or approved it */
  function detail(id, after) {
    return S.dict("GET", "objects/" + id).then(function (o) {
      if (o.error) return;
      var b = $("d-body");
      b.innerHTML = "";
      b.appendChild(el("div", { class: "muted", text: (KIND[o.kind] || o.kind) + " · " + (o.database || "") }));
      b.appendChild(el("h2", { id: "d-title", class: "nm", text: (o.parent ? o.parent + " › " : "") + o.name }));
      var d = el("p", {});
      d.innerHTML = (o.description ? esc(o.description) : '<span class="muted">No description yet.</span>') + " " + S.sourceBadge(o.description_source, o.verified) +
                    (o.gone ? ' <span class="badge gone">gone since ' + esc(S.when(o.gone_at)) + "</span>" : "");
      b.appendChild(d);
      b.appendChild(kv(o, [["metric_type", "Metric type"], ["data_type", "Type"], ["unit", "Unit"], ["category", "Category"],
                           ["synonyms", "Also called"], ["backend_help", "HELP text of the source"], ["first_seen", "First seen"],
                           ["last_seen", "Last seen"]]));
      if (state.admin) b.appendChild(editor(o, after));
      var st = o.stats || {};
      if (Object.keys(st).length) {
        b.appendChild(el("div", { class: "section" }, [el("h3", { text: "Measured" }), kv(st, STAT_KEYS)]));
      }
      if (o.children && o.children.length) {
        var pl = el("div", { class: "pill-list" });
        o.children.forEach(function (c) {
          pl.appendChild(el("button", { type: "button", class: "pill", text: c.name, title: c.description || "", onclick: function () { detail(c.id); } }));
        });
        b.appendChild(el("div", { class: "section" }, [el("h3", { text: o.kind === "metric" ? "Labels" : "Fields" }), pl]));
      }
      if (o.also_on && o.also_on.length) {
        b.appendChild(el("div", { class: "section" }, [el("h3", { text: "The same label is on " + o.also_on.length + " other metric(s)" }),
          el("div", { class: "nm", text: o.also_on.slice(0, 60).join(", ") + (o.also_on.length > 60 ? " …" : "") })]));
      }
      if (o.relations && o.relations.length) {
        var rs = el("div", { class: "section" }, [el("h3", { text: "Relations" })]);
        o.relations.forEach(function (r) {
          var row = el("div", { class: "rel " + (r.origin === "curated" ? "" : "learned") });
          row.appendChild(el("div", { class: "nm", text: r.text }));
          row.appendChild(el("div", { class: "muted", text: (r.origin === "curated" ? "written in the catalog" : r.relation === "family_part" ? "same metric family" : "measured on the data") +
                                                            (r.confidence !== null && r.confidence !== undefined ? " · confidence " + Math.round(r.confidence * 100) + "%" : "") }));
          row.appendChild(el("button", { type: "button", class: "btn small", text: "Open " + (r.other.parent ? r.other.parent + " › " : "") + r.other.name,
                                         onclick: function () { detail(r.other.id); } }));
          rs.appendChild(row);
        });
        b.appendChild(rs);
      }
      if (o.changes && o.changes.length) {
        var cs = el("div", { class: "section" }, [el("h3", { text: "History" })]);
        o.changes.forEach(function (c) {
          cs.appendChild(el("div", { text: S.when(c.at) + " — " + c.change + (c.detail && Object.keys(c.detail).length ? ": " + JSON.stringify(c.detail) : "") }));
        });
        b.appendChild(cs);
      }
      openDrawer();
    });
  }

  function editor(o, after) {
    var ta = el("textarea", { rows: "3", "aria-label": "Description" });
    ta.value = o.description || "";
    var cat = el("input", { type: "text", "aria-label": "Category", placeholder: "category" });
    cat.value = o.category || "";
    var unit = el("input", { type: "text", "aria-label": "Unit", placeholder: "unit" });
    unit.value = o.unit || "";
    var syn = el("input", { type: "text", "aria-label": "Also called", placeholder: "also called (comma separated)" });
    syn.value = (o.synonyms || []).join(", ");
    var res = el("span", { class: "result" });
    var done = function (r) {
      res.textContent = r.error || "saved";
      res.className = "result " + (r.error ? "bad" : "good");
      if (!r.error) {
        if (D.saved) D.saved();
        detail(o.id, after);
        if (active === "browse") load();
        if (after) after();
      }
    };
    var save = el("button", { type: "button", class: "btn primary small", text: "Save as curated", onclick: function () {
      S.dict("POST", "objects/" + o.id, { description: ta.value, category: cat.value, unit: unit.value, synonyms: syn.value }).then(done);
    } });
    var kids = [el("h3", { text: "Edit (admins)" }), ta, el("div", { class: "actions" }, [cat, unit]), syn, el("div", { class: "actions" }, [save])];
    if (o.description_source === "llm" && !o.verified) {
      kids[kids.length - 1].appendChild(el("button", { type: "button", class: "btn small", text: "Approve the AI text", onclick: function () {
        S.dict("POST", "objects/" + o.id, { approve: true }).then(done);
      } }));
    }
    kids[kids.length - 1].appendChild(res);
    return el("div", { class: "edit section" }, kids);
  }

  // ------------------------------------------------------------------ Data: relations, changes
  var pages = { relations: { page: 0, size: 50, total: 0 }, changes: { page: 0, size: 50, total: 0 },
                timings: { page: 0, size: 50, total: 0 } };
  function paged(name) { var p = pages[name]; return "page=" + p.page + "&size=" + p.size; }

  function relations() {
    S.dict("GET", "relations?" + paged("relations")).then(function (data) {
      pages.relations.total = data.total || 0;
      S.pager($("rel-pager"), pages.relations, relations);
      var tb = $("relations").querySelector("tbody");
      tb.innerHTML = "";
      (data.relations || []).forEach(function (r) {
        var actions = el("td", {});
        if (data.is_admin && r.origin !== "curated") {
          actions.appendChild(el("button", { type: "button", class: "linkish", text: r.rejected ? "Restore" : "Wrong",
            title: r.rejected ? "Measure and use it again" : "Never measure it again, never give it to the agent",
            onclick: function (ev) {
              ev.stopPropagation();
              S.dict("POST", "relations/" + r.id, { rejected: !r.rejected }).then(relations);
            } }));
        }
        var origin = r.origin === "curated" ? "catalog" : "measured";
        tb.appendChild(el("tr", { class: "link" + (r.rejected ? " muted" : ""), onclick: function () { detail(r.a.id); } }, [
          el("td", { class: "nm", text: r.text }),
          el("td", { html: S.esc(origin) + (r.rejected ? ' <span class="badge bad">wrong' +
                                                         (r.rejected_by ? ", " + S.esc(r.rejected_by) : "") + "</span>" : "") }),
          el("td", { class: "num", text: r.confidence !== null && r.confidence !== undefined ? Math.round(r.confidence * 100) + "%" : "" }),
          actions
        ]));
      });
      if (!(data.relations || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "4", class: "muted", text: "No relation found yet." })]));
    });
  }

  function changes() {
    S.dict("GET", "changes?days=" + $("c-days").value + "&" + paged("changes")).then(function (data) {
      pages.changes.total = data.total || 0;
      S.pager($("c-pager"), pages.changes, changes);
      var tb = $("changes").querySelector("tbody");
      tb.innerHTML = "";
      (data.changes || []).forEach(function (c) {
        tb.appendChild(el("tr", {}, [el("td", { text: c.at }), el("td", { text: c.change }),
          el("td", { class: "nm", text: (c.parent ? c.parent + " › " : "") + c.name + " (" + c.kind + ")" }),
          el("td", { text: c.database }), el("td", { class: "nm", text: c.detail && Object.keys(c.detail).length ? JSON.stringify(c.detail) : "" })]));
      });
      if (!(data.changes || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "5", class: "muted", text: "No change in this period." })]));
    });
  }

  /* a text in the side panel: a catalog entry, a document, an agent's entry with its evidence */
  function textDrawer(title, meta, text, extra) {
    var b = $("d-body");
    b.innerHTML = "";
    if (meta) b.appendChild(el("div", { class: "muted", text: meta }));
    b.appendChild(el("h2", { id: "d-title", text: title }));
    b.appendChild(el("pre", { class: "entry-text", text: text || "" }));
    (extra || []).forEach(function (x) { if (x) b.appendChild(x); });
    openDrawer();
  }
  function emptyRow(cols, text) { return el("tr", {}, [el("td", { colspan: String(cols), class: "muted", text: text })]); }
  function row(onclick, cells) {
    return el("tr", { class: "clickable", tabindex: "0", onclick: onclick,
      onkeydown: function (ev) { if (ev.key === "Enter") onclick(); } }, cells);
  }

  var browsed = false;
  function browseUnverified(sourceId) {             // the AI descriptions (of a database), to verify
    $("f-source").value = sourceId ? String(sourceId) : "";
    $("f-show").value = "unverified";
    state.page = 0;
    browsed = false;
    show("browse");
  }

  // ------------------------------------------------------------------ Learned by the agent
  function agentKnowledge() {
    return S.dict("GET", "agent_knowledge").then(function (d) {
      var box = $("agent-summary");
      box.innerHTML = "";
      box.appendChild(el("span", { class: "muted", text: S.num(d.relations_measured) + " relations measured" }));
      (d.to_verify || []).forEach(function (v) {
        box.appendChild(document.createTextNode(" · "));
        box.appendChild(el("button", { type: "button", class: "linkish", text: S.num(v.count) + " AI-written descriptions in " + v.database,
          onclick: function () { browseUnverified(v.source_id); } }));
      });
      var te = $("agent-entries").querySelector("tbody");
      te.innerHTML = "";
      (d.entries || []).forEach(function (e) {
        te.appendChild(row(function () {
          if (state.admin && D.openEntry) { D.openEntry(e.id); return; }
          textDrawer(e.title, e.classification + " · written by the agent" + (e.enabled ? "" : " · disabled"), e.content,
            [el("h3", { text: "Evidence" }), el("pre", { class: "entry-text", text: JSON.stringify(e.evidence || {}, null, 2) })]);
        }, [el("td", {}, [el("div", { class: "nm", text: e.title }), el("div", { class: "use-why", text: e.why || "" })]),
            el("td", { text: e.classification }),
            el("td", { class: "nm", text: e.origin || "" }), el("td", { text: S.when(e.updated_at) })]));
      });
      if (!(d.entries || []).length) te.appendChild(emptyRow(4, "No catalog entry written by the agent yet."));
    });
  }

  // where the data of the questions was: one row per table, the words that led there, paged
  pages.where = { page: 0, size: 25, total: 0 };
  var whereTimer = null, whereDbs = false;
  function whereData() {
    var st = pages.where, p = ["limit=" + st.size, "offset=" + st.page * st.size];
    if ($("w-q").value.trim()) p.push("q=" + encodeURIComponent($("w-q").value.trim()));
    if ($("w-db").value) p.push("database=" + encodeURIComponent($("w-db").value));
    return S.dict("GET", "where_data?" + p.join("&")).then(function (d) {
      var tb = $("associations").querySelector("tbody");
      tb.innerHTML = "";
      st.total = d.total || 0;
      if (!whereDbs) {
        whereDbs = true;
        (d.databases || []).forEach(function (x) {
          $("w-db").appendChild(el("option", { value: x.id, text: x.name }));
          $("w-add-db").appendChild(el("option", { value: x.id, text: x.name }));
        });
      }
      (d.tables || []).forEach(function (t) {
        var words = el("td", { class: "where-words" });
        var ordered = t.words.filter(function (w) { return !w.elsewhere; })      // the words that say this table
          .concat(t.words.filter(function (w) { return w.elsewhere; }));          // first, the shared ones after
        ordered.forEach(function (w) {
          var tip = (w.elsewhere ? "Also led to " + w.elsewhere + " other table" + (w.elsewhere > 1 ? "s" : "") +
            (w.other_databases.length ? " (other databases: " + w.other_databases.join(", ") + ")" : "") +
            ": alone, it does not say where the data is." : "Led only here.") + " " + S.num(w.uses) + " answer use" +
            (w.uses > 1 ? "s" : "") + ".";
          var chip = el("span", { class: "where-word" + (w.elsewhere ? " shared" : " own"), title: tip }, [
            el(w.elsewhere ? "span" : "strong", { class: "nm", text: w.word }),
            el("span", { class: "where-uses", text: S.num(w.uses) })]);
          if (w.manual) {
            chip.classList.add("manual");
            chip.title = "Added by hand" + (w.added_by ? " by " + w.added_by : "") + ": never fades. " + tip;
          }
          if (d.is_admin) chip.appendChild(el("button", { type: "button", class: "where-x", text: "\u00d7",
            title: "Wrong: \u201c" + w.word + "\u201d does not lead to " + t.name, "aria-label": "Wrong: " + w.word,
            onclick: function (ev) {
              S.confirm(ev.currentTarget, "Remove \u201c" + w.word + "\u201d from " + t.name + "?", {
                yes: "Remove", detail: "The agent no longer goes there for this word; new answers may teach it again." +
                  (w.manual ? " It was added by hand." : "") }).then(function (ok) {
                if (!ok) return;
                S.dict("POST", "where_data/forget", { word: w.word, database_id: t.database_id, kind: t.kind,
                                                     parent: t.parent, name: t.name }).then(whereData);
              });
            } }));
          words.appendChild(chip);
        });
        tb.appendChild(el("tr", {}, [
          el("td", { class: "nm", text: (t.parent ? t.parent + " \u203a " : "") + t.name + " (" + t.kind + ")" }),
          el("td", { text: t.database }), words,
          el("td", { class: "num", text: S.num(t.answers) + (t.helpful ? " (" + S.num(t.helpful) + " Helpful)" : "") }),
          el("td", { text: S.when(t.updated_at) })]));
      });
      if (!(d.tables || []).length) tb.appendChild(emptyRow(5, $("w-q").value.trim() || $("w-db").value ?
        "Nothing for this search." : "Nothing yet: it comes from the answers of the chat."));
      S.pager($("w-pager"), st, whereData);
    });
  }
  $("w-q").addEventListener("input", function () {
    clearTimeout(whereTimer);
    whereTimer = setTimeout(function () { pages.where.page = 0; whereData(); }, 250);
  });
  $("w-db").addEventListener("change", function () { pages.where.page = 0; whereData(); });
  /* an admin puts words on a table by hand: the indices and metrics of the database chosen, as suggestions */
  var tablesOf = {};
  $("w-add-db").addEventListener("change", function () {
    var dbid = $("w-add-db").value, list = $("w-add-tables");
    list.innerHTML = "";
    if (!dbid) return;
    var fill = function (names) { names.forEach(function (n) { list.appendChild(el("option", { value: n })); }); };
    if (tablesOf[dbid]) { fill(tablesOf[dbid]); return; }
    S.dict("GET", "where_data/tables?database=" + encodeURIComponent(dbid)).then(function (r) {
      tablesOf[dbid] = (r.tables || []).map(function (x) { return x.name; });
      fill(tablesOf[dbid]);
    });
  });
  $("w-add-go").addEventListener("click", function () {
    var res = $("w-add-result"), btn = $("w-add-go");
    var body = { words: $("w-add-words").value, database_id: +$("w-add-db").value || null, name: $("w-add-table").value.trim() };
    if (!body.words.trim() || !body.database_id || !body.name) {
      res.textContent = "write the words, choose the database and the index or metric"; res.className = "result bad"; return;
    }
    btn.disabled = true;
    S.dict("POST", "where_data/add", body).then(function (r) {
      btn.disabled = false;
      res.textContent = r.error || ("added: " + (r.words || []).join(", ") + " \u2192 " + r.name);
      res.className = "result " + (r.error ? "bad" : "good");
      if (!r.error) { $("w-add-words").value = ""; pages.where.page = 0; whereData(); }
    });
  });

  /* a learned answer in edit (admins): its generic question, its query (checked on the data before it is
     confirmed) and its path (the data it uses, its steps); `after`: called once saved */
  function recipeEditor(r, box, after) {
    var q = el("textarea", { rows: "2", class: "recipe-q", "aria-label": "The generic question", maxlength: "2000" });
    q.value = r.question || "";
    var code = el("textarea", { rows: Math.min(14, Math.max(4, (r.query || "").split("\n").length + 1)), class: "recipe-query",
                                spellcheck: "false", "aria-label": "The query" });
    code.value = r.query || "";
    var noQuery = r.tool === "investigation" || r.tool === "path";      // a path: read, not run
    /* the path of a learned answer: the data it uses and its steps, as the answer did them or as an admin wrote them */
    var path = noQuery ? null : el("textarea", { rows: Math.min(10, Math.max(3, (r.path || "").split("\n").length + 1)),
      class: "recipe-path", spellcheck: "false", "aria-label": "Its path: the data it uses, its steps" });
    if (path) path.value = r.path || "";
    var res = el("div", { class: "result", role: "status", "aria-live": "polite" });
    var checked = null;                                    // the query text that passed the check
    var show = function (x) {
      res.innerHTML = "";
      if (x.error) { res.className = "result bad"; res.textContent = x.error; return; }
      res.className = "result good";
      res.appendChild(document.createTextNode(x.ok ? "The query runs: " + S.num(x.rows) + " row" + (x.rows === 1 ? "" : "s") +
        (x.seconds !== undefined ? " in " + S.num(x.seconds) + " s" : "") + (x.columns && x.columns.length ? " (" + x.columns.join(", ") + ")" : "") :
        (x.note || "checked")));
      if (x.sample && x.sample.length) res.appendChild(el("pre", { class: "recipe-sample", text: x.sample.map(function (row) {
        return row.map(function (v) { return v === null ? "" : String(v); }).join(" | "); }).join("\n") }));
    };
    var check = el("button", { type: "button", class: "btn small", text: "Check the query", onclick: function () {
      check.disabled = true;
      res.className = "result"; res.textContent = "running it with your permissions…";
      S.dict("POST", "recipes/" + r.id + "/check", { query: code.value }).then(function (x) {
        check.disabled = false;
        if (!x.error) checked = code.value;
        show(x);
      });
    } });
    var save = function (confirmIt) {
      var body = { question: q.value, query: code.value };
      if (path) body.path = path.value;
      if (confirmIt) body.status = "confirmed";
      res.className = "result"; res.textContent = "saving…";
      return S.dict("POST", "recipes/" + r.id, body).then(function (x) {
        if (x.error) { show(x); return; }
        if (D.saved) D.saved();
        after();
      });
    };
    box.innerHTML = "";
    box.appendChild(el("div", { class: "recipe-edit" }, [
      el("label", { class: "field" }, [el("span", { text: "Question (generic: no one-off date, id or number)" }), q]),
      el("label", { class: "field" }, [el("span", { text: (r.tool === "investigation" ? "The investigation path" : r.tool === "path" ?
        "The path (the checks that answered it: nothing to run again)" : r.tool === "promql_query" ? "PromQL" : r.tool === "execute_sql" ||
        r.tool === "export_excel" ? "SQL" : "Chart settings (JSON)") + (r.target ? " · on " + r.target : "") }), code]),
      path ? el("label", { class: "field" }, [el("span", { text: "Its path: the data it uses and its steps (given to the agent with the query; correct it in your words)" }), path]) : null,
      el("div", { class: "actions" }, [noQuery ? null : check,
        el("button", { type: "button", class: "btn small primary", text: r.status === "confirmed" ? "Save" : "Save and confirm",
          title: "A changed query is run first: it is confirmed only if it works", onclick: function () { save(true); } }),
        r.status === "confirmed" ? null : el("button", { type: "button", class: "btn small", text: "Save only",
          onclick: function () { save(false); } }),
        el("button", { type: "button", class: "btn small", text: "Cancel", onclick: after })]),
      res,
      r.previous ? el("details", { class: "recipe-prev" }, [el("summary", { text: "Before the last change (" +
        (r.edited_by || "?") + ", " + S.when(r.edited_at) + ")" }), el("div", { text: r.previous.question || "" }),
        el("pre", { text: r.previous.query || "" })]) : null]));
    q.focus();
  }

  function recipes() {
    var st = $("r-status").value;
    return S.dict("GET", "recipes" + (st ? "?status=" + st : "")).then(function (data) {
      var tb = $("recipes").querySelector("tbody");
      tb.innerHTML = "";
      (data.recipes || []).forEach(function (r) {
        var kind = r.tool === "investigation" ? "investigation path" : r.tool === "path" ? "path (no query to run)" : r.tool;
        var way = el("td", { class: "nm" }, [el("div", { class: "muted", text: kind + (r.target ? " · " + r.target : "") }),
                                             el("details", {}, [el("summary", { text: (r.query || "").slice(0, 90) + ((r.query || "").length > 90 ? "…" : "") }),
                                                                el("pre", { text: r.query || "" })]),
                                             r.path ? el("details", { class: "recipe-way" }, [el("summary", { text: "its path: the data it uses, its steps" }),
                                                                                             el("pre", { text: r.path })]) : null]);
        var actions = el("td", { class: "row-actions" });
        var u = r.use || {};
        var q = el("td", {}, [el("div", { text: r.question }), el("div", { class: "use-why" + (r.demoted ? " bad" : r.rank > 0.2 ? " good" : ""),
          text: r.why || "" }), r.edited_by ? el("div", { class: "muted small-note", text: "edited by " + r.edited_by + ", " + S.when(r.edited_at) }) : null]);
        var tr = el("tr", { class: r.demoted ? "demoted" : "" }, [q, way,
          el("td", { class: "num", text: r.seconds !== null && r.seconds !== undefined ? S.num(r.seconds) + " s" : "" }),
          el("td", { class: "num", title: "answers it was given to: helpful / not helpful",
                     text: u.given ? S.num(u.helpful || 0) + " / " + S.num(u.not_helpful || 0) : S.num(r.helpful || r.uses) }),
          el("td", { html: '<span class="badge ' + (r.status === "confirmed" ? "ok" : r.status === "rejected" ? "bad" : r.status === "helpful" ? "warn" : "") + '">' +
                           S.esc(r.status === "helpful" ? "helpful, to review" : r.status === "auto" ? "automatic (old)" : r.status) + "</span>" }),
          actions]);
        if (data.is_admin) {
          if (r.status !== "confirmed" && r.status !== "auto") actions.appendChild(el("button", { type: "button", class: "linkish", text: "Confirm",
            onclick: function () { S.dict("POST", "recipes/" + r.id, { status: "confirmed" }).then(function (x) {
              if (x.error) { actions.appendChild(el("span", { class: "result bad", text: x.error })); return; }
              if (D.saved) D.saved(); recipes(); }); } }));
          if (r.status !== "auto") actions.appendChild(el("button", { type: "button", class: "linkish", text: "Edit",
            title: "Correct its question, its query or its path (then confirm it)", onclick: function () {
              if (tr.classList.contains("edited-row")) return;     // its editor is open below it
              var cell = el("td", { colspan: "6" });
              var row = el("tr", { class: "editing" }, [cell]);
              tr.classList.add("edited-row");                       // the row stays: the editor is under it
              tr.after(row);
              recipeEditor(r, cell, recipes);
            } }));
          if (r.status !== "rejected" && r.status !== "auto") actions.appendChild(el("button", { type: "button", class: "linkish", text: "Reject",
            onclick: function () { S.dict("POST", "recipes/" + r.id, { status: "rejected" }).then(function () { if (D.saved) D.saved(); recipes(); }); } }));
          actions.appendChild(S.sureButton("Delete", function () { S.dict("DELETE", "recipes/" + r.id).then(recipes); },
            { ask: "Delete this learned answer?", detail: "It is no longer proposed; a new Helpful answer may teach it again. " +
              "Reject keeps it without using it." }));
        }
        tb.appendChild(tr);
      });
      if (!(data.recipes || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted",
        text: "Nothing learned yet: an answer marked Helpful in the chat is learned here, for an admin to confirm or reject." })]));
    });
  }

  function timings() {
    S.dict("GET", "timings?" + paged("timings")).then(function (data) {
      pages.timings.total = data.total || 0;
      S.pager($("t-pager"), pages.timings, timings);
      var tb = $("timings").querySelector("tbody");
      tb.innerHTML = "";
      (data.timings || []).forEach(function (t) {
        var q = t.pattern || "";
        var parts = [el("summary", { text: q.length > 160 ? q.slice(0, 160) + "…" : q }), el("pre", { text: q }),
                     copyLink("Copy", function () { return q; })];
        if (t.last_error) parts.push(el("div", { class: "muted", text: "Last error:" }), el("pre", { class: "err", text: t.last_error }));
        if (t.last_query) parts.push(el("div", { class: "muted", text: "Last one as it ran (admins only):" }), el("pre", { text: t.last_query }),
                                     copyLink("Copy", function () { return t.last_query; }));
        tb.appendChild(el("tr", {}, [el("td", { class: "nm", text: t.target || "" }),
          el("td", { class: "nm" }, [el("details", { class: "query" }, parts)]), el("td", { class: "num", text: S.num(t.calls) }),
          el("td", { class: "num", text: S.num(t.avg_seconds) + " s" }), el("td", { class: "num", text: S.num(t.max_seconds) + " s" }),
          el("td", { class: "num", text: t.errors ? S.num(t.errors) : "" })]));
      });
      if (!(data.timings || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "No query yet." })]));
    });
  }

  // ------------------------------------------------------------------ Knowledge: the Context, read as documentation
  /* a reader: the contents on the left (functional, technical; a box finds the pages with some words), the page on
     the right with what it comes from; Word or PDF of a page or of everything; admins correct a page in place */
  var ctx = { pages: [], current: null, want: null };
  function ctxExport(fmt, pageId) {
    return document.body.dataset.dictionaryApi + "context/export." + fmt + (pageId ? "?page=" + pageId : "");
  }
  $("ctx-docx").href = ctxExport("docx");
  $("ctx-pdf").href = ctxExport("pdf");

  function contextTab() {
    return S.dict("GET", "context?full=1").then(function (d) {
      ctx.pages = d.pages || [];
      var box = $("ctx-actions");
      box.innerHTML = "";
      var last = d.last_build;
      box.appendChild(el("span", { class: "muted", text: (d.enabled ? "Written every night from " + d.hour + ":00" :
        "The nightly build is off (context.enabled)") + (last ? " · last build #" + last.id + ": " + last.status + ", " +
        S.when(last.finished_at || last.started_at) : " · no build yet") + " · " + S.num(ctx.pages.length) + " page" +
        (ctx.pages.length === 1 ? "" : "s") }));
      if (state.admin) {
        var res = el("span", { class: "result" });
        box.appendChild(document.createTextNode(" "));
        box.appendChild(el("button", { type: "button", class: "btn small", text: "Build now", onclick: function () {
          S.dict("POST", "context/build", {}).then(function (r) {
            res.textContent = r.error || "started: it shows in Settings → Chat settings, runs list";
          });
        } }));
        box.appendChild(res);
      }
      ["ctx-docx", "ctx-pdf"].forEach(function (id) { $(id).hidden = !ctx.pages.length; });
      ctxToc();
      var want = ctx.want || ctx.current;
      ctx.want = null;
      if (!ctx.pages.some(function (p) { return p.id === want; })) want = ctx.pages.length ? ctx.pages[0].id : null;
      if (want) ctxShow(want);
      else {
        var art = $("ctx-article");
        art.innerHTML = "";
        art.appendChild(el("div", { class: "ctx-empty" }, [el("h3", { text: "No Context yet" }),
          el("p", { class: "muted", text: "The Context is written every night from the documents, the catalog, the team " +
            "memory and the dictionary of the databases you may query." + (state.admin ? " Build now writes it at once." : "") })]));
      }
    });
  }

  function ctxMatches(p, q) {
    return !q || (p.title + "\n" + (p.text || "")).toLowerCase().indexOf(q) >= 0;
  }
  function ctxToc() {
    var toc = $("ctx-toc"), q = $("ctx-q").value.trim().toLowerCase();
    toc.innerHTML = "";
    [["functional", "Functional"], ["technical", "Technical"]].forEach(function (sec) {
      var pages = ctx.pages.filter(function (p) { return p.section === sec[0] && ctxMatches(p, q); });
      var all = ctx.pages.filter(function (p) { return p.section === sec[0]; }).length;
      toc.appendChild(el("div", { class: "toc-head" }, [el("span", { text: sec[1] }),
        el("span", { class: "muted", text: q ? S.num(pages.length) + " of " + S.num(all) : S.num(all) })]));
      var ul = el("ul", { class: "toc-list" });
      pages.forEach(function (p) {
        var on = p.id === ctx.current;
        ul.appendChild(el("li", {}, [el("button", { type: "button", class: "toc-item" + (on ? " on" : ""),
          "aria-current": on ? "page" : null, onclick: function () { ctxShow(p.id); } }, [
            el("span", { class: "toc-title", text: p.title }),
            p.ai ? el("span", { class: "badge llm", text: "AI", title: "AI-written" }) :
              p.author !== "agent" ? el("span", { class: "badge curated", text: "edited", title: "Edited by " + p.author }) : null])]));
      });
      if (!pages.length) ul.appendChild(el("li", { class: "muted toc-none", text: q ? "No page with these words." : "No page yet." }));
      toc.appendChild(ul);
    });
  }
  $("ctx-q").addEventListener("input", function () { ctxToc(); });

  function ctxShow(id) {
    var p = ctx.pages.filter(function (x) { return x.id === id; })[0];
    if (!p) return;
    ctx.current = id;
    ctxToc();
    var art = $("ctx-article");
    art.innerHTML = "";
    var meta = [p.section === "functional" ? "Functional" : "Technical", p.author !== "agent" ? "edited by " + p.author : null,
                "updated " + S.when(p.updated_at), "version " + p.version].filter(Boolean).join(" · ");
    art.appendChild(el("header", { class: "ctx-head" }, [
      el("h2", { id: "ctx-title", text: p.title }),
      el("div", { class: "ctx-meta" }, [p.ai ? el("span", { class: "badge llm", text: "AI-written",
        title: "Written by the LLM from the sources below: check before relying on it" }) : null,
        el("span", { class: "muted", text: (p.ai ? " " : "") + meta }),
        el("span", { class: "grow" }),
        el("span", { class: "export-label", text: "This page:" }),
        el("a", { class: "btn small", href: ctxExport("docx", p.id), text: "Word" }),
        el("a", { class: "btn small", href: ctxExport("pdf", p.id), text: "PDF" })])]));
    var body = el("div", { class: "answer context-page", html: p.html || "" });   // Markdown made safe on the server
    var first = body.firstElementChild;                  // its first heading only repeats the page's title: left out
    if (first && /^H[1-3]$/.test(first.tagName) && first.textContent.trim().toLowerCase() === (p.title || "").trim().toLowerCase()) {
      first.remove();
    }
    art.appendChild(body);
    if ((p.sources || []).length) {
      art.appendChild(el("details", { class: "ctx-sources" }, [el("summary", { text: "Written from " + p.sources.length + " source" +
        (p.sources.length === 1 ? "" : "s") }), el("ol", {}, p.sources.map(function (x) {
          return el("li", { text: x.title + " (" + x.ref + ")" }); }))]));
    }
    if (state.admin) {
      var area = el("textarea", { class: "context-edit", rows: "16", "aria-label": "The page (Markdown)" });
      area.value = p.content || "";
      var result = el("span", { class: "result" });
      var save = el("button", { type: "button", class: "btn primary", text: "Save my version", onclick: function () {
        save.disabled = true;
        S.dict("POST", "context/" + p.id, { content: area.value }).then(function (r) {
          save.disabled = false;
          result.textContent = r.error || "saved: the agent will not write over it";
          result.className = "result " + (r.error ? "bad" : "good");
          if (!r.error) { if (D.saved) D.saved(); ctx.want = p.id; contextTab(); }
        });
      } });
      var back = S.sureButton("Give it back to the agent", function () {
        S.dict("POST", "context/" + p.id, { reset: true }).then(function (r) {
          result.textContent = r.error || "the agent writes it again at the next build";
          if (!r.error) { if (D.saved) D.saved(); ctx.want = p.id; contextTab(); }
        });
      }, { cls: "btn", ask: "Give this page back to the agent?", yes: "Give it back",
           detail: "Your version stays until the next build, which writes the page again from its sources." });
      art.appendChild(el("details", { class: "context-editor" }, [el("summary", { text: "Correct this page" }), area,
        el("div", { class: "actions" }, [save, p.author !== "agent" ? back : null, result])]));
    }
    var top = art.getBoundingClientRect().top;
    if (top < 0) art.scrollIntoView({ block: "start" });
  }

  function contextPage(id) {                       // #open/context:<id> and the search's links: the page in the reader
    ctx.want = id;
    if (active === "context") contextTab(); else show("context", true);
  }

  // ------------------------------------------------------------------ Search: every piece found, page by page
  var found = { rows: [], page: 0, size: 20, total: 0 };

  /* a piece's name: a link to where it is read or edited (a chart or a dashboard in Superset, in a new tab; an
     index, a metric, an entry, a note... here, opened in the side panel; the address can be copied or bookmarked) */
  function refLink(r) {
    var name = r.title || r.ref;
    if (!r.link) return el("strong", { text: name });
    if (r.link.where === "superset") {
      return el("a", { href: r.link.href, target: "_blank", rel: "noopener", class: "k-name", text: name,
                       title: "Open in Superset (new tab)" });
    }
    return el("a", { href: r.link.href, class: "k-name", text: name, title: "Open it", onclick: function (ev) {
      if (ev.button || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;   // a new tab, as asked
      ev.preventDefault();
      try { history.replaceState(null, "", r.link.href); } catch (e) { /* no history API: the link still opens it */ }
      openRef(decodeURIComponent(r.link.href.replace(/^#open\//, "")), true);   // the results stay behind it
    } });
  }

  function foundPage() {
    var box = $("k-results");
    box.innerHTML = "";
    found.rows.slice(found.page * found.size, (found.page + 1) * found.size).forEach(function (r) {
      box.appendChild(el("div", { class: "k-hit" }, [
        el("div", {}, [el("span", { class: "badge", text: r.kind }), document.createTextNode(" "), refLink(r),
          r.via ? el("span", { class: "muted k-via", text: " · found by " + r.via }) : null]),
        el("div", { class: "k-text", text: r.text || "" })]));
    });
    S.pager($("k-pager"), found, function () { foundPage(); $("k-count").scrollIntoView({ block: "nearest" }); });
  }

  $("k-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var q = $("k-q").value.trim(), box = $("k-results");
    if (!q) return;
    box.innerHTML = "";
    $("k-pager").hidden = true;
    $("k-count").textContent = "Searching…";
    S.dict("GET", "search?all=1&q=" + encodeURIComponent(q) + ($("k-kind").value ? "&kind=" + $("k-kind").value : "")).then(function (d) {
      found.rows = d.results || [];
      found.total = found.rows.length;
      found.page = 0;
      $("k-count").textContent = d.error ? d.error : !found.total ? "Nothing found." :
        S.num(found.total) + (found.total === 1 ? " result" : " results") + (d.capped ? " (the " + S.num(found.total) + " best)" : "");
      foundPage();
    });
  });

  /* #open/<ref> (the search's links): the item in the side panel; an address opened as such shows the item's tab
     behind it, a click in the search's results keeps them (stay) */
  function openRef(ref, stay) {
    var m = /^([a-z]+):(\d+)$/.exec(ref || "");
    if (!m) return;
    var kind = m[1], id = +m[2];
    var tab = { object: "browse", entry: "catalog", memory: "memory", doc: "docs", note: "notes",
                recipe: "learned" }[kind];
    if (!stay && tab) show(tab, true);
    if (kind === "object") { detail(id); return; }
    if (kind === "context") { contextPage(id); return; }
    if (kind === "entry" && state.admin && D.openEntry) { D.openEntry(id); return; }
    S.dict("GET", "item?ref=" + encodeURIComponent(ref)).then(function (x) {
      if (x.error) { textDrawer("Not found", ref, x.error); return; }
      textDrawer(x.title || ref, x.kind, x.text);
    });
  }

  // ------------------------------------------------------------------ wiring
  loaders.browse = function () { if (!browsed) { browsed = true; load(); } };
  loaders.relations = relations;
  loaders.changes = changes;
  loaders.context = contextTab;
  loaders.learned = function () { agentKnowledge(); whereData(); recipes(); timings(); };
  loaders.search = function () { $("k-q").focus(); };

  $("r-status").addEventListener("change", recipes);
  var timer = null;
  $("filters").addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { state.page = 0; load(); }, 250); });
  $("filters").addEventListener("submit", function (ev) { ev.preventDefault(); });
  $("prev").addEventListener("click", function () { if (state.page > 0) { state.page--; load(); } });
  $("next").addEventListener("click", function () { state.page++; load(); });
  $("c-days").addEventListener("change", function () { pages.changes.page = 0; changes(); });
  $("d-close").addEventListener("click", function () { $("drawer").hidden = true; });
  document.querySelectorAll(".drawer").forEach(function (dr) {
    dr.addEventListener("click", function (ev) { if (ev.target === dr) dr.hidden = true; });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") document.querySelectorAll(".drawer").forEach(function (dr) { dr.hidden = true; });
  });

  /* what knowledge.js uses: its tabs in the same controller, the object drawer, the rows; it sets saved (a save
     was made: follow the agent's search), openEntry (the catalog editor) and reviewWaiting (for the first tab) */
  var D = S.dictionary = {
    register: function (name, fn) { loaders[name] = fn; },
    show: show, detail: detail, textDrawer: textDrawer, emptyRow: emptyRow, row: row, browseUnverified: browseUnverified,
    isActive: function (name) { return active === name; },
    saved: null, openEntry: null, reviewWaiting: null
  };

  function start() {
    summary();
    var h = (location.hash || "").replace(/^#/, "");
    if (/^open\//.test(h)) { openRef(decodeURIComponent(h.slice(5))); return; }
    if (h && mainOf(h)) { show(h, true); return; }
    if (state.admin && D.reviewWaiting) {
      D.reviewWaiting().then(function (n) { show(n ? "review" : "catalog", true); }, function () { show("catalog", true); });
      return;
    }
    show("catalog", true);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else setTimeout(start, 0);                       // after knowledge.js registered its tabs
  window.addEventListener("hashchange", function () {
    var h = (location.hash || "").replace(/^#/, "");
    if (/^open\//.test(h)) { openRef(decodeURIComponent(h.slice(5))); return; }
    if (h && h !== active && mainOf(h)) show(h, true);
  });
})();
