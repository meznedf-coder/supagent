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
             boxes: {}, saveTimer: null, fitted: false };

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
  function catLabel(name, n) {
    var s = name.charAt(0).toUpperCase() + name.slice(1);
    return n === 1 ? s : s + (/s$/.test(s) ? "" : "s");
  }

  // ------------------------------------------------------------------ what is shown
  function load() {
    return S.dict("GET", "map").then(function (d) {
      if (d.error) { $("map-canvas").textContent = d.error; return; }
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
    });
  }
  function focusOptions() {
    var sel = $("map-focus"), keep = sel.value;
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "", text: "Everything" }));
    st.data.categories.forEach(function (c) {
      var vals = st.data.values.filter(function (v) { return v.facet === c.name; })
        .sort(function (a, b) { return a.value.localeCompare(b.value); });
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
    var order = cats.filter(function (c) { return cols[c] && cols[c].length; });
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
        var key = a.id + ">" + b.id;
        if (drawn[key]) { drawn[key].n++; return; }
        drawn[key] = { a: a, b: b, n: 1, ids: [p, v.id] };
      });
    });
    Object.keys(drawn).forEach(function (k) {
      var e = drawn[k], path = curve(e.a, e.b);
      var line = svg("path", { d: path, fill: "none", stroke: C.axis, "stroke-width": e.n > 1 ? "2.5" : "1.5",
                               class: "map-edge", "data-a": String(e.a.id), "data-b": String(e.b.id) });
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
      gNodes.appendChild(nodeView(n, C));
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
  function nodeView(n, C) {
    var catIdx = st.data.categories.map(function (k) { return k.name; }).indexOf(n.fold || n.v.facet);
    var color = C.series[(catIdx < 0 ? 0 : catIdx) % 8];
    var g = svg("g", { class: "map-node" + (n.fold ? " map-fold" : "") + (n.v && n.v.gone ? " map-gone" : ""),
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
      if (ev.target.closest && ev.target.closest(".map-node")) return;
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
  window.supagentMap = { load: load, data: function () { return st.data; }, png: png, standalone: standalone };
})();
