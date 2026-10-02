/* supagent: helpers shared by the pages (no framework, no external script: Superset's CSP) */
(function () {
  "use strict";
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  function api(base, method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (method !== "GET") opts.headers["X-CSRFToken"] = csrf;
    return fetch(base + path, opts).then(function (r) {
      return r.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text.slice(0, 300) || r.statusText }; }
        if (r.status === 401 || r.status === 403) {
          data = { error: (data && (data.error || data.message || data.msg)) || "not allowed (log in again?)" };
        }
        if (!r.ok && !data.error) data.error = "HTTP " + r.status;
        data._status = r.status;
        return data;
      });
    }, function (err) { return { error: "network error: " + err, _status: 0 }; });
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") e.className = attrs[k];
      else if (k === "text") e.textContent = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) e.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }

  function when(v) {
    if (!v) return "";
    var d = new Date(String(v).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? "" : "Z"));
    if (isNaN(d)) return String(v);
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function whenFull(v) {
    var d = v instanceof Date ? v : new Date(String(v || "").replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(v || "") ? "" : "Z"));
    if (!v || isNaN(d)) return "";
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                                          second: "2-digit" });
  }

  function num(v) {
    if (v === null || v === undefined || v === "") return "";
    if (typeof v === "number") return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    return String(v);
  }

  function bytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function sourceBadge(src, verified) {
    if (!src) return "";
    if (src === "llm") return '<span class="badge llm" title="Written by the LLM' + (verified ? ", approved by an admin" : ", not checked by a person") + '">AI-written' + (verified ? ", approved" : "") + "</span>";
    if (src === "curated") return '<span class="badge curated" title="Written or approved by people">curated</span>';
    if (src === "backend") return '<span class="badge backend" title="HELP text of the exporter or the mapping">from the source</span>';
    return '<span class="badge">' + esc(src) + "</span>";
  }

  /* Previous · page 2 of 7 (321) · Next under a long list; st: {page (from 0), size, total} */
  function pager(box, st, reload) {
    box.innerHTML = "";
    var pages = Math.max(1, Math.ceil((st.total || 0) / st.size));
    if (st.page > pages - 1) st.page = pages - 1;
    box.hidden = pages <= 1;
    if (box.hidden) return;
    box.appendChild(el("button", { type: "button", class: "btn", text: "Previous", disabled: st.page <= 0 ? "disabled" : null,
      onclick: function () { st.page--; reload(); } }));
    box.appendChild(el("span", { class: "muted", text: "page " + (st.page + 1) + " of " + pages + " (" + num(st.total) + ")" }));
    box.appendChild(el("button", { type: "button", class: "btn", text: "Next", disabled: st.page >= pages - 1 ? "disabled" : null,
      onclick: function () { st.page++; reload(); } }));
  }

  /* ---------------------------------------------------------------- a small card next to a button: the
     explanations (the "i" buttons) and the confirmations. One at a time; Escape or a click outside closes it. */
  var popped = null;
  function unpop(answer) {
    if (!popped) return;
    var p = popped;
    popped = null;
    p.node.remove();
    p.anchor.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", p.onKey, true);
    document.removeEventListener("pointerdown", p.onDown, true);
    window.removeEventListener("resize", p.place);
    window.removeEventListener("scroll", p.place, true);
    if (p.done) p.done(answer);
    if (answer !== "keep" && p.anchor.isConnected) p.anchor.focus({ preventScroll: true });
  }
  function pop(anchor, content, opts) {
    opts = opts || {};
    var again = popped && popped.anchor === anchor;
    unpop(false);
    if (again && !opts.force) return null;          // the same button again: closed (a toggle)
    var node = el("div", { class: "pop " + (opts.cls || ""), role: opts.role || "dialog",
                           "aria-label": opts.label || null }, [content]);
    document.body.appendChild(node);
    var place = function () {
      if (!anchor.isConnected) { unpop(false); return; }
      var r = anchor.getBoundingClientRect(), w = node.offsetWidth, h = node.offsetHeight;
      var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
      var left = Math.max(8, Math.min(r.left, vw - w - 8));
      var top = r.bottom + 6;
      if (top + h > vh - 8 && r.top - h - 6 > 8) top = r.top - h - 6;     // no room below: above
      node.style.left = left + "px";
      node.style.top = Math.max(8, top) + "px";
    };
    var onKey = function (ev) { if (ev.key === "Escape") { ev.stopPropagation(); unpop(false); } };
    var onDown = function (ev) { if (!node.contains(ev.target) && !anchor.contains(ev.target)) unpop("keep"); };
    popped = { node: node, anchor: anchor, done: opts.done, onKey: onKey, onDown: onDown, place: place };
    anchor.setAttribute("aria-expanded", "true");
    place();
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return node;
  }

  /* an "i" button: its explanation in a small card (a second click, Escape or a click outside closes it).
     `text`: a string, or the id of a hidden element of the page whose content is shown */
  function info(text, label) {
    var b = el("button", { type: "button", class: "info", "aria-expanded": "false",
                           "aria-label": label ? "About " + label : "What is this?", title: "What is this?", text: "i" });
    wireInfo(b, text);
    return b;
  }
  function wireInfo(b, text) {
    if (b._wired) return;
    b._wired = true;
    b.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      var src = typeof text === "string" && text ? null : document.getElementById(b.getAttribute("aria-controls") || "");
      var body = el("div", { class: "pop-text" });
      if (src) body.innerHTML = src.innerHTML; else body.textContent = text || "";
      pop(b, body, { cls: "pop-info", label: b.getAttribute("aria-label") });
    });
  }
  function wireInfos(root) {
    (root || document).querySelectorAll("button.info[aria-controls]").forEach(function (b) { wireInfo(b, null); });
  }

  /* what cannot be undone is asked twice: the first click opens a small card that says what goes, the second
     (its red button) does it; Cancel, Escape or a click elsewhere keeps everything. No browser dialog (blocked in
     some places, and Superset's pages hide them). `ask`: the question (a string or a function returning one),
     `detail`: what goes with it, `yes`: the red button's word. Resolves true when confirmed. */
  function confirm(anchor, ask, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var yes = el("button", { type: "button", class: "btn small danger", text: opts.yes || "Delete" });
      var no = el("button", { type: "button", class: "btn small", text: opts.no || "Cancel" });
      var card = el("div", { class: "pop-confirm" }, [
        el("div", { class: "pop-ask", text: typeof ask === "function" ? ask() : ask }),
        opts.detail ? el("div", { class: "pop-detail", text: opts.detail }) : null,
        el("div", { class: "pop-actions" }, [yes, no])]);
      var node = pop(anchor, card, { cls: "pop-danger", role: "alertdialog", label: typeof ask === "string" ? ask : "Confirm",
                                     force: true, done: function (a) { resolve(a === true); } });
      if (!node) { resolve(false); return; }
      yes.addEventListener("click", function () { unpop(true); });
      no.addEventListener("click", function () { unpop(false); });
      no.focus({ preventScroll: true });            // the safe choice has the focus: Enter twice deletes nothing
    });
  }
  /* a button whose action is confirmed first (label: its text; run: what it does once confirmed) */
  function sureButton(label, run, opts) {
    opts = opts || {};
    return el("button", { type: "button", class: opts.cls || "linkish", text: label, title: opts.title || null,
      "aria-label": opts.aria || null, onclick: function (ev) {
        ev.stopPropagation();
        var b = ev.currentTarget;
        confirm(b, opts.ask || (label + "?"), opts).then(function (ok) { if (ok) run(b); });
      } });
  }

  var body = document.body.dataset;
  window.supagent = {
    chat: function (m, p, b) { return api(body.chatApi, m, p, b); },
    dict: function (m, p, b) { return api(body.dictionaryApi, m, p, b); },
    admin: function (m, p, b) { return api(body.adminApi, m, p, b); },
    isAdmin: body.admin === "yes",
    esc: esc, el: el, when: when, whenFull: whenFull, num: num, bytes: bytes, sourceBadge: sourceBadge, pager: pager,
    pop: pop, unpop: unpop, info: info, wireInfos: wireInfos, confirm: confirm, sureButton: sureButton
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { wireInfos(); });
  else wireInfos();
})();
