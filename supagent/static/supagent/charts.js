/* supagent result views: the rows of a query as a table or a chart (SVG drawn here: no
   external library, Superset's CSP allows none), with copy, CSV, Excel and PNG.
   Chart forms: a line per series over time, horizontal bars for a ranking, figures for a
   single row. One value axis only; a legend for two series or more; hover and keyboard
   tooltips; colours from the page's validated palette (--s1 ... --s8, light and dark). */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var TIME_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/;
  var MAX_SERIES = 8, MAX_BARS = 40, TABLE_ROWS = 500;

  // ------------------------------------------------------------------ helpers
  function css(name) { return getComputedStyle(document.body).getPropertyValue(name).trim(); }
  function palette() { var p = []; for (var i = 1; i <= MAX_SERIES; i++) p.push(css("--s" + i) || "#2a78d6"); return p; }
  function svg(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(e);
    return e;
  }
  function text(parent, x, y, s, attrs) {
    var t = svg("text", Object.assign({ x: x, y: y }, attrs || {}), parent);
    t.textContent = s;
    return t;
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function parseTime(v) {
    if (typeof v !== "string") return NaN;
    var s = v.trim().replace(" ", "T");
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) s += "T00:00:00";
    return Date.parse(s);            // no zone: the browser's local time, as Superset shows it
  }
  function fmt(v, compact) {
    if (!isNum(v)) return v === null || v === undefined ? "" : String(v);
    var a = Math.abs(v);
    if (compact && a >= 1e3) {
      var u = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
      for (var i = 0; i < u.length; i++) if (a >= u[i][0]) return (v / u[i][0]).toLocaleString(undefined, { maximumFractionDigits: 1 }) + u[i][1];
    }
    return v.toLocaleString(undefined, { maximumFractionDigits: a >= 100 ? 0 : a >= 1 ? 2 : 4 });
  }
  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + (lo === 0 ? 1 : Math.abs(lo) * 0.1); }
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = span / count / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step, ticks = [];
    for (var t = start; t <= end + step / 2; t += step) ticks.push(Math.abs(t) < step / 1e6 ? 0 : t);
    return ticks;
  }
  var measureCtx = document.createElement("canvas").getContext("2d");
  function textWidth(s, size) {
    measureCtx.font = (size || 12) + "px " + (css("--font") || "sans-serif");
    return measureCtx.measureText(s).width;
  }
  function clip(s, px, size) {
    s = String(s);
    if (textWidth(s, size) <= px) return s;
    while (s.length > 1 && textWidth(s + "…", size) > px) s = s.slice(0, -1);
    return s + "…";
  }

  // time ticks on round local boundaries (00:00, 03:00, 06:00...; days; weeks; months)
  var MIN = 60000, HOUR = 60 * MIN, DAY = 24 * HOUR;
  var STEPS = [MIN, 5 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 91 * DAY, 365 * DAY];
  function timeTicks(t0, t1, count) {
    var span = t1 - t0, step = STEPS[STEPS.length - 1];
    for (var i = 0; i < STEPS.length; i++) if (span / STEPS[i] <= count) { step = STEPS[i]; break; }
    var d = new Date(t0), out = [];
    if (step < HOUR) { d.setSeconds(0, 0); d.setMinutes(Math.ceil(d.getMinutes() / (step / MIN)) * (step / MIN)); }
    else if (step < DAY) { d.setMinutes(0, 0, 0); var h = step / HOUR; if (d.getTime() < t0) d.setHours(d.getHours() + 1); d.setHours(Math.ceil(d.getHours() / h) * h); }
    else if (step < 30 * DAY) { d.setHours(0, 0, 0, 0); if (d.getTime() < t0) d.setDate(d.getDate() + 1); if (step === 7 * DAY || step === 14 * DAY) { while (d.getDay() !== 1) d.setDate(d.getDate() + 1); } }
    else { d.setHours(0, 0, 0, 0); d.setDate(1); if (d.getTime() < t0) d.setMonth(d.getMonth() + 1); }
    var guard = 0;
    while (d.getTime() <= t1 && guard++ < 200) {
      out.push(d.getTime());
      if (step < DAY) d = new Date(d.getTime() + step);
      else if (step < 30 * DAY) d.setDate(d.getDate() + step / DAY);
      else d.setMonth(d.getMonth() + (step === 30 * DAY ? 1 : step === 91 * DAY ? 3 : 12));
    }
    return out.length ? out : [t0, t1];
  }

  // ------------------------------------------------------------------ what the rows are
  function analyze(res) {
    var cols = res.columns || [], rows = res.rows || [];
    return cols.map(function (name, i) {
      var n = 0, num = 0, time = 0, distinct = {};
      for (var r = 0; r < rows.length; r++) {
        var v = rows[r][i];
        if (v === null || v === undefined || v === "") continue;
        n++;
        if (isNum(v)) num++;
        else if (typeof v === "string" && TIME_RE.test(v.trim())) time++;
        if (Object.keys(distinct).length <= 1000) distinct[String(v)] = 1;
      }
      var type = !n ? "empty" : num === n ? "number" : time === n ? "time" : "category";
      if (type === "number" && /(^|_|\b)(id|code|year)$/i.test(name) && Object.keys(distinct).length > 1) type = "category";
      if (type === "number" && /^(ts|time|timestamp)$/i.test(name)) type = "category";
      return { name: name, index: i, type: type, distinct: Object.keys(distinct).length };
    });
  }

  function plan(res) {
    var cols = analyze(res), rows = res.rows || [];
    var times = cols.filter(function (c) { return c.type === "time"; });
    var nums = cols.filter(function (c) { return c.type === "number"; });
    var cats = cols.filter(function (c) { return c.type === "category"; });
    if (!rows.length || !nums.length) return { kind: "none", why: rows.length ? "no number column to draw" : "no rows", nums: nums };
    if (times.length && rows.length > 1 && times[0].distinct > 1) {      // one instant: bars, not a line
      var split = cats.filter(function (c) { return c.distinct > 1; })[0] || null;
      return { kind: "line", x: times[0], nums: nums, split: split };
    }
    if (rows.length === 1) return { kind: "figures", nums: nums, cats: cats };
    if (cats.length) return { kind: "bar", cats: cats, nums: nums };
    return { kind: "none", why: "no label column for the bars", nums: nums };
  }

  // ------------------------------------------------------------------ tooltip
  function tooltip(host) {
    var tip = document.createElement("div");
    tip.className = "viz-tip";
    tip.hidden = true;
    host.appendChild(tip);
    return {
      show: function (x, y, title, lines) {
        tip.innerHTML = "";
        var t = document.createElement("div");
        t.className = "viz-tip-title";
        t.textContent = title;
        tip.appendChild(t);
        lines.forEach(function (l) {
          var row = document.createElement("div");
          row.className = "viz-tip-row";
          if (l.color) {
            var key = document.createElement("span");
            key.className = "viz-key";
            key.style.background = l.color;
            row.appendChild(key);
          }
          var val = document.createElement("strong");
          val.textContent = l.value;
          row.appendChild(val);
          if (l.name) {
            var nm = document.createElement("span");
            nm.className = "viz-tip-name";
            nm.textContent = l.name;
            row.appendChild(nm);
          }
          tip.appendChild(row);
        });
        tip.hidden = false;
        var hw = host.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
        tip.style.left = Math.max(4, Math.min(hw - tw - 4, x + 14)) + "px";
        tip.style.top = Math.max(4, y - th - 10) + "px";
      },
      hide: function () { tip.hidden = true; }
    };
  }

  // ------------------------------------------------------------------ legend (inside the SVG: kept in the PNG)
  function legend(g, items, width, y0, kind) {
    var x = 0, y = y0, ink = css("--ink-2");
    items.forEach(function (it) {
      var w = 22 + textWidth(it.name, 12) + 16;
      if (x + w > width && x > 0) { x = 0; y += 18; }
      if (kind === "line") svg("line", { x1: x, x2: x + 14, y1: y - 4, y2: y - 4, stroke: it.color, "stroke-width": 2, "stroke-linecap": "round" }, g);
      else svg("rect", { x: x, y: y - 9, width: 10, height: 10, rx: 2, fill: it.color }, g);
      text(g, x + 20, y, it.name, { fill: ink, "font-size": 12 });
      x += w;
    });
    return y + 12;
  }

  // ------------------------------------------------------------------ line chart
  function drawLine(host, res, p, measure) {
    var rows = res.rows, xi = p.x.index, mi = measure.index;
    var colors = palette(), ink = css("--ink"), muted = css("--muted"), grid = css("--grid"), axis = css("--axis"), surface = css("--panel");
    var groups = {}, order = [];
    rows.forEach(function (r) {
      var t = parseTime(r[xi]);
      if (!isFinite(t)) return;
      var key = p.split ? String(r[p.split.index]) : measure.name;
      if (!groups[key]) { groups[key] = {}; order.push(key); }
      var v = r[mi];
      groups[key][t] = isNum(v) ? (groups[key][t] || 0) + v : groups[key][t];
    });
    var series = order.map(function (k) {
      var pts = Object.keys(groups[k]).map(Number).sort(function (a, b) { return a - b; })
        .map(function (t) { return [t, groups[k][t]]; });
      var peak = pts.reduce(function (m, q) { return isNum(q[1]) ? Math.max(m, Math.abs(q[1])) : m; }, 0);
      return { name: k, pts: pts, peak: peak };
    });
    var hidden = 0;
    if (series.length > MAX_SERIES) {
      series.sort(function (a, b) { return b.peak - a.peak; });
      hidden = series.length - MAX_SERIES;
      series = series.slice(0, MAX_SERIES);
    }
    series.forEach(function (s, i) { s.color = colors[i % colors.length]; });
    var xs = {}, lo = Infinity, hi = -Infinity;
    series.forEach(function (s) { s.pts.forEach(function (q) { xs[q[0]] = 1; if (isNum(q[1])) { lo = Math.min(lo, q[1]); hi = Math.max(hi, q[1]); } }); });
    var xvals = Object.keys(xs).map(Number).sort(function (a, b) { return a - b; });
    if (!xvals.length || !isFinite(lo)) return false;
    if (lo >= 0 && lo < hi * 0.5) lo = 0;
    var W = Math.max(320, host.clientWidth || 640), top = 28, legendH = 0;
    var root = svg("svg", { class: "viz-svg", width: W, role: "img", "aria-label": measure.name + " over time" });
    svg("rect", { x: 0, y: 0, width: W, height: 10, fill: surface, class: "viz-bg" }, root);
    text(root, 0, 16, measure.name + (p.split ? " by " + p.split.name : ""), { fill: ink, "font-size": 13, "font-weight": 600 });
    if (series.length > 1) {
      var lg = svg("g", {}, root);
      legendH = legend(lg, series.map(function (s) { return { name: s.name, color: s.color }; }), W, top + 12, "line") - top;
      if (hidden) text(root, 0, top + legendH + 4, "+ " + hidden + " more series (in the table)", { fill: muted, "font-size": 11 });
      legendH += hidden ? 14 : 4;
    }
    var yt = niceTicks(lo, hi, 5);
    var left = Math.max.apply(null, yt.map(function (t) { return textWidth(fmt(t, true), 11); })) + 12;
    var plotTop = top + legendH + 8, plotH = 240, H = plotTop + plotH + 30, right = 16;
    root.setAttribute("height", H);
    root.setAttribute("viewBox", "0 0 " + W + " " + H);
    root.querySelector(".viz-bg").setAttribute("height", H);
    var x0 = xvals[0], x1 = xvals[xvals.length - 1];
    var sx = function (t) { return left + (x1 === x0 ? (W - left - right) / 2 : (t - x0) / (x1 - x0) * (W - left - right)); };
    var y0 = yt[0], y1 = yt[yt.length - 1];
    var sy = function (v) { return plotTop + plotH - (v - y0) / (y1 - y0 || 1) * plotH; };
    yt.forEach(function (t) {
      svg("line", { x1: left, x2: W - right, y1: sy(t), y2: sy(t), stroke: t === 0 ? axis : grid, "stroke-width": 1 }, root);
      text(root, left - 8, sy(t) + 4, fmt(t, true), { fill: muted, "font-size": 11, "text-anchor": "end" });
    });
    var span = x1 - x0, day = 86400000;
    var fmtX = function (t) {
      var d = new Date(t);
      if (span > 2 * day) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      var hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
      return span > day * 0.99 && d.getHours() === 0 && d.getMinutes() === 0
        ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : hm;
    };
    var nt = Math.max(2, Math.min(8, Math.floor((W - left - right) / 90)));
    timeTicks(x0, x1, nt).forEach(function (t, i, all) {
      var X = sx(t);
      text(root, X, plotTop + plotH + 18, fmtX(t), { fill: muted, "font-size": 11,
        "text-anchor": X - left < 20 ? "start" : W - right - X < 20 ? "end" : "middle" });
    });
    series.forEach(function (s) {
      var d = "", pen = false;
      s.pts.forEach(function (q) {
        if (!isNum(q[1])) { pen = false; return; }
        d += (pen ? "L" : "M") + sx(q[0]).toFixed(1) + " " + sy(q[1]).toFixed(1);
        pen = true;
      });
      svg("path", { d: d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
      var few = s.pts.length <= 24;
      s.pts.forEach(function (q, k) {
        if (!isNum(q[1]) || (!few && k !== s.pts.length - 1)) return;
        svg("circle", { cx: sx(q[0]), cy: sy(q[1]), r: 4, fill: s.color, stroke: surface, "stroke-width": 2 }, root);
      });
    });
    if (series.length === 1) {
      var last = series[0].pts.filter(function (q) { return isNum(q[1]); }).slice(-1)[0];
      if (last) text(root, Math.min(sx(last[0]), W - right), sy(last[1]) - 10, fmt(last[1]), { fill: ink, "font-size": 11, "text-anchor": "end" });
    }
    // crosshair and one tooltip for every series at the nearest time
    var cross = svg("line", { x1: 0, x2: 0, y1: plotTop, y2: plotTop + plotH, stroke: axis, "stroke-width": 1, visibility: "hidden" }, root);
    var hit = svg("rect", { x: left, y: plotTop, width: W - left - right, height: plotH, fill: "transparent", tabindex: 0,
                            "aria-label": "chart: use the arrow keys to read the values" }, root);
    host.appendChild(root);
    var tip = tooltip(host), cur = -1;
    function at(idx) {
      cur = Math.max(0, Math.min(xvals.length - 1, idx));
      var t = xvals[cur], X = sx(t);
      cross.setAttribute("x1", X); cross.setAttribute("x2", X); cross.setAttribute("visibility", "visible");
      var lines = series.map(function (s) {
        var q = s.pts.filter(function (z) { return z[0] === t; })[0];
        return { color: s.color, value: q && isNum(q[1]) ? fmt(q[1]) : "–", name: series.length > 1 ? s.name : measure.name };
      });
      var d = new Date(t);
      var whole = span > 2 * day && d.getHours() === 0 && d.getMinutes() === 0;
      tip.show(X, plotTop + 10, whole ? d.toLocaleDateString(undefined, { dateStyle: "medium" })
        : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit",
                                         minute: "2-digit", hourCycle: "h23" }), lines);
    }
    hit.addEventListener("pointermove", function (ev) {
      var box = root.getBoundingClientRect(), scale = W / box.width;
      var X = (ev.clientX - box.left) * scale, best = 0, dist = Infinity;
      xvals.forEach(function (t, k) { var dd = Math.abs(sx(t) - X); if (dd < dist) { dist = dd; best = k; } });
      at(best);
    });
    hit.addEventListener("pointerleave", function () { cross.setAttribute("visibility", "hidden"); tip.hide(); });
    hit.addEventListener("focus", function () { at(cur < 0 ? xvals.length - 1 : cur); });
    hit.addEventListener("blur", function () { cross.setAttribute("visibility", "hidden"); tip.hide(); });
    hit.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowLeft") { at(cur - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight") { at(cur + 1); ev.preventDefault(); }
    });
    return true;
  }

  // ------------------------------------------------------------------ horizontal bars (rankings)
  function drawBars(host, res, p, measure) {
    var rows = res.rows.filter(function (r) { return isNum(r[measure.index]); });
    var more = Math.max(0, rows.length - MAX_BARS);
    rows = rows.slice(0, MAX_BARS);
    if (!rows.length) return false;
    var colors = palette(), ink = css("--ink"), ink2 = css("--ink-2"), muted = css("--muted"), grid = css("--grid"), axis = css("--axis"), surface = css("--panel");
    var label = function (r) { return p.cats.map(function (c) { return r[c.index] === null ? "(empty)" : String(r[c.index]); }).join(" · "); };
    var W = Math.max(320, host.clientWidth || 640);
    var labelW = Math.min(W * 0.4, Math.max.apply(null, rows.map(function (r) { return textWidth(label(r), 12); })) + 4);
    var vals = rows.map(function (r) { return r[measure.index]; });
    var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
    var ticks = niceTicks(lo, hi, 5), t0 = ticks[0], t1 = ticks[ticks.length - 1];
    var valueW = Math.max.apply(null, vals.map(function (v) { return textWidth(fmt(v), 11); })) + 10;
    var left = labelW + 10, right = valueW + 8, top = 30, band = 28, thick = Math.min(18, band - 8);
    var H = top + rows.length * band + 26 + (more ? 16 : 0);
    var sx = function (v) { return left + (v - t0) / (t1 - t0 || 1) * (W - left - right); };
    var root = svg("svg", { class: "viz-svg", width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": measure.name + " by " + p.cats.map(function (c) { return c.name; }).join(", ") });
    svg("rect", { x: 0, y: 0, width: W, height: H, fill: surface }, root);
    text(root, 0, 16, measure.name + " by " + p.cats.map(function (c) { return c.name; }).join(", "), { fill: ink, "font-size": 13, "font-weight": 600 });
    ticks.forEach(function (t) {
      svg("line", { x1: sx(t), x2: sx(t), y1: top - 4, y2: top + rows.length * band, stroke: t === 0 ? axis : grid, "stroke-width": 1 }, root);
      text(root, sx(t), top + rows.length * band + 16, fmt(t, true), { fill: muted, "font-size": 11, "text-anchor": "middle" });
    });
    var tip = tooltip(host);
    rows.forEach(function (r, i) {
      var v = r[measure.index], y = top + i * band + (band - thick) / 2, xa = sx(Math.min(0, v)), xb = sx(Math.max(0, v));
      var w = Math.max(1, xb - xa), rad = Math.min(4, w, thick / 2), pos = v >= 0;
      var d = pos
        ? "M" + xa + " " + y + "H" + (xb - rad) + "Q" + xb + " " + y + " " + xb + " " + (y + rad) + "V" + (y + thick - rad) + "Q" + xb + " " + (y + thick) + " " + (xb - rad) + " " + (y + thick) + "H" + xa + "Z"
        : "M" + xb + " " + y + "H" + (xa + rad) + "Q" + xa + " " + y + " " + xa + " " + (y + rad) + "V" + (y + thick - rad) + "Q" + xa + " " + (y + thick) + " " + (xa + rad) + " " + (y + thick) + "H" + xb + "Z";
      var g = svg("g", { class: "viz-bar", tabindex: 0, "aria-label": label(r) + ": " + fmt(v) }, root);
      svg("rect", { x: 0, y: top + i * band, width: W, height: band, fill: "transparent" }, g);   // the hit target: the whole band
      svg("path", { d: d, fill: colors[0] }, g);
      text(g, labelW, top + i * band + band / 2 + 4, clip(label(r), labelW, 12), { fill: ink2, "font-size": 12, "text-anchor": "end" });
      text(g, pos ? xb + 6 : xa - 6, top + i * band + band / 2 + 4, fmt(v), { fill: ink, "font-size": 11, "text-anchor": pos ? "start" : "end" });
      var show = function (ev) {
        var box = root.getBoundingClientRect(), scale = box.width / W;
        tip.show(ev && ev.clientX ? ev.clientX - box.left : xb * scale, (top + i * band) * scale, label(r), [{ value: fmt(v), name: measure.name }]);
        g.classList.add("on");
      };
      var hide = function () { tip.hide(); g.classList.remove("on"); };
      g.addEventListener("pointermove", show);
      g.addEventListener("pointerleave", hide);
      g.addEventListener("focus", function () { show(null); });
      g.addEventListener("blur", hide);
    });
    if (more) text(root, 0, H - 4, "+ " + more + " more rows (in the table)", { fill: muted, "font-size": 11 });
    host.appendChild(root);
    return true;
  }

  // ------------------------------------------------------------------ one row: figures
  function drawFigures(host, res, p) {
    var box = document.createElement("div");
    box.className = "viz-figures";
    var row = res.rows[0];
    p.nums.forEach(function (c) {
      var f = document.createElement("div");
      f.className = "viz-figure";
      var l = document.createElement("div");
      l.className = "viz-figure-label";
      l.textContent = c.name;
      var v = document.createElement("div");
      v.className = "viz-figure-value";
      v.textContent = fmt(row[c.index], true);
      v.title = fmt(row[c.index]);
      f.appendChild(l);
      f.appendChild(v);
      box.appendChild(f);
    });
    (p.cats || []).forEach(function (c) {
      var f = document.createElement("div");
      f.className = "viz-figure-note";
      f.textContent = c.name + ": " + (row[c.index] === null ? "" : row[c.index]);
      box.appendChild(f);
    });
    host.appendChild(box);
    return true;
  }

  // ------------------------------------------------------------------ table
  function drawTable(host, res) {
    var cols = analyze(res), rows = res.rows.slice(), sortCol = -1, asc = true, limit = TABLE_ROWS;
    var wrap = document.createElement("div");
    wrap.className = "viz-table-wrap";
    var table = document.createElement("table");
    table.className = "viz-table";
    wrap.appendChild(table);
    var more = document.createElement("button");
    more.type = "button";
    more.className = "btn small";
    function render() {
      table.innerHTML = "";
      var thead = table.createTHead(), hr = thead.insertRow();
      cols.forEach(function (c, i) {
        var th = document.createElement("th");
        th.scope = "col";
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = c.name + (sortCol === i ? (asc ? " ▲" : " ▼") : "");
        b.title = "Sort by " + c.name;
        b.addEventListener("click", function () {
          asc = sortCol === i ? !asc : c.type !== "number";
          sortCol = i;
          rows.sort(function (a, z) {
            var x = a[i], y = z[i];
            if (x === y) return 0;
            if (x === null || x === undefined) return 1;
            if (y === null || y === undefined) return -1;
            return (x < y ? -1 : 1) * (asc ? 1 : -1);
          });
          render();
        });
        th.appendChild(b);
        if (c.type === "number") th.className = "num";
        hr.appendChild(th);
      });
      var tb = table.createTBody();
      rows.slice(0, limit).forEach(function (r) {
        var tr = tb.insertRow();
        cols.forEach(function (c, i) {
          var td = tr.insertCell();
          var v = r[i];
          td.textContent = isNum(v) ? fmt(v) : v === null || v === undefined ? "" : String(v);
          if (c.type === "number") td.className = "num";
        });
      });
      more.hidden = rows.length <= limit;
      more.textContent = "Show all " + rows.length.toLocaleString() + " rows";
    }
    more.addEventListener("click", function () { limit = rows.length; render(); });
    render();
    host.appendChild(wrap);
    host.appendChild(more);
  }

  // ------------------------------------------------------------------ exports
  function csvText(res, sep) {
    var q = function (v) {
      if (v === null || v === undefined) return "";
      var s = String(v);
      return sep === "\t" ? s.replace(/[\t\n\r]/g, " ") : (/[",\n\r;]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s);
    };
    return [res.columns.map(q).join(sep)].concat(res.rows.map(function (r) { return r.map(q).join(sep); })).join("\r\n");
  }
  function download(blob, name) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function png(svgNode, name) {
    var clone = svgNode.cloneNode(true);
    clone.setAttribute("xmlns", NS);
    clone.querySelectorAll("[tabindex]").forEach(function (n) { n.removeAttribute("tabindex"); });
    clone.setAttribute("font-family", css("--font") || "sans-serif");
    var W = +svgNode.getAttribute("width"), H = +svgNode.getAttribute("height"), scale = 2;
    var img = new Image();
    return new Promise(function (resolve, reject) {
      img.onload = function () {
        var c = document.createElement("canvas");
        c.width = W * scale; c.height = H * scale;
        var ctx = c.getContext("2d");
        ctx.scale(scale, scale);
        ctx.drawImage(img, 0, 0, W, H);
        c.toBlob(function (b) { if (b) { download(b, name); resolve(); } else reject(new Error("no image")); }, "image/png");
      };
      img.onerror = function () { reject(new Error("the chart could not be turned into an image")); };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(clone));
    });
  }

  window.supagentViz = {
    plan: plan, drawLine: drawLine, drawBars: drawBars, drawFigures: drawFigures, drawTable: drawTable,
    csvText: csvText, download: download, png: png, fmt: fmt
  };
})();
