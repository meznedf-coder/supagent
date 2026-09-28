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

  var body = document.body.dataset;
  window.supagent = {
    chat: function (m, p, b) { return api(body.chatApi, m, p, b); },
    dict: function (m, p, b) { return api(body.dictionaryApi, m, p, b); },
    admin: function (m, p, b) { return api(body.adminApi, m, p, b); },
    isAdmin: body.admin === "yes",
    esc: esc, el: el, when: when, whenFull: whenFull, num: num, bytes: bytes, sourceBadge: sourceBadge
  };
})();
