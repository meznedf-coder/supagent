/* supagent Data dictionary: the System map (0.8). The architecture of the system as the categories describe it: one
   column per category (subjects, applications, components, the deployment's own), a box per part with what it does
   (its description, else a sentence of the documents that name it) and its items, a grey line from a part to what
   it is part of, the interactions an admin drew as arrows. A category folds into one box and opens again with a
   click on its name (a big one starts folded); each viewer chooses the categories shown (the legend, or the cross
   in a category's name: kept in the browser); the map can take the whole screen. Admins: Edit to move the boxes
   and the categories (drag a category's name), put categories in a group drawn around them (display only), draw an
   interaction (a box, then another), write what a part is, hide a part.
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
  /* What this viewer sees: `cats` the categories chosen (none: every category; kept in the browser), `parts` the
     parts chosen (none: every part of those categories; each chosen part comes with what it is part of, what it
     is made of and what it interacts with). Both are chosen in the box above the map; the legend and the cross
     in a category's name change `cats` too. */
  var st = { data: null, edit: false, open: {}, sel: null, from: null, view: { x: 0, y: 0, k: 1 },
             boxes: {}, saveTimer: null, fitted: false, seq: 0, sig: "", fresh: null, freshTimer: null, autoAt: 0, note: "", freshAt: -1, freshCats: [], inputAt: Date.now(),
             cols: {}, groups: [], cats: readCats(), parts: [], full: false, pick: null };
  var CATS_KEY = "supagent.map.categories";      // the categories this viewer chose (this browser only)
  var GROUP_PAD = 12, GROUP_HEAD = 22, PICK_PARTS = 80;
  function readCats() {
    try {
      var v = JSON.parse(window.localStorage.getItem("supagent.map.categories") || "[]");
      return Array.isArray(v) ? v.map(String) : [];
    } catch (e) { return []; }                    // no storage (a private window): the choice lasts as long as the page
  }
  function saveCats() {
    try { window.localStorage.setItem(CATS_KEY, JSON.stringify(st.cats)); } catch (e) { /* kept for this page only */ }
  }
  function isOff(c) { return st.cats.length > 0 && st.cats.indexOf(c) < 0; }
  function allCats() { return st.data.categories.map(function (c) { return c.name; }); }
  /* a category shown or hidden: with none chosen every one is shown, so hiding one chooses all the others; the
     last one cannot be hidden (the choice is emptied: everything again) */
  function showCat(c, on) {
    var all = allCats(), now = st.cats.length ? st.cats.filter(function (x) { return all.indexOf(x) >= 0; }) : all.slice();
    if (on && now.indexOf(c) < 0) now.push(c);
    if (!on) now = now.filter(function (x) { return x !== c; });
    st.cats = !now.length || now.length === all.length ? [] : all.filter(function (x) { return now.indexOf(x) >= 0; });
    saveCats();
    syncPick();
  }
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
  function signature(d) { return JSON.stringify([d.values, d.links, d.categories, d.proposed, d.proposed_interactions, d.missing, d.layout && [d.layout.positions, d.layout.hidden, d.layout.folded, d.layout.columns, d.layout.groups]]); }
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
      st.cols = lay.columns || {};
      st.groups = lay.groups || [];
      st.cats = st.cats.filter(function (c) { return allCats().indexOf(c) >= 0; });   // a category removed since
      st.parts = st.parts.filter(function (i) { return !!st.byId[i]; });
      syncPick();
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
  /* The box above the map: the categories and the parts to show, several at once, found by typing. Nothing
     chosen: everything. A part with parts says how many (choose it to see them). */
  function pickLabel(c) { return catLabel(c.name, 2) + " (" + S.num(c.count) + ")"; }
  function pickLoad(words) {
    var q = words.toLowerCase(), items = [], more = 0;
    st.data.categories.forEach(function (c) {
      if (!c.count) return;
      var label = catLabel(c.name, 2);
      if (!q || label.toLowerCase().indexOf(q) >= 0 || c.name.indexOf(q) >= 0) {
        items.push({ id: "cat:" + c.name, label: label, group: "Categories", cls: "cat", hint: S.num(c.count) + " part" + (c.count > 1 ? "s" : "") });
      }
    });
    var n = 0;
    st.data.categories.forEach(function (c) {
      st.data.values.filter(function (v) { return v.facet === c.name; })
        .sort(function (a, b) { return a.value.localeCompare(b.value); }).forEach(function (v) {
          if (q && v.value.toLowerCase().indexOf(q) < 0 &&
              !(v.synonyms || []).some(function (x) { return String(x).toLowerCase().indexOf(q) >= 0; })) return;
          if (++n > PICK_PARTS) { more++; return; }
          var kids = (st.kids[v.id] || []).length;
          items.push({ id: String(v.id), label: v.value, group: catLabel(c.name, 2), hint: kids ? S.num(kids) + " part" + (kids > 1 ? "s" : "") : "" });
        });
    });
    return { items: items, more: more };
  }
  function pickChosen() {
    var by = {};
    st.data.categories.forEach(function (c) { by[c.name] = c; });
    return st.cats.filter(function (c) { return by[c]; }).map(function (c) {
      return { id: "cat:" + c, label: catLabel(c, 2), group: "Categories", cls: "cat" }; })
      .concat(st.parts.filter(function (i) { return st.byId[i]; }).map(function (i) {
        return { id: String(i), label: st.byId[i].value, group: catLabel(st.byId[i].facet, 2) }; }));
  }
  function syncPick() {
    if (!st.data) return;
    if (!st.pick) {
      st.pick = S.picker({ load: pickLoad, chosen: pickChosen(), placeholder: "Everything: choose categories or parts…",
        label: "Show: categories and parts", wait: 0, onchange: function (chosen) {
          var all = allCats();
          var cats = chosen.filter(function (x) { return String(x.id).indexOf("cat:") === 0; }).map(function (x) { return String(x.id).slice(4); });
          st.cats = cats.length === all.length ? [] : all.filter(function (c) { return cats.indexOf(c) >= 0; });
          st.parts = chosen.filter(function (x) { return String(x.id).indexOf("cat:") !== 0; }).map(function (x) { return +x.id; });
          saveCats();
          unselect();
          draw();
          fit(true);
        } });
      var host = $("map-focus");
      host.innerHTML = "";
      host.appendChild(st.pick.el);
    } else st.pick.set(pickChosen());
  }
  /* the parts shown: every part of the categories chosen (the hidden ones aside); with parts chosen, each of them
     with what it is part of, what is part of it and what it interacts with */
  function shownIds() {
    var ids = {};
    if (st.parts.length) {
      st.parts.forEach(function (f) {
        if (!st.byId[f]) return;
        var todo = [f];
        ids[f] = true;
        while (todo.length) {                                 // what it is part of, all the way up
          var v = st.byId[todo.pop()];
          (v.parents || []).forEach(function (p) { if (st.byId[p] && !ids[p]) { ids[p] = true; todo.push(p); } });
        }
        var level = [f];
        for (var depth = 0; depth < 2; depth++) {             // what is part of it, two levels down
          var nxt = [];
          level.forEach(function (i) { (st.kids[i] || []).forEach(function (k) { if (!ids[k]) { ids[k] = true; nxt.push(k); } }); });
          level = nxt;
        }
        st.data.links.forEach(function (x) { if (x.a === f) ids[x.b] = true; if (x.b === f) ids[x.a] = true; });
      });
      Object.keys(ids).forEach(function (i) {                 // of the categories chosen (a chosen part is always shown)
        if (st.parts.indexOf(+i) < 0 && isOff(st.byId[i].facet)) delete ids[i];
      });
      return ids;
    }
    st.data.values.forEach(function (v) { if (st.hidden.indexOf(v.id) < 0 && !isOff(v.facet)) ids[v.id] = true; });
    return ids;
  }
  /* the categories in the order of their columns: the wider first, the members of a group side by side (at the
     place of its first one) */
  function groupOf(c) {
    for (var i = 0; i < st.groups.length; i++) if ((st.groups[i].categories || []).indexOf(c) >= 0) return st.groups[i];
    return null;
  }
  function catOrder() {
    var cats = st.data.categories.map(function (c) { return c.name; }), out = [], used = {};
    cats.forEach(function (c) {
      if (used[c]) return;
      var g = groupOf(c);
      (g ? cats.filter(function (x) { return g.categories.indexOf(x) >= 0; }) : [c]).forEach(function (x) {
        if (!used[x]) { used[x] = true; out.push(x); }
      });
    });
    return out;
  }

  // ------------------------------------------------------------------ the layout: columns, an order with few crossings
  function layout(ids) {
    var cats = catOrder();
    var cols = {}, nodes = {};
    Object.keys(ids).forEach(function (i) {
      var v = st.byId[i];
      (cols[v.facet] = cols[v.facet] || []).push(v);
    });
    var C = colors(), titleFont = "600 13.5px " + C.font, textFont = "12px " + C.font;
    var none = !!st.data.is_admin && !st.parts.length;       // an admin: a category with no value yet has its column
    cats.forEach(function (c) { if (none && !cols[c] && !isOff(c)) cols[c] = []; });
    var order = cats.filter(function (c) { return cols[c] && (cols[c].length || none); });
    /* the place of each column when every category is shown: a box an admin placed there keeps its place next to
       its column when a viewer shows fewer categories (the columns close up) */
    var has = {};
    st.data.values.forEach(function (v) { if (st.hidden.indexOf(v.id) < 0) has[v.facet] = true; });
    var full = cats.filter(function (c) { return has[c] || !!st.data.is_admin; });
    var shift = {};
    order.forEach(function (c, ci) {
      var fi = full.indexOf(c);
      shift[c] = fi < 0 ? 0 : (ci - fi) * (BOX_W + COL_GAP);
    });
    var folded = {};
    order.forEach(function (c) {
      var fromData = cols[c].filter(function (v) { return v.source === "data"; }).length * 2 >= cols[c].length;
      var big = cols[c].length > FOLD_ALWAYS || (cols[c].length > FOLD_AT && fromData);
      var usual = st.folded[c] === undefined ? big : !!st.folded[c];      // as an admin left it, else a big one is folded
      folded[c] = !st.parts.length && cols[c].length > 0 && (st.open[c] === undefined ? usual : !st.open[c]);
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
      var off = st.cols[c] || [0, 0];                         // where an admin moved the category
      var x = PAD + ci * (BOX_W + COL_GAP) + (+off[0] || 0), top = PAD + (+off[1] || 0), y = top + HEAD_H;
      var info = st.data.categories.filter(function (k) { return k.name === c; })[0] || {};
      heads.push({ cat: c, x: x, y: top, n: cols[c].length, folded: !!folded[c], shift: shift[c], about: info.about || "",
                   fields: info.fields || "",
                   color: C.series[st.data.categories.map(function (k) { return k.name; }).indexOf(c) % 8] });
      if (!cols[c].length) {                                  // no value yet: said in its column
        nodes["none:" + c] = { id: "none:" + c, none: c, cat: c, x: x, y: y, w: BOX_W, h: 58 };
        return;
      }
      if (folded[c]) {
        var names = cols[c].slice(0, 6).map(function (v) { return v.value; }).join(", ") + (cols[c].length > 6 ? "…" : "");
        var lines = wrap(names, textFont, BOX_W - 24, 3);
        var h = 36 + lines.length * LINE;
        nodes["cat:" + c] = { id: "cat:" + c, fold: c, cat: c, x: x, y: y, w: BOX_W, h: h, title: [catLabel(c, cols[c].length) + " · " +
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
        nodes[v.id] = { id: v.id, v: v, cat: c, x: p ? p[0] + shift[c] : x, y: p ? p[1] : y, w: BOX_W, h: h, title: title, about: about,
                        items: items, hintFrom: v.hint && !v.description ? v.hint.from : null, moved: !!p };
        y += h + GAP;
      });
    });
    /* the groups an admin made (display only): a frame around the columns of their categories that are shown */
    var frames = [];
    st.groups.forEach(function (g, gi) {
      var mine = heads.filter(function (h) { return (g.categories || []).indexOf(h.cat) >= 0; });
      if (!mine.length) return;
      var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      mine.forEach(function (h) {
        x0 = Math.min(x0, h.x); y0 = Math.min(y0, h.y); x1 = Math.max(x1, h.x + BOX_W); y1 = Math.max(y1, h.y + HEAD_H);
      });
      Object.keys(nodes).forEach(function (k) {
        var n = nodes[k];
        if (n.alias || (g.categories || []).indexOf(n.cat) < 0) return;
        x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y - HEAD_H); x1 = Math.max(x1, n.x + n.w); y1 = Math.max(y1, n.y + n.h);
      });
      frames.push({ name: g.name, index: gi, x: x0 - GROUP_PAD, y: y0 - GROUP_HEAD, w: x1 - x0 + 2 * GROUP_PAD,
                    h: y1 - y0 + GROUP_HEAD + GROUP_PAD });
    });
    return { nodes: nodes, heads: heads, folded: folded, frames: frames, shift: shift };
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
    var gFrames = svg("g", {}), gHeads = svg("g", {}), gEdges = svg("g", {}), gLinks = svg("g", {}), gNodes = svg("g", {});
    [gFrames, gEdges, gLinks, gNodes, gHeads].forEach(function (g) { vp.appendChild(g); });
    L.frames.forEach(function (f) {                           // a group of categories: a frame around their columns
      var g = svg("g", { class: "map-group", "data-group": String(f.index) });
      g.appendChild(svg("rect", { x: f.x, y: f.y, width: f.w, height: f.h, rx: 12, fill: C.bg, stroke: C.axis,
                                  "stroke-width": "1.2", "stroke-dasharray": "6 4" }));
      var t = svg("text", { x: f.x + 12, y: f.y + 15, fill: C.ink2, "font-family": C.font, "font-size": "12",
                            "font-weight": "700", "letter-spacing": ".06em" });
      t.textContent = String(f.name || "").toUpperCase();
      g.appendChild(t);
      gFrames.appendChild(g);
    });
    L.heads.forEach(function (h) { gHeads.appendChild(headView(h, C)); });
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
    st.data.links.forEach(function (x) {                       // the interactions an admin drew
      if (!ids[x.a] || !ids[x.b]) return;
      var a = real(L.nodes, x.a), b = real(L.nodes, x.b);
      if (!a || !b || a === b) return;
      var path = curve(a, b, true);
      // no text on the graph: the arrow, and a wider invisible line to click; a click shows what it is (its short
      // explanation) and, folded, what to do when following it
      var g = svg("g", { class: "map-link", "data-a": String(a.id), "data-b": String(b.id), "data-link": String(x.id),
                         tabindex: "0", role: "button",
                         "aria-label": (a.v ? a.v.value : "") + " " + x.label + " " + (b.v ? b.v.value : "") + ": details" });
      g.appendChild(svg("path", { d: path.d, fill: "none", stroke: "transparent", "stroke-width": "12", class: "map-link-hit",
                                  "pointer-events": "stroke" }));
      g.appendChild(svg("path", { d: path.d, fill: "none", stroke: C.accent, "stroke-width": "2", "marker-end": "url(#map-arrow)" }));
      g.appendChild(svg("title", {}, [document.createTextNode((a.v ? a.v.value : "") + " " + x.label + " " + (b.v ? b.v.value : ""))]));
      var open = function (ev) { if (ev) { ev.stopPropagation(); ev.preventDefault(); } linkPanel(x); };
      g.addEventListener("click", open);
      g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") open(ev); });
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
  /* The name of a category on the map: a click folds its parts into one box or opens them again; its cross hides
     the category for this viewer (the legend shows it again); in Edit an admin drags it to move the category. */
  function headView(h, C) {
    var label = catLabel(h.cat, h.n);
    var g = svg("g", { class: "map-head" + (h.folded ? " folded" : ""), transform: "translate(" + h.x + "," + h.y + ")",
                       "data-cat": h.cat, tabindex: "0", role: "button", "aria-expanded": String(!h.folded),
                       "aria-label": label + ", " + S.num(h.n) + (h.n ? (h.folded ? ": open" : ": fold into one box") : "") });
    g.appendChild(svg("rect", { x: -6, y: -4, width: BOX_W + 12, height: HEAD_H - 2, rx: 6, fill: C.panel, "fill-opacity": "0",
                                "pointer-events": "all", class: "map-head-hit" }));
    g.appendChild(svg("rect", { x: 0, y: 0, width: BOX_W, height: 4, rx: 2, fill: h.color }));
    if (h.n) g.appendChild(svg("path", { d: h.folded ? "M2,13 L8,18 L2,23 z" : "M0,15 L10,15 L5,21 z", fill: C.ink2, class: "map-chevron" }));
    var t = svg("text", { x: h.n ? 16 : 0, y: 24, fill: C.ink2, "font-family": C.font, "font-size": "13", "font-weight": "600",
                          "letter-spacing": ".04em" });
    t.textContent = label.toUpperCase() + "  ·  " + S.num(h.n);
    g.appendChild(t);
    if (h.about) {                                            // what the category is, in a line under its name
      measure.font = "11px " + C.font;
      var line = h.about;
      while (measure.measureText(line).width > BOX_W - 4 && line.length > 4) line = line.slice(0, -2);
      var sub = svg("text", { x: 0, y: 37, fill: C.muted, "font-family": C.font, "font-size": "11", class: "map-about" });
      sub.textContent = line === h.about ? line : line.replace(/\s*\S*$/, "") + "…";
      g.appendChild(sub);
    }
    g.appendChild(svg("title", {}, [document.createTextNode((h.about ? label + ": " + h.about + "\n" : "") +
      (h.n ? (h.folded ? "Open: every " + label.toLowerCase() : "Fold into one box") +
      (st.edit ? " (drag: move the category)" : "") : label))]));
    var about = svg("g", { class: "map-info", transform: "translate(" + (BOX_W - 42) + ",8)", tabindex: "0", role: "button",
                           "aria-label": "What " + label.toLowerCase() + " are" + (st.data.is_admin ? ": read or write it" : "") });
    about.appendChild(svg("rect", { x: -3, y: -3, width: 22, height: 22, rx: 5, fill: C.panel, "fill-opacity": "0", "pointer-events": "all" }));
    about.appendChild(svg("circle", { cx: 8, cy: 8, r: 7, fill: "none", stroke: C.muted, "stroke-width": "1.4" }));
    about.appendChild(svg("path", { d: "M8,7 L8,12 M8,4 L8,4.6", stroke: C.muted, "stroke-width": "1.8", "stroke-linecap": "round", fill: "none" }));
    about.appendChild(svg("title", {}, [document.createTextNode("What this category is")]));
    var openAbout = function (ev) { ev.stopPropagation(); ev.preventDefault(); categoryPanel(h.cat); };
    about.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });
    about.addEventListener("click", openAbout);
    about.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") openAbout(ev); });
    g.appendChild(about);
    var hide = svg("g", { class: "map-hide", transform: "translate(" + (BOX_W - 18) + ",8)", tabindex: "0", role: "button",
                          "aria-label": "Hide " + label.toLowerCase() + " (the legend shows them again)" });
    hide.appendChild(svg("rect", { x: -3, y: -3, width: 22, height: 22, rx: 5, fill: C.panel, "fill-opacity": "0", "pointer-events": "all" }));
    hide.appendChild(svg("path", { d: "M3,3 L13,13 M13,3 L3,13", stroke: C.muted, "stroke-width": "1.8", "stroke-linecap": "round", fill: "none" }));
    hide.appendChild(svg("title", {}, [document.createTextNode("Hide this category (the legend shows it again)")]));
    var hideIt = function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      showCat(h.cat, false);
      unselect();
      draw();
    };
    hide.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });
    hide.addEventListener("click", hideIt);
    hide.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") hideIt(ev); });
    g.appendChild(hide);
    var toggle = function () {
      if (!h.n) return;
      st.open[h.cat] = h.folded;                              // folded: open it; open: fold it
      if (st.edit && st.data.is_admin) { st.folded[h.cat] = !h.folded; saveLayout(); }   // an admin: for everyone
      unselect();
      draw();
    };
    g.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (g._dragged) { g._dragged = false; return; }
      toggle();
    });
    g.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    g.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });        // not a pan of the map
    if (st.edit && st.data.is_admin) dragColumn(g, h);
    return g;
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
    var g = svg("g", { class: "map-none", transform: "translate(" + n.x + "," + n.y + ")", "data-cat": n.cat, tabindex: "0", role: "button",
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
                       transform: "translate(" + n.x + "," + n.y + ")", "data-id": String(n.id), "data-cat": n.cat, tabindex: "0",
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
      g.appendChild(svg("title", {}, [document.createTextNode("Open: every one (a click here, or on the category's name)")]));
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
    /* the categories: each key shows or hides its category on this viewer's map (kept in the browser) */
    st.data.categories.forEach(function (c, i) {
      var on = !isOff(c.name);
      box.appendChild(el("button", { type: "button", class: "map-key map-cat" + (on ? "" : " off"), "aria-pressed": String(on),
        title: on ? "Shown: click to hide this category" : "Hidden: click to show this category",
        onclick: function () { showCat(c.name, !on); unselect(); draw(); } },
        [el("i", { style: "background:" + C.series[i % 8] }),
         document.createTextNode(catLabel(c.name, c.count) + " " + S.num(c.count))]));
    });
    var hiddenCats = st.data.categories.filter(function (c) { return isOff(c.name); });
    if (hiddenCats.length) box.appendChild(el("button", { type: "button", class: "chip", id: "map-all-cats",
      text: "Show the " + (hiddenCats.length > 1 ? hiddenCats.length + " hidden categories" : "hidden category"),
      onclick: function () { st.cats = []; saveCats(); syncPick(); draw(); fit(true); } }));
    box.appendChild(el("span", { class: "map-key" }, [el("i", { class: "line" }), document.createTextNode("is part of")]));
    if (st.data.links.length) box.appendChild(el("span", { class: "map-key" }, [el("i", { class: "arrow" }),
      document.createTextNode("interaction")]));
    if (st.data.values.some(function (v) { return v.gone; })) box.appendChild(el("span", { class: "map-key" }, [
      el("i", { class: "gone" }), document.createTextNode("its items are gone")]));
    if (st.edit && st.data.is_admin) box.appendChild(el("button", { type: "button", class: "chip", id: "map-groups",
      title: "Put categories in a group drawn around them (display only: nothing changes for the agent)",
      text: st.groups.length ? "Groups (" + st.groups.length + ")…" : "Group categories…", onclick: groupsPanel }));
    if (st.parts.length) box.appendChild(el("button", { type: "button", class: "chip", id: "map-all-parts",
      text: "Show every part", title: "The map no longer narrowed to the parts chosen",
      onclick: function () { st.parts = []; syncPick(); draw(); fit(true); } }));
    if (st.data.proposed) box.appendChild(el("button", { type: "button", class: "chip",
      title: "The map draws the approved values: a proposed one comes once approved",
      text: S.num(st.data.proposed) + " proposed value" + (st.data.proposed > 1 ? "s wait" : " waits") + " in To review",
      onclick: function () { D.show("review"); } }));
    if (st.data.proposed_interactions) box.appendChild(el("button", { type: "button", class: "chip",
      title: "Interactions your documents state, read by the LLM: each is drawn once approved",
      text: S.num(st.data.proposed_interactions) + " proposed interaction" + (st.data.proposed_interactions > 1 ? "s wait" : " waits") + " in To review",
      onclick: function () { D.show("review"); } }));
    if ((st.data.missing || []).length) box.appendChild(el("button", { type: "button", class: "chip", id: "map-missing",
      title: "What an investigation follows and does not find yet: parts not in the data, parts with no interaction, joins, usual values",
      text: S.num(st.data.missing.length) + " point" + (st.data.missing.length > 1 ? "s" : "") + " to complete for investigations",
      onclick: missing }));
    if (st.hidden.length && st.data.is_admin) box.appendChild(el("button", { type: "button", class: "chip",
      text: "Show the " + st.hidden.length + " hidden", onclick: function () { st.hidden = []; saveLayout(); draw(); } }));
  }
  /* A category's panel: what it is (an admin writes it; the agent and the router are given it with the parts a
     question names), how many parts it has, the fields its values are read from. */
  function categoryPanel(cat, note) {
    unselect();
    var c = st.data.categories.filter(function (k) { return k.name === cat; })[0];
    if (!c) return;
    var box = $("map-side");
    box.innerHTML = "";
    box.hidden = false;
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("div", { class: "muted", text: "Category" + (c.builtin ? " (built in)" : "") }));
    box.appendChild(el("h3", { text: catLabel(c.name, 2) }));
    box.appendChild(c.about ? el("p", { text: c.about }) : el("p", { class: "muted", text: "Nobody wrote what this category is yet." }));
    box.appendChild(el("div", { class: "muted", text: S.num(c.count) + " part" + (c.count === 1 ? "" : "s") + " on the map" +
      (c.fields ? " · its values are read from the fields " + c.fields : " · its values are added by hand or proposed by the LLM") }));
    var g = groupOf(c.name);
    if (g) box.appendChild(el("div", { class: "muted", text: "Drawn in the group “" + g.name + "”." }));
    if (st.data.is_admin) {
      var ta = el("textarea", { rows: "3", maxlength: "300", "aria-label": "What this category is",
                                placeholder: "What this category is, in a sentence (a pool: a group of servers that share the same queue of slots)" });
      ta.value = c.about || "";
      var res = el("span", { class: "result" + (note ? " good" : ""), text: note || "" });
      box.appendChild(el("div", { class: "map-edit" }, [el("strong", { text: "What it is (admins)" }), ta,
        el("div", { class: "actions" }, [el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () {
          S.dict("POST", "map", { describe_category: { name: c.name, about: ta.value } }).then(function (r) {
            if (r.error) { res.textContent = r.error; res.className = "result bad"; return; }
            if (D.saved) D.saved();
            c.about = r.about || "";                           // at once; the reading below brings the same
            draw();
            categoryPanel(c.name, "saved: people, the agent and the router read it");
            load();
          });
        } }), res])]));
    }
    box.appendChild(el("div", { class: "actions" }, [
      el("button", { type: "button", class: "btn small", text: "Show this category only", onclick: function () {
        st.cats = [c.name]; saveCats(); syncPick(); unselect(); draw(); fit(true); } })]));
  }
  /* Groups of categories (admins, in Edit): a frame drawn around the columns of the categories of a group, which
     are put side by side. Display only: the agent, the categories and their values do not know them. */
  function groupsPanel() {
    unselect();
    var box = $("map-side");
    box.innerHTML = "";
    box.hidden = false;
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("div", { class: "muted", text: "Display" }));
    box.appendChild(el("h3", { text: "Groups of categories" }));
    box.appendChild(el("p", { class: "muted", text: "A group is a frame around the columns of its categories, put side by side (servers, resources and network under “Infrastructure”). It only changes how the map looks." }));
    var cats = st.data.categories.map(function (c) { return c.name; });
    var form = function (g, gi) {
      var name = el("input", { type: "text", maxlength: "40", placeholder: "Name of the group", "aria-label": "Name of the group" });
      name.value = g ? g.name : "";
      var boxes = cats.map(function (c) {
        var cb = el("input", { type: "checkbox", value: c });
        cb.checked = !!g && (g.categories || []).indexOf(c) >= 0;
        var other = groupOf(c);
        return el("label", { class: "map-group-cat" }, [cb, document.createTextNode(" " + catLabel(c, 2) +
          (other && other !== g ? " (in “" + other.name + "”)" : ""))]);
      });
      var res = el("span", { class: "result" });
      var save = el("button", { type: "button", class: "btn small primary", text: g ? "Save" : "Add the group", onclick: function () {
        var chosen = boxes.map(function (l) { return l.firstChild; }).filter(function (cb) { return cb.checked; }).map(function (cb) { return cb.value; });
        var label = name.value.trim();
        if (!label || !chosen.length) { res.textContent = "a name and one category at least"; res.className = "result bad"; return; }
        var next = st.groups.map(function (x, i) {                 // a category is in one group: taken from the others
          return i === gi ? null : { name: x.name, categories: (x.categories || []).filter(function (c) { return chosen.indexOf(c) < 0; }) };
        }).filter(function (x) { return x && x.categories.length; });
        next.splice(g ? Math.min(gi, next.length) : next.length, 0, { name: label, categories: chosen });
        st.groups = next;
        saveLayout(); draw(); fit(true); groupsPanel();
      } });
      var acts = [save];
      if (g) acts.push(el("button", { type: "button", class: "btn small", text: "Remove the group", onclick: function () {
        st.groups = st.groups.filter(function (x, i) { return i !== gi; });
        saveLayout(); draw(); fit(true); groupsPanel();
      } }));
      acts.push(res);
      return el("fieldset", { class: "map-group-form" }, [el("legend", { text: g ? g.name : "A new group" }), name,
        el("div", { class: "map-group-cats" }, boxes), el("div", { class: "actions" }, acts)]);
    };
    st.groups.forEach(function (g, gi) { box.appendChild(form(g, gi)); });
    box.appendChild(form(null, -1));
  }
  /* what the agent would follow in an investigation and does not find yet (admins): the same list as
     "superset supagent check-system" */
  function missing() {
    unselect();
    var box = $("map-side");
    box.innerHTML = "";
    box.hidden = false;
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("div", { class: "muted", text: "For investigations" }));
    box.appendChild(el("h3", { text: "To complete" }));
    box.appendChild(el("p", { class: "muted", text: "An investigation follows the parts of this map, where each is in the data, how the tables join and which field holds a usual value. What it does not find yet:" }));
    var ul = el("ul", { class: "map-links" });
    st.data.missing.forEach(function (x) { ul.appendChild(el("li", {}, [el("span", { text: x })])); });
    box.appendChild(ul);
    box.appendChild(el("p", { class: "muted small-note", text: "The same list, and what the agent is given for a question: superset supagent check-system --question \"...\"" }));
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
    [list("Part of:", parts), list("Made of:", kids)].forEach(function (x) { if (x) box.appendChild(x); });   // (none: no line)
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
        ul.appendChild(el("li", {}, [el("button", { type: "button", class: "linkish", text: text,
                                                    onclick: function () { linkPanel(x); } }),
          x.note ? el("span", { class: "muted", text: ": " + x.note }) : null,
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
  /* An interaction clicked on the map: what it is (its short explanation), what to do when following it (the long
     one, folded), where it was said; admins correct both, or remove it. */
  function linkPanel(x) {
    var a = st.byId[x.a], b = st.byId[x.b], admin = st.data.is_admin;
    if (!a || !b) return;
    st.sel = null;
    st.svg.querySelectorAll(".map-node, .map-edge, .map-link").forEach(function (n) { n.classList.remove("dim", "on"); });
    st.svg.querySelectorAll(".map-node, .map-edge, .map-link").forEach(function (n) {
      var mine = n.classList.contains("map-link") ? n.dataset.link === String(x.id) :
        n.classList.contains("map-node") ? (n.dataset.id === String(x.a) || n.dataset.id === String(x.b)) : false;
      n.classList.toggle("on", mine);
      n.classList.toggle("dim", !mine);
    });
    var box = $("map-side");
    box.innerHTML = "";
    box.hidden = false;
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("div", { class: "muted", text: "Interaction" }));
    box.appendChild(el("h3", {}, [el("button", { type: "button", class: "linkish", text: a.value, onclick: function () { select(a.id); } }),
      document.createTextNode(" " + x.label + " "),
      el("button", { type: "button", class: "linkish", text: b.value, onclick: function () { select(b.id); } })]));
    box.appendChild(x.note ? el("p", { class: "map-link-short", text: x.note }) :
      el("p", { class: "muted", text: "Not explained yet (the next classification writes it, or an admin below)." }));
    if (x.detail) box.appendChild(el("details", { class: "map-link-long" }, [el("summary", { text: "What to do when following it" }),
      el("p", { text: x.detail })]));
    var who = x.explained_by === "llm" ? "Explained by the AI" : x.explained_by ? "Explained by " + x.explained_by : "";
    var from = x.source === "admin" ? "drawn by an admin" : x.source === "llm" ? "read in a text, approved" : x.source ? "from " + x.source : "";
    if (who || from) box.appendChild(el("div", { class: "muted small-note", text: [who, from].filter(Boolean).join(" · ") }));
    if (x.evidence) box.appendChild(el("div", { class: "muted small-note", text: "Said in: " + x.evidence }));
    if (!admin) return;
    var shortIn = el("input", { type: "text", maxlength: "500", value: x.note || "", "aria-label": "Short explanation",
                                placeholder: "In a few words: what it waits for, reads, sends, uses" });
    var longIn = el("textarea", { rows: "4", maxlength: "2000", "aria-label": "Long explanation",
                                  placeholder: "What to do when following it: what to check on the other part, what a problem there does here" });
    longIn.value = x.detail || "";
    var res = el("span", { class: "result" });
    box.appendChild(el("details", { class: "map-edit", open: st.edit ? "open" : null }, [el("summary", { text: "Edit (admins)" }),
      shortIn, longIn,
      el("div", { class: "actions" }, [
        el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () {
          S.dict("POST", "map", { interaction: { id: x.id, a: x.a, b: x.b, kind: x.kind, note: shortIn.value, detail: longIn.value } }).then(function (r) {
            res.textContent = r.error || "saved"; res.className = "result " + (r.error ? "bad" : "good");
            if (!r.error) { if (D.saved) D.saved(); load(); }
          });
        } }),
        S.sureButton("Remove", function () {
          S.dict("POST", "map", { remove_interaction: x.id }).then(function (r) { if (!r.error) { unselect(); load(); } });
        }, { cls: "btn small", aria: "Remove this interaction", ask: "Remove “" + a.value + " " + x.label + " " + b.value + "”?", yes: "Remove" }),
        res])]));
  }
  function focusOn(id) {
    st.parts = [+id];
    syncPick();
    st.sel = id;
    draw();
    fit(true);
  }
  function around() {                                         // the parts the map is narrowed to, for a title
    var names = st.parts.filter(function (i) { return st.byId[i]; }).map(function (i) { return st.byId[i].value; });
    return names.length ? ": around " + names.slice(0, 3).join(", ") + (names.length > 3 ? " and " + (names.length - 3) + " more" : "") : "";
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
    var note = el("input", { type: "text", maxlength: "500", placeholder: "In a few words: what it waits for, reads, sends, uses",
                             "aria-label": "Short explanation" });
    var detail = el("textarea", { rows: "3", maxlength: "2000", "aria-label": "Long explanation",
                                  placeholder: "What to do when following it (optional): what to check on the other part, what a problem there does here" });
    var res = el("span", { class: "result" });
    box.appendChild(el("button", { type: "button", class: "close", "aria-label": "Close", text: "×", onclick: unselect }));
    box.appendChild(el("h3", { text: "A new interaction" }));
    box.appendChild(el("p", {}, [el("strong", { text: a.value }), document.createTextNode(" "), kind,
      document.createTextNode(" "), el("strong", { text: b.value })]));
    box.appendChild(note);
    box.appendChild(detail);
    box.appendChild(el("div", { class: "actions" }, [el("button", { type: "button", class: "btn small primary", text: "Save", onclick: function () {
      S.dict("POST", "map", { interaction: { a: a.id, b: b.id, kind: kind.value, note: note.value, detail: detail.value } }).then(function (r) {
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
    // the boxes, the names of the categories (a category an admin moved up or left), the frames of the groups
    var all = ns.map(function (n) { return { x: n.x, y: n.y, w: n.w, h: n.h }; })
      .concat(st.layout.heads.map(function (h) { return { x: h.x, y: h.y, w: BOX_W, h: HEAD_H }; }))
      .concat(st.layout.frames || []);
    var x0 = Math.min.apply(null, all.map(function (n) { return n.x; }));
    var top = Math.min(0, Math.min.apply(null, all.map(function (n) { return n.y; })) - 8);
    var x1 = Math.max.apply(null, all.map(function (n) { return n.x + n.w; })) + 90;
    var y1 = Math.max.apply(null, all.map(function (n) { return n.y + n.h; }));
    return { x: x0 - PAD, y: top, w: x1 - x0 + 2 * PAD, h: y1 - top + PAD };
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
      if (ev.target.closest && ev.target.closest(".map-node, .map-none, .map-head")) return;   // a box, a category's name: its own click
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
        // kept as on the map with every category shown (a viewer who shows fewer sees the columns closer)
        st.positions[n.id] = [n.x - ((st.layout.shift || {})[n.cat] || 0), n.y];
        saveLayout();
        var keep = st.sel;
        draw();
        if (keep) select(keep, true);
      }
      start = null;
    });
  }
  /* In Edit, the name of a category is dragged: its boxes follow (the ones an admin placed too), the lines are
     drawn again at the end. A click without a move still folds or opens the category. */
  function dragColumn(g, h) {
    var start = null, moving = [];
    g.addEventListener("pointerdown", function (ev) {
      if (ev.target.closest && ev.target.closest(".map-hide, .map-info")) return;
      start = { x: ev.clientX, y: ev.clientY, dx: 0, dy: 0 };
      moving = Array.prototype.filter.call(st.svg.querySelectorAll(".map-head, .map-node, .map-none"), function (x) {
        return x.getAttribute("data-cat") === h.cat; }).map(function (x) {
        var m = /translate\(([-\d.]+),([-\d.]+)\)/.exec(x.getAttribute("transform") || "");
        return { el: x, x: m ? +m[1] : 0, y: m ? +m[2] : 0 };
      });
      g.setPointerCapture(ev.pointerId);
    });
    g.addEventListener("pointermove", function (ev) {
      if (!start) return;
      var dx = Math.round((ev.clientX - start.x) / st.view.k), dy = Math.round((ev.clientY - start.y) / st.view.k);
      if (Math.abs(dx) + Math.abs(dy) < 3 && !g._dragged) return;
      g._dragged = true;
      start.dx = dx; start.dy = dy;
      moving.forEach(function (m) { m.el.setAttribute("transform", "translate(" + (m.x + dx) + "," + (m.y + dy) + ")"); });
    });
    g.addEventListener("pointerup", function () {
      if (start && g._dragged) {
        var off = st.cols[h.cat] || [0, 0];
        st.cols[h.cat] = [Math.round((+off[0] || 0) + start.dx), Math.round((+off[1] || 0) + start.dy)];
        st.data.values.forEach(function (v) {                  // the boxes placed by hand keep their place in it
          var p = st.positions[v.id];
          if (v.facet === h.cat && p) st.positions[v.id] = [p[0] + start.dx, p[1] + start.dy];
        });
        saveLayout();
        draw();
      }
      start = null;
    });
  }
  function saveLayout() {
    clearTimeout(st.saveTimer);
    st.saveTimer = setTimeout(function () {
      S.dict("POST", "map", { layout: { positions: st.positions, hidden: st.hidden, folded: st.folded,
                                         columns: st.cols, groups: st.groups } });
    }, 600);
  }
  /* The map on the whole screen: the browser's full screen when it allows it, else the whole window. */
  function setFull(on) {
    var box = $("sub-map"), btn = $("map-full");
    st.full = on;
    box.classList.toggle("map-full", on);
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.textContent = on ? "Exit full screen" : "Full screen";
    var p = null;
    if (on && box.requestFullscreen && !document.fullscreenElement) p = box.requestFullscreen();
    else if (!on && document.fullscreenElement && document.exitFullscreen) p = document.exitFullscreen();
    if (p && p.catch) p.catch(function () { /* refused: the map still takes the whole window */ });
    setTimeout(function () { if (st.svg) fit(true); }, 80);
  }

  // ------------------------------------------------------------------ exports: SVG, PNG, PDF
  function standalone() {
    var C = colors(), b = bbox(), title = "System map";
    var copy = st.svg.cloneNode(true);
    copy.querySelectorAll(".dim").forEach(function (x) { x.classList.remove("dim"); });
    copy.querySelectorAll(".map-new, .map-edge-new").forEach(function (x) { x.classList.remove("map-new", "map-edge-new"); });
    copy.querySelectorAll(".map-none, .map-hide, .map-info, .map-head-hit").forEach(function (x) { x.remove(); });   // the page's buttons: not of a file
    copy.querySelectorAll(".map-head").forEach(function (g) { g.removeAttribute("tabindex"); g.removeAttribute("role"); g.removeAttribute("aria-expanded"); });
    copy.setAttribute("xmlns", NS);
    copy.setAttribute("width", String(Math.ceil(b.w)));
    copy.setAttribute("height", String(Math.ceil(b.h + 40)));
    copy.setAttribute("viewBox", b.x + " " + (b.y - 40) + " " + b.w + " " + (b.h + 40));
    copy.querySelector(".map-viewport").removeAttribute("transform");
    var bg = svg("rect", { x: b.x, y: b.y - 40, width: b.w, height: b.h + 40, fill: C.panel });
    copy.insertBefore(bg, copy.firstChild.nextSibling);
    var t = svg("text", { x: b.x + PAD, y: b.y - 12, fill: C.ink, "font-family": C.font, "font-size": "16", "font-weight": "700" });
    t.textContent = title + around();
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
                               title: "System map" + around() })
      }).then(function (r) { if (!r.ok) throw new Error("PDF: HTTP " + r.status); return r.blob(); });
    }).then(function (blob) { download(blob, "system-map.pdf"); }));
  });
  $("map-fit").addEventListener("click", function () { fit(false); });
  $("map-edit").addEventListener("click", function () {
    st.edit = !st.edit;
    this.classList.toggle("on", st.edit);
    this.setAttribute("aria-pressed", String(st.edit));
    $("map-canvas").classList.toggle("editing", st.edit);
    draw();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    if (st.from) { st.from = null; $("map-canvas").classList.remove("connecting"); return; }
    if (st.full && !document.fullscreenElement) setFull(false);       // the whole window (no real full screen): back
  });
  $("map-full").addEventListener("click", function () { setFull(!st.full); });
  document.addEventListener("fullscreenchange", function () {
    if (!document.fullscreenElement && st.full) setFull(false);       // Escape, or the browser's own way out
    else if (st.svg) setTimeout(function () { fit(true); }, 80);
  });
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
                         layout: function () { return st.layout; }, view: function () { return st.view; },
                         idleFor: function (ms) { st.inputAt = Date.now() - ms; } };       // (the tests: nobody there)
})();
