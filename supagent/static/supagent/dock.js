/* The chat as a panel docked on the right of Superset's pages (dashboards, charts, datasets, SQL
   Lab...): the Chat tab of the top bar opens and closes it, and the page stays usable beside it.
   Ctrl / Cmd / Shift / middle click on the tab still opens the full chat page. The panel stays
   open from page to page in this tab, and keeps its width in this browser. The chat itself is the
   chat page in a frame of the same site (?embed=1): its links to Superset open in this page. */
(function () {
  "use strict";
  var script = document.currentScript;
  var CHAT = (script && script.getAttribute("data-chat")) || "/supagent/";
  var OPEN_KEY = "supagent-dock-open", WIDTH_KEY = "supagent-dock-width";
  var MIN = 320, WIDTH = 440, NARROW = 900;
  var dock = null, frame = null, fallback = null;

  function get(area, key) { try { return window[area].getItem(key); } catch (e) { return null; } }
  function put(area, key, value) {
    try { if (value === null) window[area].removeItem(key); else window[area].setItem(key, value); } catch (e) { /* private mode */ }
  }
  function maxWidth() { return Math.max(MIN, Math.round(window.innerWidth * 0.7)); }
  function width() {
    var w = parseInt(get("localStorage", WIDTH_KEY) || "", 10) || WIDTH;
    return Math.min(Math.max(w, MIN), maxWidth());
  }
  function dark() {
    var mode = get("localStorage", "superset-theme-mode");
    return mode === "dark" || (mode !== "default" && !!window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
  var chatPath = new URL(CHAT, location.href).pathname;
  function isChatLink(a) {
    if (!a || !a.getAttribute("href")) return false;
    try {
      var u = new URL(a.getAttribute("href"), location.href);
      return u.origin === location.origin && u.pathname === chatPath && !u.search;
    } catch (e) { return false; }
  }

  function layout() {
    var open = !!dock && !dock.hidden, narrow = window.innerWidth < NARROW;
    var w = narrow ? window.innerWidth : width();
    document.documentElement.style.setProperty("--supagent-dock-width", w + "px");
    document.documentElement.classList.toggle("supagent-docked", open && !narrow);
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

  function build() {
    dock = el("aside", { id: "supagent-dock", "aria-label": "Chat" });
    dock.hidden = true;
    var bar = el("div", { class: "sd-bar" });
    bar.appendChild(el("span", { class: "sd-title" }, "Chat"));
    var full = el("a", { class: "sd-btn", href: CHAT, target: "_top", title: "Open the chat as a full page" }, "⤢");
    full.addEventListener("click", function () { put("sessionStorage", OPEN_KEY, null); });
    bar.appendChild(full);
    var close = el("button", { class: "sd-btn", type: "button", title: "Close the chat", "aria-label": "Close the chat" }, "×");
    close.addEventListener("click", hide);
    bar.appendChild(close);
    dock.appendChild(bar);
    var grip = el("div", { class: "sd-grip", title: "Drag to resize", "aria-hidden": "true" });
    dock.appendChild(grip);
    frame = el("iframe", { title: "Chat", src: CHAT + "?embed=1", allow: "clipboard-write" });
    frame.addEventListener("load", checkFrame);
    dock.appendChild(frame);
    fallback = el("div", { class: "sd-fallback" });
    fallback.hidden = true;
    fallback.appendChild(document.createTextNode("The chat cannot be shown here. "));
    fallback.appendChild(el("a", { href: CHAT, target: "_top" }, "Open the chat page"));
    dock.appendChild(fallback);
    grip.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      grip.setPointerCapture(ev.pointerId);
      dock.classList.add("sd-resizing");            // the frame must not catch the pointer meanwhile
      var move = function (e) {
        var w = Math.min(Math.max(window.innerWidth - e.clientX, MIN), maxWidth());
        put("localStorage", WIDTH_KEY, String(w));
        layout();
      };
      var up = function () {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        dock.classList.remove("sd-resizing");
        relayout();
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
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
    if (!isChatLink(a)) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (dock && !dock.hidden) hide(); else show();
  }, true);
  window.addEventListener("resize", layout);
  window.supagentDock = { show: show, hide: hide };
  if (get("sessionStorage", OPEN_KEY) === "1") show();
})();
