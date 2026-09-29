/* supagent data dictionary page */
(function () {
  "use strict";
  var S = window.supagent, el = S.el, esc = S.esc;
  var state = { page: 0, size: 50, total: 0, admin: false };
  var $ = function (id) { return document.getElementById(id); };
  var KIND = { metric: "metric", label: "label", family: "family", index: "index", field: "field" };

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
      state.admin = !!data.is_admin;
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
                     s.unverified ? el("span", { html: S.num(s.unverified) + ' <span class="badge llm">AI-written</span> to check' }) : null]))
        ]);
        box.appendChild(card);
        if (!sel.querySelector('option[value="' + s.id + '"]')) sel.appendChild(el("option", { value: s.id, text: s.database }));
      });
      if (!(data.sources || []).length) {
        box.appendChild(el("div", { class: "source muted", text: "Nothing learned yet for the databases you may query. An admin can start a learning run in Settings → Chat settings." }));
      }
    });
  }

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
    S.dict("GET", "objects?" + query()).then(function (data) {
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

  function detail(id) {
    S.dict("GET", "objects/" + id).then(function (o) {
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
      if (state.admin) b.appendChild(editor(o));
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
          row.appendChild(el("button", { type: "button", class: "btn small", text: "Open " + (r.other.parent ? r.other.parent + " \u203a " : "") + r.other.name,
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
      $("drawer").hidden = false;
      $("d-close").focus();
    });
  }

  function editor(o) {
    var ta = el("textarea", { rows: "3", "aria-label": "Description" });
    ta.value = o.description || "";
    var cat = el("input", { type: "text", "aria-label": "Category", placeholder: "category" });
    cat.value = o.category || "";
    var unit = el("input", { type: "text", "aria-label": "Unit", placeholder: "unit" });
    unit.value = o.unit || "";
    var syn = el("input", { type: "text", "aria-label": "Also called", placeholder: "also called (comma separated)" });
    syn.value = (o.synonyms || []).join(", ");
    var res = el("span", { class: "result" });
    var save = el("button", { type: "button", class: "btn primary small", text: "Save as curated", onclick: function () {
      S.dict("POST", "objects/" + o.id, { description: ta.value, category: cat.value, unit: unit.value, synonyms: syn.value }).then(function (r) {
        res.textContent = r.error || "saved";
        res.className = "result " + (r.error ? "bad" : "good");
        if (!r.error) { detail(o.id); load(); }
      });
    } });
    var kids = [el("h3", { text: "Edit (admins)" }), ta, el("div", { class: "actions" }, [cat, unit]), syn, el("div", { class: "actions" }, [save])];
    if (o.description_source === "llm" && !o.verified) {
      kids[kids.length - 1].appendChild(el("button", { type: "button", class: "btn small", text: "Approve the AI text", onclick: function () {
        S.dict("POST", "objects/" + o.id, { approve: true }).then(function () { detail(o.id); load(); });
      } }));
    }
    kids[kids.length - 1].appendChild(res);
    return el("div", { class: "edit section" }, kids);
  }

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
    $("drawer").hidden = false;
  }
  function emptyRow(cols, text) { return el("tr", {}, [el("td", { colspan: String(cols), class: "muted", text: text })]); }
  function row(onclick, cells) {
    return el("tr", { class: "clickable", tabindex: "0", onclick: onclick,
      onkeydown: function (ev) { if (ev.key === "Enter") onclick(); } }, cells);
  }

  function knowledge() {
    return S.dict("GET", "knowledge").then(function (d) {
      var tb = $("kn-entries").querySelector("tbody");
      tb.innerHTML = "";
      (d.entries || []).forEach(function (e) {
        tb.appendChild(row(function () {
          textDrawer(e.title, e.classification + (e.category ? " · " + e.category : "") + " · by " + (e.by || "?"), e.content);
        }, [el("td", { class: "nm", text: e.title }), el("td", { text: e.classification }), el("td", { text: e.category || "" }),
            el("td", { text: e.by || "" }), el("td", { text: S.when(e.updated_at) })]));
      });
      if (!(d.entries || []).length) tb.appendChild(emptyRow(5, "No catalog entry yet."));
      var td = $("kn-docs").querySelector("tbody");
      td.innerHTML = "";
      (d.docs || []).forEach(function (x) {
        td.appendChild(row(function () {
          textDrawer(x.title, (x.url || "uploaded file") + (x.category ? " · " + x.category : ""),
            x.excerpt + (x.chars > (x.excerpt || "").length ? "\n\u2026 (" + S.num(x.chars) + " characters in all)" : ""));
        }, [el("td", { class: "nm", text: x.title }), el("td", { text: x.kind === "url" ? "site" + (x.pages ? " (" + x.pages + " pages)" : "") : "file" }),
            el("td", { text: x.status || "" }), el("td", { class: "num", text: S.num(x.chars) + " characters" }),
            el("td", { text: S.when(x.fetched_at) })]));
      });
      if (!(d.docs || []).length) td.appendChild(emptyRow(5, "No document or site yet."));
      var ul = $("kn-memory");
      ul.innerHTML = "";
      (d.team_memory || []).forEach(function (m) {
        ul.appendChild(el("li", {}, [el("span", { class: "badge", text: m.kind }), " ", m.text,
          el("span", { class: "muted", text: " · " + S.when(m.created_at) })]));
      });
      if (!(d.team_memory || []).length) ul.appendChild(el("li", { class: "muted", text: "No team memory yet." }));
    });
  }

  function browseUnverified(sourceId) {             // the AI descriptions of a database, to verify
    $("f-source").value = String(sourceId);
    $("f-show").value = "unverified";
    document.querySelector('.subtabs button[data-tab="browse"]').click();
    state.page = 0;
    load();
  }

  function agentKnowledge() {
    return S.dict("GET", "agent_knowledge").then(function (d) {
      var box = $("agent-summary");
      box.innerHTML = "";
      box.appendChild(el("span", { class: "muted", text: S.num(d.relations_measured) + " relations measured" }));
      (d.to_verify || []).forEach(function (v) {
        box.appendChild(document.createTextNode(" · "));
        box.appendChild(el("button", { type: "button", class: "linkish", text: S.num(v.count) + " AI descriptions to verify in " + v.database,
          onclick: function () { browseUnverified(v.source_id); } }));
      });
      var te = $("agent-entries").querySelector("tbody");
      te.innerHTML = "";
      (d.entries || []).forEach(function (e) {
        te.appendChild(row(function () {
          textDrawer(e.title, e.classification + " · written by the agent" + (e.enabled ? "" : " · disabled"), e.content,
            [el("h3", { text: "Evidence" }), el("pre", { class: "entry-text", text: JSON.stringify(e.evidence || {}, null, 2) })]);
        }, [el("td", { class: "nm", text: e.title }), el("td", { text: e.classification }),
            el("td", { class: "nm", text: e.origin || "" }), el("td", { text: S.when(e.updated_at) })]));
      });
      if (!(d.entries || []).length) te.appendChild(emptyRow(4, "No catalog entry written by the agent yet."));
      var ta = $("associations").querySelector("tbody");
      ta.innerHTML = "";
      (d.associations || []).forEach(function (a) {
        ta.appendChild(el("tr", {}, [el("td", { class: "nm", text: a.word }),
          el("td", { class: "nm", text: (a.parent ? a.parent + " \u203a " : "") + a.name + " (" + a.kind + ")" }),
          el("td", { text: String(a.database) }), el("td", { class: "num", text: S.num(a.uses) })]));
      });
      if (!(d.associations || []).length) ta.appendChild(emptyRow(4, "Nothing yet: it comes from the answers of the chat."));
    });
  }

  function contextPage(id) {
    S.dict("GET", "context/" + id).then(function (p) {
      if (p.error) return;
      var b = $("d-body");
      b.innerHTML = "";
      b.appendChild(el("div", { class: "muted", text: p.section + " · version " + p.version + " · " + S.when(p.updated_at) +
        (p.author !== "agent" ? " · edited by " + p.author : "") }));
      b.appendChild(el("h2", { id: "d-title", text: p.title }));
      if (p.ai) b.appendChild(el("p", {}, [el("span", { class: "badge llm", text: "AI-written" }),
        el("span", { class: "muted", text: " from the sources below; check before relying on it" })]));
      b.appendChild(el("div", { class: "answer context-page", html: p.html }));   // Markdown made safe on the server
      if ((p.sources || []).length) {
        b.appendChild(el("h3", { text: "Sources" }));
        var ol = el("ol", { class: "context-sources" });
        p.sources.forEach(function (x) { ol.appendChild(el("li", { text: x.title + " (" + x.ref + ")" })); });
        b.appendChild(ol);
      }
      if (state.admin) {
        var area = el("textarea", { class: "context-edit", rows: "14", "aria-label": "Page" });
        area.value = p.content;
        var result = el("span", { class: "result" });
        var save = el("button", { type: "button", class: "btn primary", text: "Save my version", onclick: function () {
          S.dict("POST", "context/" + p.id, { content: area.value }).then(function (r) {
            result.textContent = r.error || "saved: the agent will not write over it";
            if (!r.error) contextTab();
          });
        } });
        var back = el("button", { type: "button", class: "btn", text: "Give it back to the agent", onclick: function () {
          S.dict("POST", "context/" + p.id, { reset: true }).then(function (r) {
            result.textContent = r.error || "the agent writes it again at the next build";
            if (!r.error) contextTab();
          });
        } });
        b.appendChild(el("details", { class: "context-editor" }, [el("summary", { text: "Correct this page" }), area,
          el("div", { class: "actions" }, [save, p.author !== "agent" ? back : null, result])]));
      }
      $("drawer").hidden = false;
    });
  }

  function contextTab() {
    return S.dict("GET", "context").then(function (d) {
      ["functional", "technical"].forEach(function (sec) {
        var ul = $("ctx-" + sec);
        ul.innerHTML = "";
        (d.pages || []).filter(function (p) { return p.section === sec; }).forEach(function (p) {
          var badge = p.ai ? el("span", { class: "badge llm", text: "AI-written" }) :
            (p.author !== "agent" ? el("span", { class: "badge curated", text: "edited by " + p.author }) :
              el("span", { class: "badge backend", text: "facts" }));
          ul.appendChild(el("li", {}, [el("button", { type: "button", class: "linkish", text: p.title,
            onclick: function () { contextPage(p.id); } }), " ", badge,
            el("span", { class: "muted", text: " · " + S.when(p.updated_at) })]));
        });
        if (!ul.children.length) ul.appendChild(el("li", { class: "muted", text: "No page yet." }));
      });
      var box = $("ctx-actions");
      box.innerHTML = "";
      var last = d.last_build;
      box.appendChild(el("span", { class: "muted", text: (d.enabled ? "Built every night from " + d.hour + ":00" :
        "The nightly build is off (context.enabled)") + (last ? " · last build #" + last.id + ": " + last.status + ", " +
        S.when(last.finished_at || last.started_at) : " · no build yet") }));
      if (state.admin) {
        var res = el("span", { class: "result" });
        box.appendChild(document.createTextNode(" "));
        box.appendChild(el("button", { type: "button", class: "btn", text: "Build now", onclick: function () {
          S.dict("POST", "context/build", {}).then(function (r) {
            res.textContent = r.error || "started: it shows in Settings → Chat settings, runs list";
          });
        } }));
        box.appendChild(res);
      }
    });
  }

  function learned() {
    agentKnowledge();
    var st = $("r-status").value;
    S.dict("GET", "recipes" + (st ? "?status=" + st : "")).then(function (data) {
      var tb = $("recipes").querySelector("tbody");
      tb.innerHTML = "";
      (data.recipes || []).forEach(function (r) {
        var way = el("td", { class: "nm" }, [el("div", { class: "muted", text: r.tool + (r.target ? " \u00b7 " + r.target : "") }),
                                             el("details", {}, [el("summary", { text: (r.query || "").slice(0, 90) + ((r.query || "").length > 90 ? "\u2026" : "") }),
                                                                el("pre", { text: r.query || "" })])]);
        var actions = el("td", {});
        if (data.is_admin) {
          [["confirmed", "Confirm"], ["rejected", "Reject"]].forEach(function (a) {
            if (r.status !== a[0] && r.status !== "auto") actions.appendChild(el("button", { type: "button", class: "linkish", text: a[1], onclick: function () {
              S.dict("POST", "recipes/" + r.id, { status: a[0] }).then(learned);
            } }));
          });
          actions.appendChild(el("button", { type: "button", class: "linkish", text: "Delete", onclick: function () {
            S.dict("DELETE", "recipes/" + r.id).then(learned);
          } }));
        }
        tb.appendChild(el("tr", {}, [el("td", { text: r.question }), way,
          el("td", { class: "num", text: r.seconds !== null && r.seconds !== undefined ? S.num(r.seconds) + " s" : "" }),
          el("td", { class: "num", text: S.num(r.helpful || r.uses) }),
          el("td", { html: '<span class="badge ' + (r.status === "confirmed" ? "ok" : r.status === "rejected" ? "bad" : r.status === "helpful" ? "warn" : "") + '">' +
                           S.esc(r.status === "helpful" ? "helpful, to review" : r.status === "auto" ? "automatic (old)" : r.status) + "</span>" }),
          actions]));
      });
      if (!(data.recipes || []).length) tb.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted",
        text: "Nothing learned yet: an answer marked Helpful in the chat is learned here, for an admin to confirm or reject." })]));
    });
    timings();
  }

  function timings() {
    S.dict("GET", "timings?" + paged("timings")).then(function (data) {
      pages.timings.total = data.total || 0;
      S.pager($("t-pager"), pages.timings, timings);
      var tb = $("timings").querySelector("tbody");
      tb.innerHTML = "";
      (data.timings || []).forEach(function (t) {
        var q = t.pattern || "";
        var parts = [el("summary", { text: q.length > 160 ? q.slice(0, 160) + "\u2026" : q }), el("pre", { text: q }),
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

  document.querySelectorAll(".subtabs button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".subtabs button").forEach(function (x) { x.classList.toggle("on", x === btn); });
      ["browse", "relations", "changes", "learned", "knowledge", "context", "search"].forEach(function (t) { $("tab-" + t).hidden = t !== btn.dataset.tab; });
      if (btn.dataset.tab === "relations") relations();
      if (btn.dataset.tab === "changes") changes();
      if (btn.dataset.tab === "learned") learned();
      if (btn.dataset.tab === "knowledge") knowledge();
      if (btn.dataset.tab === "context") contextTab();
    });
  });
  $("r-status").addEventListener("change", learned);
  $("k-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var q = $("k-q").value.trim(), box = $("k-results");
    if (!q) return;
    box.innerHTML = "";
    box.appendChild(el("div", { class: "muted", text: "Searching\u2026" }));
    S.dict("GET", "search?q=" + encodeURIComponent(q) + ($("k-kind").value ? "&kind=" + $("k-kind").value : "")).then(function (d) {
      box.innerHTML = "";
      (d.results || []).forEach(function (r) {
        box.appendChild(el("div", { class: "k-hit" }, [
          el("div", {}, [el("span", { class: "badge", text: r.kind }), document.createTextNode(" "), el("strong", { text: r.title })]),
          el("div", { class: "k-text", text: (r.text || "").slice(0, 700) + ((r.text || "").length > 700 ? "\u2026" : "") })]));
      });
      if (!(d.results || []).length) box.appendChild(el("div", { class: "muted", text: d.error || "Nothing found." }));
    });
  });
  var timer = null;
  $("filters").addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { state.page = 0; load(); }, 250); });
  $("filters").addEventListener("submit", function (ev) { ev.preventDefault(); });
  $("prev").addEventListener("click", function () { if (state.page > 0) { state.page--; load(); } });
  $("next").addEventListener("click", function () { state.page++; load(); });
  $("c-days").addEventListener("change", function () { pages.changes.page = 0; changes(); });
  $("d-close").addEventListener("click", function () { $("drawer").hidden = true; });
  $("drawer").addEventListener("click", function (ev) { if (ev.target === $("drawer")) $("drawer").hidden = true; });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") $("drawer").hidden = true; });
  summary().then(load);
})();
