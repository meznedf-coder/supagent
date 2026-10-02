/* The chat as a panel on Superset's pages (dashboards, charts, datasets, SQL Lab...): the Chat tab of the top bar
   opens and closes it. Two ways (the button next to the title switches, remembered in this browser):
     docked    on the right; the page narrows beside it and its charts fit the width left; drag the left edge (or
               focus it and use the arrow keys) to make it wider or narrower. It leaves the page PAGE_MIN pixels at
               least (a dashboard's header does not fit in less): in a window too small for both, the chat floats
     floating  a window over the page, moved by its title bar and resized from any edge or corner; the page and its
               charts do not move
   Ctrl / Cmd / Shift / middle click on the tab still opens the full chat page. The panel stays open from page to
   page in this tab. The chat itself is the chat page in a frame of the same site (?embed=1): its links to Superset
   open in this page. */
(function () {
  "use strict";
  var script = document.currentScript;
  var CHAT = (script && script.getAttribute("data-chat")) || "/supagent/";
  var OPEN_KEY = "supagent-dock-open", WIDTH_KEY = "supagent-dock-width", MODE_KEY = "supagent-dock-mode",
      BOX_KEY = "supagent-dock-box";
  var MIN = 320, MIN_H = 320, WIDTH = 440, NARROW = 900, KEEP = 120, PAGE_MIN = 900;
  var dock = null, frame = null, fallback = null, modeBtn = null, hint = null, hintTimer = null;

  function get(area, key) { try { return window[area].getItem(key); } catch (e) { return null; } }
  function put(area, key, value) {
    try { if (value === null) window[area].removeItem(key); else window[area].setItem(key, value); } catch (e) { /* private mode */ }
  }
  function maxWidth() { return Math.max(MIN, Math.min(Math.round(window.innerWidth * 0.7), window.innerWidth - PAGE_MIN)); }
  function canDock() { return window.innerWidth >= PAGE_MIN + MIN; }
  function width() {
    var w = parseInt(get("localStorage", WIDTH_KEY) || "", 10) || WIDTH;
    return Math.min(Math.max(w, MIN), maxWidth());
  }
  function mode() { return get("localStorage", MODE_KEY) === "float" ? "float" : "dock"; }
  function dark() {
    var m = get("localStorage", "superset-theme-mode");
    return m === "dark" || (m !== "default" && !!window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
  var chatPath = new URL(CHAT, location.href).pathname;
  function isChatLink(a) {
    if (!a || !a.getAttribute("href")) return false;
    try {
      var u = new URL(a.getAttribute("href"), location.href);
      return u.origin === location.origin && u.pathname === chatPath && !u.search;
    } catch (e) { return false; }
  }

  /* the floating window's place and size, kept inside the window (a part of its title bar always reachable) */
  function box() {
    var b = null;
    try { b = JSON.parse(get("localStorage", BOX_KEY) || "null"); } catch (e) { b = null; }
    var vw = window.innerWidth, vh = window.innerHeight;
    if (!b || typeof b.w !== "number") b = { w: Math.min(480, vw - 40), h: Math.min(640, vh - 80), x: vw - Math.min(480, vw - 40) - 24, y: 64 };
    b.w = Math.min(Math.max(b.w, MIN), vw);
    b.h = Math.min(Math.max(b.h, MIN_H), vh);
    b.x = Math.min(Math.max(b.x, KEEP - b.w), vw - KEEP);
    b.y = Math.min(Math.max(b.y, 0), vh - 40);
    return b;
  }
  /* the docked width, kept within its bounds; past the widest it may be, a hint says to float it instead */
  function setWidth(w) {
    var max = maxWidth();
    put("localStorage", WIDTH_KEY, String(Math.min(Math.max(w, MIN), max)));
    if (w > max + 12 && hint) {
      hint.textContent = "Wider: float it over the page (\u29c9)";
      hint.hidden = false;
      clearTimeout(hintTimer);
      hintTimer = setTimeout(function () { hint.hidden = true; }, 4000);
    }
  }
  function saveBox(b) { put("localStorage", BOX_KEY, JSON.stringify({ x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.w), h: Math.round(b.h) })); }

  function layout() {
    var open = !!dock && !dock.hidden, narrow = window.innerWidth < NARROW;
    var floating = !narrow && (mode() === "float" || !canDock());
    var w = narrow ? window.innerWidth : width();
    document.documentElement.style.setProperty("--supagent-dock-width", w + "px");
    document.documentElement.classList.toggle("supagent-docked", open && !narrow && !floating);
    if (!dock) return;
    dock.classList.toggle("sd-floating", floating);
    dock.classList.toggle("sd-docked", !floating);
    if (floating) {
      var b = box();
      dock.style.left = b.x + "px"; dock.style.top = b.y + "px";
      dock.style.width = b.w + "px"; dock.style.height = b.h + "px";
    } else {
      dock.style.left = dock.style.top = dock.style.width = dock.style.height = "";
    }
    if (modeBtn) {
      modeBtn.textContent = floating ? "⇥" : "⧉";
      modeBtn.title = floating ? "Dock on the right (the page narrows beside it)" : "Float over the page (move and resize it freely)";
      modeBtn.setAttribute("aria-label", modeBtn.title);
      modeBtn.hidden = narrow || !canDock();
    }
  }
  function relayout() {           // the page's charts fit the width left to them
    layout();
    try { window.dispatchEvent(new Event("resize")); } catch (e) { /* old browsers */ }
  }

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text) e.textContent = text;
    return e;
  }

  /* a pointer drag that the frame does not catch meanwhile */
  function dragging(target, onMove, onEnd) {
    target.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0 || (ev.target.closest && ev.target.closest(".sd-btn"))) return;
      ev.preventDefault();
      target.setPointerCapture(ev.pointerId);
      dock.classList.add("sd-resizing");
      var start = { x: ev.clientX, y: ev.clientY, box: box(), width: width() };
      var move = function (e) { onMove(e.clientX - start.x, e.clientY - start.y, start, e); };
      var up = function () {
        target.removeEventListener("pointermove", move);
        target.removeEventListener("pointerup", up);
        target.removeEventListener("pointercancel", up);
        dock.classList.remove("sd-resizing");
        if (onEnd) onEnd();
      };
      target.addEventListener("pointermove", move);
      target.addEventListener("pointerup", up);
      target.addEventListener("pointercancel", up);
    });
  }

  function build() {
    dock = el("aside", { id: "supagent-dock", "aria-label": "Chat", class: "sd-docked" });
    dock.hidden = true;
    var bar = el("div", { class: "sd-bar", title: "" });
    bar.appendChild(el("span", { class: "sd-title" }, "Chat"));
    hint = el("span", { class: "sd-hint", role: "status" });
    hint.hidden = true;
    bar.appendChild(hint);
    modeBtn = el("button", { class: "sd-btn sd-mode", type: "button" });
    modeBtn.addEventListener("click", function () {
      put("localStorage", MODE_KEY, mode() === "float" ? "dock" : "float");
      relayout();
    });
    bar.appendChild(modeBtn);
    var full = el("a", { class: "sd-btn", href: CHAT, target: "_top", title: "Open the chat as a full page" }, "⤢");
    full.addEventListener("click", function () { put("sessionStorage", OPEN_KEY, null); });
    bar.appendChild(full);
    var close = el("button", { class: "sd-btn", type: "button", title: "Close the chat", "aria-label": "Close the chat" }, "×");
    close.addEventListener("click", hide);
    bar.appendChild(close);
    dock.appendChild(bar);
    // docked: the left edge (a visible handle; arrow keys when focused); floating: the title bar moves it
    var grip = el("div", { class: "sd-grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
                           "aria-label": "Width of the chat (arrow keys)", title: "Drag to make the chat wider or narrower" });
    grip.appendChild(el("span", { class: "sd-grip-dots", "aria-hidden": "true" }));
    dock.appendChild(grip);
    dragging(grip, function (dx, _dy, start) {
      setWidth(start.width - dx);
      layout();
    }, relayout);
    grip.addEventListener("keydown", function (ev) {
      var step = ev.shiftKey ? 80 : 24, w = width();
      if (ev.key === "ArrowLeft") w += step; else if (ev.key === "ArrowRight") w -= step; else return;
      ev.preventDefault();
      setWidth(w);
      relayout();
    });
    dragging(bar, function (dx, dy, start) {
      if (mode() !== "float") return;
      var b = start.box;
      saveBox({ x: b.x + dx, y: b.y + dy, w: b.w, h: b.h });
      layout();
    });
    bar.addEventListener("dblclick", function (ev) {      // floating: as big as the window, or back
      if (mode() !== "float" || (ev.target.closest && ev.target.closest(".sd-btn"))) return;
      var b = box(), vw = window.innerWidth, vh = window.innerHeight;
      var big = b.w >= vw - 40 && b.h >= vh - 40;
      var prev = null;
      try { prev = JSON.parse(get("sessionStorage", BOX_KEY) || "null"); } catch (e) { prev = null; }
      if (big && prev) saveBox(prev);
      else { put("sessionStorage", BOX_KEY, JSON.stringify(b)); saveBox({ x: 12, y: 12, w: vw - 24, h: vh - 24 }); }
      layout();
    });
    ["n", "s", "e", "w", "ne", "nw", "se", "sw"].forEach(function (d) {     // floating: every edge and corner
      var h = el("div", { class: "sd-handle sd-" + d, "aria-hidden": "true" });
      dock.appendChild(h);
      dragging(h, function (dx, dy, start) {
        var b = { x: start.box.x, y: start.box.y, w: start.box.w, h: start.box.h };
        if (d.indexOf("e") >= 0) b.w = Math.max(MIN, start.box.w + dx);
        if (d.indexOf("s") >= 0) b.h = Math.max(MIN_H, start.box.h + dy);
        if (d.indexOf("w") >= 0) { b.w = Math.max(MIN, start.box.w - dx); b.x = start.box.x + start.box.w - b.w; }
        if (d.indexOf("n") >= 0) { b.h = Math.max(MIN_H, start.box.h - dy); b.y = start.box.y + start.box.h - b.h; }
        saveBox(b);
        layout();
      });
    });
    frame = el("iframe", { title: "Chat", src: CHAT + "?embed=1", allow: "clipboard-write" });
    frame.addEventListener("load", checkFrame);
    dock.appendChild(frame);
    fallback = el("div", { class: "sd-fallback" });
    fallback.hidden = true;
    fallback.appendChild(document.createTextNode("The chat cannot be shown here. "));
    fallback.appendChild(el("a", { href: CHAT, target: "_top" }, "Open the chat page"));
    dock.appendChild(fallback);
    document.body.appendChild(dock);
  }

  function checkFrame() {             // a site that forbids frames, or a login page: say so
    var ok = false;
    try {
      var body = frame.contentDocument && frame.contentDocument.body;
      ok = !!(body && body.getAttribute("data-page"));
    } catch (e) { ok = false; }
    fallback.hidden = ok;
    frame.hidden = !ok;
  }

  function show() {
    if (!dock) build();
    dock.classList.toggle("sd-dark", dark());
    dock.hidden = false;
    put("sessionStorage", OPEN_KEY, "1");
    relayout();
  }
  function hide() {
    if (!dock) return;
    dock.hidden = true;
    put("sessionStorage", OPEN_KEY, null);
    relayout();
  }

  document.addEventListener("click", function (ev) {
    if (ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    var a = ev.target && ev.target.closest ? ev.target.closest("a") : null;
    if (!isChatLink(a) || (dock && dock.contains(a))) return;     // the panel's own full-page button: a link
    ev.preventDefault();
    ev.stopPropagation();
    if (dock && !dock.hidden) hide(); else show();
  }, true);
  window.addEventListener("resize", layout);
  window.supagentDock = { show: show, hide: hide, mode: mode };
  if (get("sessionStorage", OPEN_KEY) === "1") show();
})();
