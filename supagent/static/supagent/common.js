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

  /* A choice of several values in a long list (what a part is part of, the categories and parts a map shows):
     the chosen ones as chips, a box to type in, the values that match listed below it: all of them when nothing
     is typed (the first ones when there are many, with how many more), fewer as one types. Several are chosen
     one after the other; a chosen one is taken back with its cross, with a click on it in the list, or with
     Backspace in the empty box. Keys: arrows, Enter, Escape.
       opts.load(words) -> [{id, label, group, hint}] or {items, more}, or a promise of it (the page's own data, or
                           the server's search)
       opts.chosen      the ones chosen at the start; opts.onchange(chosen); opts.placeholder; opts.label
       opts.single      one at most
     Returns {el, ids(), chosen(), set(list), close()}. */
  var pickN = 0;
  function picker(opts) {
    opts = opts || {};
    var id = "pick-" + (++pickN), chosen = (opts.chosen || []).slice(), items = [], more = 0, active = -1, open = false,
      seq = 0, timer = null, busy = false;
    var chips = el("span", { class: "pick-chips" });
    var input = el("input", { type: "text", class: "pick-input", role: "combobox", "aria-expanded": "false",
      "aria-autocomplete": "list", "aria-controls": id, "aria-label": opts.label || opts.placeholder || "Choose",
      autocomplete: "off", spellcheck: "false" });
    var list = el("div", { class: "pick-list", role: "listbox", id: id, "aria-multiselectable": opts.single ? null : "true" });
    list.hidden = true;
    var frame = el("div", { class: "pick-box" }, [chips, input]);
    var box = el("div", { class: "pick" + (opts.cls ? " " + opts.cls : "") }, [frame, list]);
    function has(i) { return chosen.some(function (c) { return String(c.id) === String(i); }); }
    function changed() { if (opts.onchange) opts.onchange(chosen.slice()); }
    function paintChips() {
      chips.innerHTML = "";
      chosen.forEach(function (c) {
        chips.appendChild(el("span", { class: "pick-chip" + (c.cls ? " " + c.cls : ""), title: (c.group ? c.group + ": " : "") + c.label }, [
          el("span", { class: "pick-chip-text", text: c.label }),
          el("button", { type: "button", class: "pick-x", "aria-label": "Remove " + c.label, text: "×", onclick: function (ev) {
            ev.stopPropagation();
            toggle(c);
            input.focus();
          } })]));
      });
      input.placeholder = chosen.length ? "" : (opts.placeholder || "");
    }
    function toggle(it) {
      var at = -1;
      chosen.forEach(function (c, i) { if (String(c.id) === String(it.id)) at = i; });
      if (at >= 0) chosen.splice(at, 1);
      else {
        if (opts.single) chosen = [];
        chosen.push({ id: it.id, label: it.label, group: it.group, hint: it.hint, data: it.data, cls: it.cls });
      }
      paintChips();
      paintList();
      changed();
    }
    function paintList() {
      list.innerHTML = "";
      var group = null;
      items.forEach(function (it, i) {
        if ((it.group || "") !== group) {
          group = it.group || "";
          if (group) list.appendChild(el("div", { class: "pick-group", text: group }));
        }
        var on = has(it.id);
        var o = el("div", { class: "pick-opt" + (i === active ? " active" : "") + (on ? " on" : ""), role: "option",
                            id: id + "-" + i, "aria-selected": String(on) }, [
          el("span", { class: "pick-check", "aria-hidden": "true", text: on ? "✓" : "" }),
          el("span", { class: "pick-label", text: it.label }),
          it.hint ? el("span", { class: "pick-hint", text: it.hint }) : null]);
        o.addEventListener("mousedown", function (ev) { ev.preventDefault(); });       // the box keeps the focus
        o.addEventListener("click", function () { pick(it); });
        list.appendChild(o);
      });
      if (!items.length) list.appendChild(el("div", { class: "pick-none", text: busy ? "Searching…" : "Nothing matches" }));
      if (more > 0) list.appendChild(el("div", { class: "pick-none", text: num(more) + " more: type to narrow the list" }));
      if (active >= 0) input.setAttribute("aria-activedescendant", id + "-" + active);
      else input.removeAttribute("aria-activedescendant");
    }
    function pick(it) {
      toggle(it);
      if (opts.single) { close(); return; }
      if (input.value) { input.value = ""; search(); }
    }
    function search() {
      var mine = ++seq;
      busy = true;
      Promise.resolve(opts.load ? opts.load(input.value.trim()) : []).then(function (r) {
        if (mine !== seq) return;                         // a later typing answers
        busy = false;
        items = (r && r.items) || (Array.isArray(r) ? r : []);
        more = (r && r.more) || 0;
        active = items.length && input.value.trim() ? 0 : -1;
        paintList();
      });
    }
    function outside(ev) { if (!box.contains(ev.target)) close(); }
    function show() {
      if (open) return;
      open = true;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      document.addEventListener("pointerdown", outside, true);
      search();
    }
    function close() {
      if (!open) return;
      open = false;
      list.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      document.removeEventListener("pointerdown", outside, true);
    }
    input.addEventListener("focus", show);
    input.addEventListener("click", show);
    input.addEventListener("input", function () {
      show();
      clearTimeout(timer);
      timer = setTimeout(search, opts.wait === undefined ? 160 : opts.wait);
    });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!open) { show(); return; }
        if (!items.length) return;
        active = (active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        paintList();
        var cur = list.querySelector(".pick-opt.active");
        if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter") {
        if (open && active >= 0 && items[active]) { ev.preventDefault(); pick(items[active]); }
      } else if (ev.key === "Escape") {
        if (open) { ev.preventDefault(); ev.stopPropagation(); close(); }
      } else if (ev.key === "Backspace" && !input.value && chosen.length) {
        toggle(chosen[chosen.length - 1]);
      }
    });
    input.addEventListener("blur", function () {
      setTimeout(function () { if (!box.contains(document.activeElement)) close(); }, 150);
    });
    frame.addEventListener("mousedown", function (ev) {
      if (ev.target !== input && !(ev.target.closest && ev.target.closest(".pick-x"))) { ev.preventDefault(); input.focus(); }
    });
    paintChips();
    return { el: box, input: input,
             ids: function () { return chosen.map(function (c) { return c.id; }); },
             chosen: function () { return chosen.slice(); },
             set: function (now) { chosen = (now || []).slice(); paintChips(); if (open) paintList(); },
             close: close };
  }

  var body = document.body.dataset;
  window.supagent = {
    chat: function (m, p, b) { return api(body.chatApi, m, p, b); },
    dict: function (m, p, b) { return api(body.dictionaryApi, m, p, b); },
    admin: function (m, p, b) { return api(body.adminApi, m, p, b); },
    isAdmin: body.admin === "yes",
    esc: esc, el: el, when: when, whenFull: whenFull, num: num, bytes: bytes, sourceBadge: sourceBadge, pager: pager,
    pop: pop, unpop: unpop, info: info, wireInfos: wireInfos, confirm: confirm, sureButton: sureButton, picker: picker
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { wireInfos(); });
  else wireInfos();
})();
