/* Light or dark, as Superset shows it: the mode the user chose in Superset (kept by Superset in
   localStorage "superset-theme-mode": default, dark or system), the system's preference when
   nothing was chosen, and always light when Superset has no dark theme (THEME_DARK = None).
   Loaded in <head> so that the page never flashes in the other mode. */
(function () {
  var html = document.documentElement;
  var KEY = "superset-theme-mode";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function mode() {
    if (html.getAttribute("data-superset-dark") !== "yes") return "light";
    var m = saved();
    if (m === "dark") return "dark";
    if (m === "default") return "light";
    return media && media.matches ? "dark" : "light";        /* "system", or nothing chosen */
  }
  function rgb(hex) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  function mix(a, b, t) {
    var x = rgb(a), y = rgb(b);
    return "#" + [0, 1, 2].map(function (i) {
      var v = Math.round(x[i] * (1 - t) + y[i] * t);
      return (v < 16 ? "0" : "") + v.toString(16);
    }).join("");
  }
  function luminance(hex) {
    var c = rgb(hex).map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function apply() {
    var m = mode();
    html.setAttribute("data-theme", m);
    var primary = html.getAttribute(m === "dark" ? "data-primary-dark" : "data-primary");
    var s = html.style;
    if (!primary || !/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(primary)) {
      ["--accent", "--accent-ink", "--accent-soft", "--user-bubble"].forEach(function (p) { s.removeProperty(p); });
      return;
    }
    if (m === "dark") {
      var text = luminance(primary) < 0.3 ? mix(primary, "#ffffff", 0.3) : primary;   /* readable on dark panels */
      s.setProperty("--accent", text);
      s.setProperty("--accent-ink", luminance(text) > 0.35 ? "#0d1417" : "#ffffff");
      s.setProperty("--accent-soft", mix("#1a2228", primary, 0.28));
      s.setProperty("--user-bubble", mix("#1a2228", primary, 0.2));
    } else {
      s.setProperty("--accent", primary);
      s.setProperty("--accent-ink", luminance(primary) > 0.5 ? "#1f2a33" : "#ffffff");
      s.setProperty("--accent-soft", mix("#ffffff", primary, 0.12));
      s.setProperty("--user-bubble", mix("#ffffff", primary, 0.1));
    }
  }
  apply();
  if (media) {
    if (media.addEventListener) media.addEventListener("change", apply);
    else if (media.addListener) media.addListener(apply);
  }
  /* the mode changed in another Superset tab */
  window.addEventListener("storage", function (e) { if (!e.key || e.key === KEY) apply(); });
  window.supagentTheme = { mode: mode, apply: apply };
})();
