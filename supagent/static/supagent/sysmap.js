/* supagent Data dictionary: the System map (0.8). The architecture of the system as the categories describe it: one
   column per category (subjects, applications, components, the deployment's own), a box per part with what it does
   (its description, else a sentence of the documents that name it) and its items, a grey line from a part to what
   it is part of, the interactions an admin drew as arrows. A big category is folded into one box (click it to open
   it). Admins: Edit to move the boxes, draw an interaction (a box, then another), write what a part is, hide a part.
   Exports: SVG, PNG, PDF. Drawn in SVG by hand: Superset's pages allow no external script. */
(function () {
  "use strict";
  var S = window.supagent, el = S.el, D = S.dictionary;
  var $ = function (id) { return document.getElementById(id); };
  if (!D || !$("map-canvas")) return;
  var NS = "http://www.w3.org/2000/svg";
  var FOLD_AT = 18, FOLD_ALWAYS = 60;   // a category read from the data with more parts than 18 is folded into one
                                        // box (servers...), any category with more than 60
  var BOX_W = 224, GAP = 14, COL_GAP = 96, PAD = 28, HEAD_H = 40, LINE = 16;
  var st = { data: null, edit: false, open: {}, focus: "", sel: null, from: null, view: { x: 0, y: 0, k: 1 },
             boxes: {}, saveTimer: null, fitted: false, seq: 0, sig: "", fresh: null, freshTimer: null, autoAt: 0, note: "", freshAt: -1, freshCats: [], inputAt: Date.now() };
  var AUTO_MS = 20000, FRESH_MS = 20000;   // read again every 20 s while it is looked at; what is new stays outlined 20 s
  var IDLE_MS = 15 * 60000;                // nobody at the page for 15 minutes: it stops reading (the session may end)
  var OTHERS_MS = 60000;                   // who is not an admin: read again once a minute

  // ------------------------------------------------------------------ the colours of the page (tokens), resolved
  function tok(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function colors() {
    return { panel: tok("--panel", "#fff"), bg: tok("--bg", "#f6f8f9"), ink: tok("--ink", "#1f2a33"),
             ink2: tok("--ink-2", "#4b5a66"), muted: tok("--muted", "#74828c"), line: tok("--line", "#e1e7eb"),
             axis: tok("--axis", "#c3cbd0"), accent: tok("--accent", "#1f86a8"), warn: tok("--warn", "#9a6400"),
             font: tok("--font", "sans-serif"),
             series: ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"].map(function (n, i) {
               return tok(n, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"][i]); }) };
  }
  function svg(tag, attrs, kids) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]); });
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  var measure = document.createElement("canvas").getContext("2d");
  function wrap(text, font, width, maxLines) {
    measure.font = font;
    var words = String(text || "").split(/\s+/).filter(Boolean), lines = [], cur = "";
    for (var i = 0; i < words.length; i++) {
      var next = cur ? cur + " " + words[i] : words[i];
      if (measure.measureText(next).width <= width || !cur) { cur = next; continue; }
      lines.push(cur);
      cur = words[i];
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && cur) lines.push(cur);
    else if (cur && lines.length === maxLines) lines[maxLines - 1] = lines[maxLines - 1].replace(/\s*\S*$/, "") + "…";
    return lines.map(function (l) {
      while (measure.measureText(l).width > width && l.length > 1) l = l.slice(0, -2) + "…";
      return l;
    });
  }
  function catLabel(name, n) {                                // Subjects, Applications, Components; one's own: as named
    var s = name.charAt(0).toUpperCase() + name.slice(1);
    return n === 1 || ["subject", "application", "component"].indexOf(name) < 0 ? s : s + "s";
  }

  // ------------------------------------------------------------------ what is shown
  /* The map follows the categories by itself: it is read again each time it is shown, when the window comes back,
     and every 20 seconds while it is looked at (quietly: drawn again only when something changed, never while an
     admin edits it). While it is read the page says so; what a reading brought (a new part, a new "part of") is
     outlined for a moment and said next to the title. */
  function signature(d) { return JSON.stringify([d.values, d.links, d.categories, d.proposed, d.layout && [d.layout.positions, d.layout.hidden, d.layout.folded]]); }
  function setState(text, cls) {
    var box = $("map-state");
    if (!box) return;
    box.textContent = text || "";
    box.className = "map-state" + (cls ? " " + cls : "");
  }
  function changes(before, after) {                           // what a reading brought
    var had = {}, pairs = {}, now = {}, out = { ids: [], pairs: [], gone: 0, cats: [] };
    var cats = before.categories.map(function (c) { return c.name; });
    after.categories.forEach(function (c) { if (cats.indexOf(c.name) < 0) out.cats.push(c.name); });
    before.values.forEach(function (v) {
      had[v.id] = true;
      (v.parents || []).forEach(function (p) { pairs[p + ">" + v.id] = true; });
    });
    after.values.forEach(function (v) {
      now[v.id] = true;
      if (!had[v.id]) out.ids.push(v.id);
      (v.parents || []).forEach(function (p) { if (!pairs[p + ">" + v.id]) out.pairs.push(p + ">" + v.id); });
    });
    before.values.forEach(function (v) { if (!now[v.id]) out.gone++; });
    return out;
  }
  function unfresh() {
    st.fresh = null;
    st.freshCats = [];
    st.note = "";
    document.querySelectorAll("#map-canvas .map-new, #map-canvas .map-edge-new").forEach(function (x) {
      x.classList.remove("map-new", "map-edge-new"); });
    setState("");
  }
  function say(what) {                                        // what the reading brought, for a moment
    if (what && (what.ids.length || what.pairs.length || what.gone || what.cats.length)) {
      var parts = [];
      if (what.cats.length) parts.push(what.cats.length === 1 ? "new category “" + catLabel(what.cats[0], 1) + "”" : what.cats.length + " new categories");
      if (what.ids.length) parts.push(what.ids.length === 1 ? "new part “" + st.byId[what.ids[0]].value + "”" : what.ids.length + " new parts");
      if (what.pairs.length) parts.push(what.pairs.length + " new “part of”");
      if (what.gone) parts.push(what.gone + " part" + (what.gone > 1 ? "s" : "") + " removed");
      st.note = "Updated: " + parts.join(", ");
      clearTimeout(st.freshTimer);
      st.freshTimer = setTimeout(unfresh, FRESH_MS);
    }
    setState(st.note || "", st.note ? "changed" : "");
    if (st.note && ((st.fresh && Object.keys(st.fresh.ids).length) || st.freshCats.length)) {
      $("map-state").appendChild(el("button", { type: "button", class: "linkish", text: "Show", onclick: showNew,
                                                title: "Bring what is new in view (again: the next one)" }));
    }
  }
  function onScreen(n) {
    var c = $("map-canvas"), k = st.view.k, x0 = n.x * k + st.view.x, y0 = n.y * k + st.view.y;
    return x0 >= 0 && y0 >= 0 && x0 + n.w * k <= c.clientWidth && y0 + n.h * k <= c.clientHeight;
  }
  function newNodes() {              // the boxes of the new parts (a folded category: its box), of the new categories
    var seen = {};
    return (st.fresh ? Object.keys(st.fresh.ids) : []).map(function (i) { return real(st.layout.nodes, i); })
      .concat(st.freshCats.map(function (c) { return st.layout.nodes["none:" + c]; }))
      .filter(function (n) { if (!n || seen[n.id]) return false; seen[n.id] = true; return true; });
  }
  function showNew() {                                        // the next new part brought in view
    var nodes = newNodes();
    if (!nodes.length) return;
    st.freshAt = (st.freshAt + 1) % nodes.length;
    inView(nodes[st.freshAt]);
  }
  function load(quiet) {
    quiet = quiet === true;
    var seq = ++st.seq;
    if (!quiet) {
      setState(st.data ? "Updating…" : "Loading the map…", "busy");
      $("map-canvas").setAttribute("aria-busy", "true");
    }
    return S.dict("GET", "map").then(function (d) {
      if (seq !== st.seq) return;                             // a later reading answers
      $("map-canvas").removeAttribute("aria-busy");
      if (d.error) {
        if (st.data) { setState("Not updated (" + d.error + "): the map of before", "warn"); return; }
        setState("");
        $("map-canvas").textContent = d.error;
        return;
      }
      var sig = signature(d), before = st.data;
      if (quiet && (sig === st.sig || editing())) {           // nothing new, or an admin is editing: later
        if ($("map-state").classList.contains("warn")) setState(st.note || "", st.note ? "changed" : "");
        return;
      }
      var what = before ? changes(before, d) : null;
      st.sig = sig;
      if (what && (what.ids.length || what.pairs.length)) {
        st.fresh = { ids: {}, pairs: {} };
        what.ids.forEach(function (i) { st.fresh.ids[i] = true; });
        what.pairs.forEach(function (k) { st.fresh.pairs[k] = true; });
      }
      st.data = d;
      st.byId = {};
      d.values.forEach(function (v) { st.byId[v.id] = v; });
      st.kids = {};
      d.values.forEach(function (v) { (v.parents || []).forEach(function (p) { (st.kids[p] = st.kids[p] || []).push(v.id); }); });
      var lay = d.layout || {};
      st.positions = lay.positions || {};
      st.hidden = lay.hidden || [];
      st.folded = lay.folded || {};
      focusOptions();
      draw();
      if (!st.fitted) { fit(true); st.fitted = true; }
      st.freshAt = -1;
      st.freshCats = what ? what.cats : [];
      if (!quiet && (st.fresh || st.freshCats.length)) {      // just made elsewhere on the page: brought in view
        if (st.freshCats.length) fit(true);                   // one more column: the first view again
        var nodes = newNodes();
        if (nodes.length && !nodes.some(onScreen)) { st.freshAt = 0; inView(nodes[0]); }
      }
      say(what);
    });
  }
  function editing() {                                        // an admin moves boxes, draws an interaction or types
    var a = document.activeElement;
    return st.edit || !!st.from || (!!a && $("map-side").contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }
  function auto() {
    if (!st.data || !D.isActive("map") || document.visibilityState !== "visible" || editing()) return;
    if (Date.now() - st.inputAt > IDLE_MS) return;            // nobody there: no request keeps the session alive
    // admins change the categories and see it at once; for the others (their map costs the server what each of
    // its items costs to check) once a minute is enough
    if (Date.now() - st.autoAt < (st.data.is_admin ? 3000 : OTHERS_MS - 2000)) return;
    st.autoAt = Date.now();
    load(true);
  }
  function focusOptions() {
    var sel = $("map-focus"), keep = sel.value;
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "", text: "Everything" }));
    st.data.categories.forEach(function (c) {
      var vals = st.data.values.filter(function (v) { return v.facet === c.name; })
        .sort(function (a, b) { return a.value.localeCompare(b.value); });
      if (!vals.length) return;
      sel.appendChild(el("optgroup", { label: catLabel(c.name, 2) + " (" + c.count + ")" }, vals.map(function (v) {
        return el("option", { value: String(v.id), text: v.value }); })));
    });
    sel.value = keep && st.byId[keep] ? keep : "";
    st.focus = sel.value;
  }
  /* the parts shown: every part (the hidden ones aside), or the part in focus with what it is part of, what is
     part of it and what it interacts with */
  function shownIds() {
    var ids = {};
    if (st.focus && st.byId[st.focus]) {
      var f = +st.focus, todo = [f];
      ids[f] = true;
      while (todo.length) {                                   // what it is part of, all the way up
        var v = st.byId[todo.pop()];
        (v.parents || []).forEach(function (p) { if (st.byId[p] && !ids[p]) { ids[p] = true; todo.push(p); } });
      }
      var level = [f];
      for (var depth = 0; depth < 2; depth++) {               // what is part of it, two levels down
        var nxt = [];
        level.forEach(function (i) { (st.kids[i] || []).forEach(function (k) { if (!ids[k]) { ids[k] = true; nxt.push(k); } }); });
        level = nxt;
      }
      st.data.links.forEach(function (x) { if (x.a === f) ids[x.b] = true; if (x.b === f) ids[x.a] = true; });
      return ids;
    }
    st.data.values.forEach(function (v) { if (st.hidden.indexOf(v.id) < 0) ids[v.id] = true; });
    return ids;
  }

  // ------------------------------------------------------------------ the layout: columns, an order with few crossings
  function layout(ids) {
    var cats = st.data.categories.map(function (c) { return c.name; });
    var cols = {}, nodes = {};
    Object.keys(ids).forEach(function (i) {
      var v = st.byId[i];
      (cols[v.facet] = cols[v.facet] || []).push(v);
    });
    var C = colors(), titleFont = "600 13.5px " + C.font, textFont = "12px " + C.font;
    var none = !!st.data.is_admin && !st.focus;              // an admin: a category with no value yet has its column
    cats.forEach(function (c) { if (none && !cols[c]) cols[c] = []; });
    var order = cats.filter(function (c) { return cols[c] && (cols[c].length || none); });
    var folded = {};
    order.forEach(function (c) {
      var fromData = cols[c].filter(function (v) { return v.source === "data"; }).length * 2 >= cols[c].length;
      var big = !st.focus && (cols[c].length > FOLD_ALWAYS || (cols[c].length > FOLD_AT && fromData));
      folded[c] = big && (st.open[c] === undefined ? st.folded[c] !== false : !st.open[c]);
    });
    var rankOf = {};                                          // a part's place in its column (the previous pass)
    order.forEach(function (c, ci) {
      var list = cols[c];
      list.sort(function (a, b) {
        var pa = bary(a, rankOf), pb = bary(b, rankOf);
        if (pa !== pb) return pa - pb;
        return (b.items || 0) - (a.items || 0) || a.value.localeCompare(b.value);
      });
      list.forEach(function (v, i) { rankOf[v.id] = ci * 10000 + i; });
    });
    for (var pass = 0; pass < 2; pass++) {                    // up again: a column by what is part of it
      order.slice(0, -1).reverse().forEach(function (c) {
        var ci = order.indexOf(c);
        cols[c].sort(function (a, b) { return down(a, rankOf) - down(b, rankOf); });
        cols[c].forEach(function (v, i) { rankOf[v.id] = ci * 10000 + i; });
      });
    }
    var heads = [];
    order.forEach(function (c, ci) {
      var x = PAD + ci * (BOX_W + COL_GAP), y = PAD + HEAD_H;
      heads.push({ cat: c, x: x, n: cols[c].length, color: C.series[st.data.categories.map(function (k) { return k.name; }).indexOf(c) % 8] });
      if (!cols[c].length) {                                  // no value yet: said in its column
        nodes["none:" + c] = { id: "none:" + c, none: c, x: x, y: y, w: BOX_W, h: 58 };
        return;
      }
      if (folded[c]) {
        var names = cols[c].slice(0, 6).map(function (v) { return v.value; }).join(", ") + (cols[c].length > 6 ? "…" : "");
        var lines = wrap(names, textFont, BOX_W - 24, 3);
        var h = 52 + lines.length * LINE;
        nodes["cat:" + c] = { id: "cat:" + c, fold: c, x: x, y: y, w: BOX_W, h: h, title: [catLabel(c, cols[c].length) + " · " +
          S.num(cols[c].length)], lines: lines, members: cols[c].map(function (v) { return v.id; }) };
        cols[c].forEach(function (v) { nodes[v.id] = { alias: "cat:" + c }; });
        return;
      }
      cols[c].forEach(function (v) {
        var title = wrap(v.value, titleFont, BOX_W - 24, 2);
        var about = v.description ? wrap(v.description, textFont, BOX_W - 24, 3) :
          v.hint ? wrap("“" + v.hint.text + "”", "italic " + textFont, BOX_W - 24, 3) : [];
        var items = v.gone ? "its items are gone" : v.items ? S.num(v.items) + " item" + (v.items > 1 ? "s" : "") : "no item yet";
        var h = 16 + title.length * 17 + (about.length ? 4 + about.length * LINE : 0) + (v.hint && !v.description ? LINE : 0) + 22;
        var p = st.positions[v.id];
        nodes[v.id] = { id: v.id, v: v, x: p ? p[0] : x, y: p ? p[1] : y, w: BOX_W, h: h, title: title, about: about,
                        items: items, hintFrom: v.hint && !v.description ? v.hint.from : null, moved: !!p };
        y += h + GAP;
      });
    });
    return { nodes: nodes, heads: heads, folded: folded };
  }
  function bary(v, rankOf) {
    var ps = (v.parents || []).filter(function (p) { return rankOf[p] !== undefined; });
    if (!ps.length) return 1e9;
    return ps.reduce(function (s, p) { return s + rankOf[p] % 10000; }, 0) / ps.length;
  }
  function down(v, rankOf) {
    var ks = (st.kids[v.id] || []).filter(function (k) { return rankOf[k] !== undefined; });
    if (!ks.length) return rankOf[v.id] % 10000;
    return ks.reduce(function (s, k) { return s + rankOf[k] % 10000; }, 0) / ks.length;
  }
  function real(nodes, id) {                                  // a folded part: its category's box
    var n = nodes[id];
    return n && n.alias ? nodes[n.alias] : n;
  }

  // ------------------------------------------------------------------ drawing
  function draw() {
    var box = $("map-canvas");
    box.innerHTML = "";
    var C = colors();
    if (!st.data.values.length) {
      box.appendChild(el("div", { class: "map-empty" }, [el("h3", { text: "No part of the system yet" }),
        el("p", { class: "muted", text: "The map draws the approved values of the categories (Knowledge → Categories): " +
          "subjects, applications, components, your own categories. The daily learning proposes them from the data and the " +
          "texts; an admin approves them or adds them by hand." })]));
      $("map-legend").innerHTML = "";
      return;
    }
    var ids = shownIds(), L = layout(ids);
    st.layout = L;
    var root = svg("svg", { class: "map-svg", role: "img", "aria-label": "System map" });
    var defs = svg("defs", {}, [
      svg("marker", { id: "map-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "8", markerHeight: "8",
                      orient: "auto-start-reverse" }, [svg("path", { d: "M0,0 L10,5 L0,10 z", fill: C.accent })])]);
    root.appendChild(defs);
    var vp = svg("g", { class: "map-viewport" });
    root.appendChild(vp);
    var gHeads = svg("g", {}), gEdges = svg("g", {}), gLinks = svg("g", {}), gNodes = svg("g", {});
    [gHeads, gEdges, gLinks, gNodes].forEach(function (g) { vp.appendChild(g); });
    L.heads.forEach(function (h) {
      gHeads.appendChild(svg("rect", { x: h.x, y: PAD, width: BOX_W, height: 4, rx: 2, fill: h.color }));
      var t = svg("text", { x: h.x, y: PAD + 24, fill: C.ink2, "font-family": C.font, "font-size": "13", "font-weight": "600",
                            "letter-spacing": ".04em" });
      t.textContent = catLabel(h.cat, h.n).toUpperCase() + "  ·  " + S.num(h.n);
      gHeads.appendChild(t);
    });
    var drawn = {};
    st.data.values.forEach(function (v) {                      // what each part is part of
      if (!ids[v.id]) return;
      (v.parents || []).forEach(function (p) {
        if (!ids[p]) return;
        var a = real(L.nodes, p), b = real(L.nodes, v.id);
        if (!a || !b || a === b) return;
        var key = a.id + ">" + b.id, isNew = !!(st.fresh && st.fresh.pairs[p + ">" + v.id]);
        if (drawn[key]) { drawn[key].n++; drawn[key].fresh = drawn[key].fresh || isNew; return; }
        drawn[key] = { a: a, b: b, n: 1, ids: [p, v.id], fresh: isNew };
      });
    });
    Object.keys(drawn).forEach(function (k) {
      var e = drawn[k], path = curve(e.a, e.b);
      var line = svg("path", { d: path, fill: "none", stroke: C.axis, "stroke-width": e.n > 1 ? "2.5" : "1.5",
                               class: "map-edge" + (e.fresh ? " map-edge-new" : ""), "data-a": String(e.a.id),
                               "data-b": String(e.b.id) });
      line.appendChild(svg("title", {}, [document.createTextNode((e.b.v ? e.b.v.value : e.b.title[0]) + " is part of " +
        (e.a.v ? e.a.v.value : e.a.title[0]) + (e.n > 1 ? " (" + e.n + " parts)" : ""))]));
      gEdges.appendChild(line);
    });
    var placed = [];                                           // the labels drawn: the next ones avoid them and the boxes
    var boxes = Object.keys(L.nodes).map(function (k) { return L.nodes[k]; }).filter(function (n) { return !n.alias; });
    var clear = function (x, y, w) {
      return !placed.some(function (r) { return Math.abs(r.y - y) < 20 && x < r.x + r.w + 6 && r.x < x + w + 6; }) &&
        !boxes.some(function (n) { return x < n.x + n.w + 4 && n.x < x + w + 4 && y - 11 < n.y + n.h && n.y < y + 11; });
    };
    var free = function (mx, my, w) {                         // on the curve, else beside it; then below, above, further...
      var xs = [mx - w / 2, mx + 4, mx - w - 4];
      for (var tries = 0; tries < 13; tries++) {
        var y = my + (tries % 2 ? 1 : -1) * 22 * Math.ceil(tries / 2);
        for (var i = 0; i < xs.length; i++) {
          if (clear(xs[i], y, w)) { placed.push({ x: xs[i], y: y, w: w }); return { x: xs[i], y: y }; }
        }
      }
      placed.push({ x: xs[0], y: my, w: w });
      return { x: xs[0], y: my };
    };
    st.data.links.forEach(function (x) {                       // the interactions an admin drew
      if (!ids[x.a] || !ids[x.b]) return;
      var a = real(L.nodes, x.a), b = real(L.nodes, x.b);
      if (!a || !b || a === b) return;
      var path = curve(a, b, true);
      var g = svg("g", { class: "map-link", "data-a": String(a.id), "data-b": String(b.id) });
      g.appendChild(svg("path", { d: path.d, fill: "none", stroke: C.accent, "stroke-width": "2", "marker-end": "url(#map-arrow)" }));
      var label = x.label + (x.note ? ": " + x.note : "");
      var short = label.length > 34 ? label.slice(0, 33) + "…" : label;
      measure.font = "11.5px " + C.font;
      var w = measure.measureText(short).width + 10;
      var at = free(path.mx, path.my, w);
      g.appendChild(svg("rect", { x: at.x, y: at.y - 9, width: w, height: 18, rx: 9, fill: C.panel,
                                  stroke: C.accent, "stroke-width": "1" }));
      var t = svg("text", { x: at.x + w / 2, y: at.y + 4, "text-anchor": "middle", fill: C.accent, "font-family": C.font,
                            "font-size": "11.5" });
      t.textContent = short;
      g.appendChild(t);
      g.appendChild(svg("title", {}, [document.createTextNode((a.v ? a.v.value : "") + " " + label + " " + (b.v ? b.v.value : ""))]));
      gLinks.appendChild(g);
    });
    Object.keys(L.nodes).forEach(function (k) {
      var n = L.nodes[k];
      if (n.alias) return;
      gNodes.appendChild(n.none ? noneView(n, C) : nodeView(n, C));
    });
    box.appendChild(root);
    st.svg = root;
    st.vp = vp;
    apply();
    legend(C);
    if (st.sel && L.nodes[st.sel]) select(st.sel, true);
  }
  function curve(a, b, interaction) {
    var ax, ay = a.y + a.h / 2, bx, by = b.y + b.h / 2;
    if (b.x > a.x + a.w / 2) { ax = a.x + a.w; bx = b.x; }
    else if (b.x + b.w < a.x + a.w / 2) { ax = a.x; bx = b.x + b.w; }
    else { ax = a.x + a.w; bx = b.x + b.w; }                   // the same column: around its right side
    var same = Math.abs(a.x - b.x) < a.w / 2, dx = same ? 70 + Math.abs(ay - by) * 0.12 : Math.max(40, Math.abs(bx - ax) / 2);
    var c1x = same ? ax + dx : ax + (bx > ax ? dx : -dx), c2x = same ? bx + dx : bx - (bx > ax ? dx : -dx);
    var d = "M" + ax + "," + ay + " C" + c1x + "," + ay + " " + c2x + "," + by + " " + bx + "," + by;
    if (!interaction) return d;
    return { d: d, mx: (ax + 3 * c1x + 3 * c2x + bx) / 8, my: (ay + by) / 2 };
  }
  function noneView(n, C) {                                   // a category with no value yet (admins): where to add one
    var g = svg("g", { class: "map-none", transform: "translate(" + n.x + "," + n.y + ")", tabindex: "0", role: "button",
                       "aria-label": catLabel(n.none, 2) + ": no value yet, add one in Categories" });
    g.appendChild(svg("rect", { width: n.w, height: n.h, rx: 8, fill: "none", stroke: C.axis, "stroke-width": "1.2",
                                "stroke-dasharray": "5 4", "pointer-events": "all" }));     // the whole box takes the click
    var t = svg("text", { x: 14, y: 24, fill: C.muted, "font-family": C.font, "font-size": "12.5" });
    t.textContent = "No value yet";
    g.appendChild(t);
    var a = svg("text", { x: 14, y: 43, fill: C.accent, "font-family": C.font, "font-size": "12", "font-weight": "600" });
    a.textContent = "Add one in Categories";
    g.appendChild(a);
    var go = function () {
      D.show("categories");
      var sel = $("fac-new-facet"), inp = $("fac-new-value");
      if (sel && sel.querySelector('option[value="' + n.none + '"]')) sel.value = n.none;
      if (inp) inp.focus();
    };
    g.addEventListener("click", go);
    g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
    return g;
  }
  function nodeView(n, C) {
    var catIdx = st.data.categories.map(function (k) { return k.name; }).indexOf(n.fold || n.v.facet);
    var color = C.series[(catIdx < 0 ? 0 : catIdx) % 8];
    var isNew = !!st.fresh && (n.fold ? n.members.some(function (i) { return st.fresh.ids[i]; }) : !!st.fresh.ids[n.id]);
    var g = svg("g", { class: "map-node" + (n.fold ? " map-fold" : "") + (n.v && n.v.gone ? " map-gone" : "") + (isNew ? " map-new" : ""),
                       transform: "translate(" + n.x + "," + n.y + ")", "data-id": String(n.id), tabindex: "0",
                       role: "button", "aria-label": n.fold ? n.title[0] + ": open" : n.v.value });
    g.appendChild(svg("rect", { width: n.w, height: n.h, rx: 8, fill: C.panel, stroke: n.v && n.v.gone ? C.warn : C.line,
                                "stroke-width": "1.2", "stroke-dasharray": n.v && n.v.gone ? "5 4" : null }));
    g.appendChild(svg("rect", { width: 5, height: n.h, rx: 2.5, fill: color }));
    var y = 22;
    n.title.forEach(function (line) {
      var t = svg("text", { x: 14, y: y, fill: C.ink, "font-family": C.font, "font-size": "13.5", "font-weight": "600" });
      t.textContent = line;
      g.appendChild(t);
      y += 17;
    });
    if (n.fold) {
      n.lines.forEach(function (line) {
        var t = svg("text", { x: 14, y: y + 2, fill: C.ink2, "font-family": C.font, "font-size": "12" });
        t.textContent = line; g.appendChild(t); y += LINE;
      });
      var o = svg("text", { x: 14, y: n.h - 10, fill: C.accent, "font-family": C.font, "font-size": "12", "font-weight": "600" });
      o.textContent = "Open: every one";
      g.appendChild(o);
    } else {
      if (n.about.length) y += 4;
      n.about.forEach(function (line) {
        var t = svg("text", { x: 14, y: y, fill: C.ink2, "font-family": C.font, "font-size": "12",
                              "font-style": n.v.description ? null : "italic" });
        t.textContent = line; g.appendChild(t); y += LINE;
      });
      if (n.hintFrom) {
        var f = svg("text", { x: 14, y: y, fill: C.muted, "font-family": C.font, "font-size": "11" });
        f.textContent = "from " + (n.hintFrom.length > 34 ? n.hintFrom.slice(0, 33) + "…" : n.hintFrom);
        g.appendChild(f); y += LINE;
      }
      var it = svg("text", { x: 14, y: n.h - 10, fill: n.v.gone ? C.warn : C.muted, "font-family": C.font, "font-size": "11.5" });
      it.textContent = n.items;
      g.appendChild(it);
    }
    g.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (g._dragged) { g._dragged = false; return; }
      if (n.fold) { st.open[n.fold] = true; draw(); return; }
      if (st.from && st.from !== n.id) { connectTo(n.id); return; }
      select(n.id);
    });
    g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); g.dispatchEvent(new MouseEvent("click")); } });
    if (st.edit && !n.fold) dragBox(g, n);
    return g;
  }
  function legend(C) {
    var box = $("map-legend");
    box.innerHTML = "";
    st.data.categories.forEach(function (c, i) {
      box.appendChild(el("span", { class: "map-key" }, [el("i", { style: "background:" + C.series[i % 8] }),
        document.createTextNode(catLabel(c.name, c.count) + " " + S.num(c.count))]));
    });
    box.appendChild(el("span", { class: "map-key" }, [el("i", { class: "line" }), document.createTextNode("is part of")]));
    if (st.data.links.length) box.appendChild(el("span", { class: "map-key" }, [el("i", { class: "arrow" }),
      document.createTextNode("interaction")]));
    if (st.data.values.some(function (v) { return v.gone; })) box.appendChild(el("span", { class: "map-key" }, [
      el("i", { class: "gone" }), document.createTextNode("its items are gone")]));
    Object.keys(st.layout.folded).forEach(function (c) {
      if (!st.focus && (st.layout.folded[c] || st.open[c])) {
        box.appendChild(el("button", { type: "button", class: "chip", text: (st.layout.folded[c] ? "Open " : "Fold ") +
          catLabel(c, 2).toLowerCase(), onclick: function () { st.open[c] = !!st.layout.folded[c]; draw(); } }));
      }
    });
    if (st.focus) box.appendChild(el("button", { type: "button", class: "chip", text: "Show everything",
      onclick: function () { $("map-focus").value = ""; st.focus = ""; draw(); fit(true); } }));
    if (st.data.proposed) box.appendChild(el("button", { type: "button", class: "chip",
      title: "The map draws the approved values: a proposed one comes once approved",
      text: S.num(st.data.proposed) + " proposed value" + (st.data.proposed > 1 ? "s wait" : " waits") + " in To review",
      onclick: function () { D.show("review"); } }));
    if (st.hidden.length && st.data.is_admin) box.appendChild(el("button", { type: "button", class: "chip",
      text: "Show the " + st.hidden.length + " hidden", onclick: function () { st.hidden = []; saveLayout(); draw(); } }));
  }

  // ------------------------------------------------------------------ the side panel: a part, what it does, its edits
  function select(id, quiet) {
    st.sel = id;
    var n = st.layout.nodes[id];
    st.svg.querySelectorAll(".map-node, .map-edge, .map-link").forEach(function (x) { x.classList.remove("dim", "on"); });
    if (!n || n.alias) { $("map-side").hidden = true; return; }
    var near = {}; near[id] = true;
    st.svg.querySelectorAll(".map-edge, .map-link").forEach(function (x) {
      var touches = x.dataset.a === String(id) || x.dataset.b === String(id);
      x.classList.toggle("on", touches);
      x.classList.toggle("dim", !touches);
      if (touches) { near[x.dataset.a] = true; near[x.dataset.b] = true; }
    });
    st.svg.querySelectorAll(".map-node").forEach(function (x) { x.classList.toggle("dim", !near[x.dataset.id]); });
    var g = st.svg.querySelector('.map-node[data-id="' + id + '"]');
    if (g) g.classList.add("on");
    side(n.v, quiet);
    if (!quiet) inView(n);
  }
  /* the part a panel is about stays in sight (the panel narrows the map) */
  function inView(n) {
    var c = $("map-canvas"), W = c.clientWidth, H = c.clientHeight, k = st.view.k;
    var x0 = n.x * k + st.view.x, y0 = n.y * k + st.view.y, x1 = x0 + n.w * k, y1 = y0 + n.h * k;
    if (x1 > W - 12) st.view.x -= x1 - W + 24;
    if (x0 < 12) st.view.x += 24 - x0;
    if (y1 > H - 12) st.view.y -= y1 - H + 24;
    if (y0 < 12) st.view.y += 24 - y0;
    apply();
  }
  function unselect() {
    st.sel = null; st.from = null;
    if (st.svg) st.svg.querySelectorAll(".dim, .on").forEach(function (x) { x.classList.remove("dim", "on"); });
    $("map-side").hidden = true;
    $("map-canvas").classList.remove("connecting");
  }
  function side(v, quiet) {
    var box = $("map-side"), admin = st.data.is_admin;
    box.innerHTML = "";
    box.hidden = false;
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("div", { class: "muted", text: catLabel(v.facet, 1) }));
    box.appendChild(el("h3", { text: v.value }));
    if ((v.synonyms || []).length) box.appendChild(el("div", { class: "muted", text: "also called " + v.synonyms.join(", ") }));
    if (v.description) box.appendChild(el("p", { text: v.description }));
    else if (v.hint) box.appendChild(el("p", {}, [el("em", { text: "“" + v.hint.text + "”" }),
      el("span", { class: "muted", text: " (from " + v.hint.from + ")" })]));
    else box.appendChild(el("p", { class: "muted", text: "No description yet." }));
    var parts = (v.parents || []).map(function (p) { return st.byId[p]; }).filter(Boolean);
    var kids = (st.kids[v.id] || []).map(function (k) { return st.byId[k]; }).filter(Boolean);
    var list = function (title, vals) {
      if (!vals.length) return null;
      return el("div", { class: "map-rel" }, [el("strong", { text: title + " " }), el("span", {}, vals.slice(0, 40).map(function (x, i) {
        return el("button", { type: "button", class: "linkish", text: x.value + (i < Math.min(vals.length, 40) - 1 ? "," : ""),
          onclick: function () { focusOn(x.id); } }); }).concat(vals.length > 40 ? [document.createTextNode(" … (" + vals.length + ")")] : []))]);
    };
    box.appendChild(list("Part of:", parts));
    box.appendChild(list("Made of:", kids));
    var kinds = Object.keys(v.kinds || {}).map(function (k) { return S.num(v.kinds[k]) + " " + k; });
    box.appendChild(el("div", { class: "muted", text: v.gone ? "Its items are gone (the data or texts it was about no longer exist)." :
      v.items ? S.num(v.items) + " item" + (v.items > 1 ? "s" : "") + " about it: " + kinds.join(", ") : "No item about it yet." }));
    if ((v.origins || []).length) box.appendChild(el("div", { class: "muted small-note", text: "From: " + v.origins.join("; ") }));
    var mine = st.data.links.filter(function (x) { return x.a === v.id || x.b === v.id; });
    if (mine.length) {
      var ul = el("ul", { class: "map-links" });
      mine.forEach(function (x) {
        var other = st.byId[x.a === v.id ? x.b : x.a];
        var text = x.a === v.id ? x.label + " " + (other ? other.value : "?") : (other ? other.value : "?") + " " + x.label + " it";
        ul.appendChild(el("li", {}, [el("span", { text: text + (x.note ? " (" + x.note + ")" : "") }),
          admin ? S.sureButton("×", function () {
            S.dict("POST", "map", { remove_interaction: x.id }).then(function (r) { if (!r.error) load(); });
          }, { cls: "linkish", aria: "Remove this interaction", ask: "Remove “" + text + "”?", yes: "Remove" }) : null]));
      });
      box.appendChild(el("div", { class: "map-rel" }, [el("strong", { text: "Interactions" }), ul]));
    }
    box.appendChild(el("div", { class: "actions" }, [
      el("button", { type: "button", class: "btn small", text: "What touches it", onclick: function () { focusOn(v.id); } }),
      el("a", { class: "btn small", href: "#search", text: "Its knowledge", onclick: function (ev) {
        ev.preventDefault(); D.show("search"); $("k-q").value = v.value; $("k-form").requestSubmit(); } })]));
    if (admin) {
      var ta = el("textarea", { rows: "3", "aria-label": "What it is, what it does", placeholder: "What it is, what it does (a sentence)" });
      ta.value = v.description || "";
      var res = el("span", { class: "result" });
      var acts = [el("button", { type: "button", class: "btn small primary", text: "Save the description", onclick: function () {
        S.dict("POST", "map", { describe: { id: v.id, description: ta.value } }).then(function (r) {
          res.textContent = r.error || "saved"; res.className = "result " + (r.error ? "bad" : "good");
          if (!r.error) { if (D.saved) D.saved(); load(); }
        });
      } })];
      if (!v.description && v.hint) acts.push(el("button", { type: "button", class: "btn small", text: "Use the sentence found",
        onclick: function () { ta.value = v.hint.text.replace(/…$/, ""); ta.focus(); } }));
      acts.push(res);
      box.appendChild(el("details", { class: "map-edit", open: st.edit ? "open" : null }, [el("summary", { text: "Edit (admins)" }),
        ta, el("div", { class: "actions" }, acts),
        el("div", { class: "actions" }, [
          el("button", { type: "button", class: "btn small", text: "Draw an interaction from here", onclick: function () {
            st.from = v.id;
            $("map-canvas").classList.add("connecting");
            res.className = "result"; res.textContent = "now click the part it interacts with (Escape: cancel)";
          } }),
          el("button", { type: "button", class: "btn small", text: "Hide from the map", onclick: function () {
            if (st.hidden.indexOf(v.id) < 0) st.hidden.push(v.id);
            saveLayout(); unselect(); draw();
          } })])]));
    }
    if (!quiet) box.scrollTop = 0;
  }
  function focusOn(id) {
    $("map-focus").value = String(id);
    st.focus = String(id);
    st.sel = id;
    draw();
    fit(true);
  }
  function connectTo(id) {
    var a = st.byId[st.from], b = st.byId[id];
    st.from = null;
    $("map-canvas").classList.remove("connecting");
    if (!a || !b) return;
    var box = $("map-side");
    box.hidden = false;
    box.innerHTML = "";
    var kind = el("select", { "aria-label": "Kind of interaction" }, Object.keys(st.data.interactions).map(function (k) {
      return el("option", { value: k, text: st.data.interactions[k] }); }));
    var note = el("input", { type: "text", maxlength: "500", placeholder: "In a few words (optional): what, how often…", "aria-label": "Note" });
    var res = el("span", { class: "result" });
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("h3", { text: "A new interaction" }));
    box.appendChild(el("p", {}, [el("strong", { text: a.value }), document.createTextNode(" "), kind,
      document.createTextNode(" "), el("strong", { text: b.value })]));
    box.appendChild(note);
    box.appendChild(el("div", { class: "actions" }, [el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () {
      S.dict("POST", "map", { interaction: { a: a.id, b: b.id, kind: kind.value, note: note.value } }).then(function (r) {
        if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
        if (D.saved) D.saved();
        st.sel = a.id;
        load();
      });
    } }), el("button", { type: "button", class: "btn small", text: "Cancel", onclick: unselect }), res]));
    kind.focus();
  }

  // ------------------------------------------------------------------ moving, panning, zooming
  function apply() {
    if (st.vp) st.vp.setAttribute("transform", "translate(" + st.view.x + "," + st.view.y + ") scale(" + st.view.k + ")");
  }
  function bbox() {
    var ns = st.layout ? Object.keys(st.layout.nodes).map(function (k) { return st.layout.nodes[k]; }).filter(function (n) { return !n.alias; }) : [];
    if (!ns.length) return { x: 0, y: 0, w: 400, h: 300 };
    var x0 = Math.min.apply(null, ns.map(function (n) { return n.x; })), y0 = 0;
    var x1 = Math.max.apply(null, ns.map(function (n) { return n.x + n.w; })) + 90;
    var y1 = Math.max.apply(null, ns.map(function (n) { return n.y + n.h; }));
    return { x: x0 - PAD, y: y0, w: x1 - x0 + 2 * PAD, h: y1 + PAD };
  }
  /* Fit: the whole map in the window; the first view: as wide as the window at most (readable), from the top */
  function fit(readable) {
    if (!st.svg) return;
    var b = bbox(), W = $("map-canvas").clientWidth || 800, H = $("map-canvas").clientHeight || 560;
    var k = readable ? Math.max(0.55, Math.min(1, W / b.w)) : Math.max(0.15, Math.min(1.2, Math.min(W / b.w, H / b.h)));
    st.view = { k: k, x: Math.max(0, (W - b.w * k) / 2) - b.x * k,
                y: readable ? -b.y * k : Math.max(0, (H - b.h * k) / 2) - b.y * k };
    apply();
  }
  function wire() {
    var c = $("map-canvas"), drag = null;
    c.addEventListener("wheel", function (ev) {
      if (!st.svg) return;
      ev.preventDefault();
      var r = c.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
      var k = Math.max(0.15, Math.min(2.5, st.view.k * Math.exp(-ev.deltaY * 0.0015)));
      st.view.x = mx - (mx - st.view.x) * k / st.view.k;
      st.view.y = my - (my - st.view.y) * k / st.view.k;
      st.view.k = k;
      apply();
    }, { passive: false });
    c.addEventListener("pointerdown", function (ev) {
      if (ev.target.closest && ev.target.closest(".map-node, .map-none")) return;      // a box: its own click
      drag = { x: ev.clientX, y: ev.clientY, vx: st.view.x, vy: st.view.y, moved: false };
      c.setPointerCapture(ev.pointerId);
    });
    c.addEventListener("pointermove", function (ev) {
      if (!drag) return;
      if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 3) drag.moved = true;
      st.view.x = drag.vx + ev.clientX - drag.x;
      st.view.y = drag.vy + ev.clientY - drag.y;
      apply();
    });
    c.addEventListener("pointerup", function () {
      if (drag && !drag.moved) unselect();
      drag = null;
    });
    c.addEventListener("keydown", function (ev) {
      var step = 60;
      if (ev.key === "Escape") { unselect(); return; }
      if (ev.key === "+" || ev.key === "=") st.view.k = Math.min(2.5, st.view.k * 1.15);
      else if (ev.key === "-") st.view.k = Math.max(0.15, st.view.k / 1.15);
      else if (ev.key === "ArrowLeft") st.view.x += step;
      else if (ev.key === "ArrowRight") st.view.x -= step;
      else if (ev.key === "ArrowUp") st.view.y += step;
      else if (ev.key === "ArrowDown") st.view.y -= step;
      else return;
      ev.preventDefault();
      apply();
    });
  }
  function dragBox(g, n) {
    var start = null;
    g.addEventListener("pointerdown", function (ev) {
      ev.stopPropagation();
      start = { x: ev.clientX, y: ev.clientY, nx: n.x, ny: n.y };
      g.setPointerCapture(ev.pointerId);
    });
    g.addEventListener("pointermove", function (ev) {
      if (!start) return;
      var dx = (ev.clientX - start.x) / st.view.k, dy = (ev.clientY - start.y) / st.view.k;
      if (Math.abs(dx) + Math.abs(dy) < 3 && !g._dragged) return;
      g._dragged = true;
      n.x = Math.round(start.nx + dx); n.y = Math.round(start.ny + dy);
      g.setAttribute("transform", "translate(" + n.x + "," + n.y + ")");
    });
    g.addEventListener("pointerup", function () {
      if (start && g._dragged) {
        st.positions[n.id] = [n.x, n.y];
        saveLayout();
        var keep = st.sel;
        draw();
        if (keep) select(keep, true);
      }
      start = null;
    });
  }
  function saveLayout() {
    clearTimeout(st.saveTimer);
    st.saveTimer = setTimeout(function () {
      S.dict("POST", "map", { layout: { positions: st.positions, hidden: st.hidden, folded: st.folded } });
    }, 600);
  }

  // ------------------------------------------------------------------ exports: SVG, PNG, PDF
  function standalone() {
    var C = colors(), b = bbox(), title = "System map";
    var copy = st.svg.cloneNode(true);
    copy.querySelectorAll(".dim").forEach(function (x) { x.classList.remove("dim"); });
    copy.querySelectorAll(".map-new, .map-edge-new").forEach(function (x) { x.classList.remove("map-new", "map-edge-new"); });
    copy.querySelectorAll(".map-none").forEach(function (x) { x.remove(); });     // "add one": of the page, not of a file
    copy.setAttribute("xmlns", NS);
    copy.setAttribute("width", String(Math.ceil(b.w)));
    copy.setAttribute("height", String(Math.ceil(b.h + 40)));
    copy.setAttribute("viewBox", b.x + " " + (b.y - 40) + " " + b.w + " " + (b.h + 40));
    copy.querySelector(".map-viewport").removeAttribute("transform");
    var bg = svg("rect", { x: b.x, y: b.y - 40, width: b.w, height: b.h + 40, fill: C.panel });
    copy.insertBefore(bg, copy.firstChild.nextSibling);
    var t = svg("text", { x: b.x + PAD, y: b.y - 12, fill: C.ink, "font-family": C.font, "font-size": "16", "font-weight": "700" });
    t.textContent = title + (st.focus && st.byId[st.focus] ? ": around " + st.byId[st.focus].value : "");
    copy.appendChild(t);
    copy.querySelectorAll(".map-node").forEach(function (g) { g.removeAttribute("tabindex"); g.removeAttribute("role"); });
    return { text: new XMLSerializer().serializeToString(copy), w: b.w, h: b.h + 40 };
  }
  function download(blob, name) {
    var a = el("a", { href: URL.createObjectURL(blob), download: name });
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function png(scale) {
    var s = standalone();
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        var k = Math.min(scale || 2, 8000 / Math.max(s.w, s.h));
        st.pngScale = k;
        var cv = document.createElement("canvas");
        cv.width = Math.ceil(s.w * k); cv.height = Math.ceil(s.h * k);
        var ctx = cv.getContext("2d");
        ctx.scale(k, k);
        ctx.drawImage(img, 0, 0);
        cv.toBlob(function (b) { if (b) resolve(b); else reject(new Error("the picture could not be made")); }, "image/png");
      };
      img.onerror = function () { reject(new Error("the picture could not be made")); };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(s.text);
    });
  }
  function busy(btn, p) {
    btn.disabled = true;
    return p.then(function () { btn.disabled = false; }, function (e) { btn.disabled = false; btn.title = String(e.message || e); });
  }
  $("map-svg").addEventListener("click", function () {
    if (!st.svg) return;
    download(new Blob([standalone().text], { type: "image/svg+xml" }), "system-map.svg");
  });
  $("map-png").addEventListener("click", function () {
    if (st.svg) busy(this, png(2).then(function (b) { download(b, "system-map.png"); }));
  });
  $("map-pdf").addEventListener("click", function () {
    if (!st.svg) return;
    var btn = this;
    busy(btn, png(2).then(function (b) {
      return new Promise(function (resolve) { var r = new FileReader(); r.onload = function () { resolve(r.result); }; r.readAsDataURL(b); });
    }).then(function (dataUrl) {
      var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
      return fetch(document.body.dataset.dictionaryApi + "map/export.pdf", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ png: dataUrl, scale: st.pngScale || 2,
                               title: "System map" + (st.focus && st.byId[st.focus] ? ": around " + st.byId[st.focus].value : "") })
      }).then(function (r) { if (!r.ok) throw new Error("PDF: HTTP " + r.status); return r.blob(); });
    }).then(function (blob) { download(blob, "system-map.pdf"); }));
  });
  $("map-fit").addEventListener("click", function () { fit(false); });
  $("map-focus").addEventListener("change", function () { st.focus = this.value; unselect(); draw(); fit(true); });
  $("map-edit").addEventListener("click", function () {
    st.edit = !st.edit;
    this.classList.toggle("on", st.edit);
    this.setAttribute("aria-pressed", String(st.edit));
    $("map-canvas").classList.toggle("editing", st.edit);
    draw();
  });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && st.from) { st.from = null; $("map-canvas").classList.remove("connecting"); } });
  if (!S.isAdmin) $("map-edit").hidden = true;
  wire();
  D.register("map", load);
  setInterval(auto, AUTO_MS);
  /* someone is at the page: a key, the pointer, the wheel, the window coming back (after a pause, read at once) */
  function input() {
    var was = Date.now() - st.inputAt > IDLE_MS;
    st.inputAt = Date.now();
    if (was) auto();
  }
  ["pointerdown", "keydown", "wheel", "touchstart"].forEach(function (name) {
    document.addEventListener(name, input, { passive: true, capture: true });
  });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") { st.inputAt = Date.now(); auto(); } });
  window.addEventListener("focus", function () { st.inputAt = Date.now(); auto(); });
  window.supagentMap = { load: load, data: function () { return st.data; }, png: png, standalone: standalone,
                         idleFor: function (ms) { st.inputAt = Date.now() - ms; } };       // (the tests: nobody there)
})();
