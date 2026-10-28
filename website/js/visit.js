/* Page visit summary: time on page, scroll depth, clicks and tour video plays, sent when the page is left */
(function () {
  var URL_ = self.SITE_ANALYTICS_URL || "https://telemetry-receiver.llmsyscore.workers.dev/v1/site";
  var SITE_HOSTS = ["llmsyscore.com", "www.llmsyscore.com"];
  var MAX_CLICKS = 30;
  var MIN_MS = 5000;

  function hex16() {
    var out = "", i;
    try {
      var a = new Uint8Array(8);
      crypto.getRandomValues(a);
      for (i = 0; i < 8; i++) out += ("0" + a[i].toString(16)).slice(-2);
      return out;
    } catch (e) {
      for (i = 0; i < 16; i++) out += Math.floor(Math.random() * 16).toString(16);
      return out;
    }
  }

  function store(key, val) {
    try {
      if (val === undefined) return sessionStorage.getItem(key);
      sessionStorage.setItem(key, val);
    } catch (e) {}
    return null;
  }

  var visit = store("lsmSiteVisit");
  if (!/^[0-9a-f]{16}$/.test(visit || "")) {
    visit = hex16();
    store("lsmSiteVisit", visit);
  }
  var n = (parseInt(store("lsmSitePages"), 10) || 0) + 1;
  store("lsmSitePages", String(n));

  var page = hex16();
  var ms = 0;
  var shownAt = document.visibilityState === "visible" ? Date.now() : null;
  var scroll = 0;
  var clicks = [];
  var video = null;
  var lastSig = null, lastMs = 0, sends = 0;

  function tick() {
    if (shownAt !== null) {
      var now = Date.now();
      ms += now - shownAt;
      shownAt = now;
    }
  }

  function sampleScroll() {
    var root = document.documentElement;
    var h = root.scrollHeight;
    var seen = (window.scrollY || window.pageYOffset || 0) + window.innerHeight;
    var pct = h > 0 && h > window.innerHeight ? Math.round(seen / h * 100) : 100;
    if (pct > 100) pct = 100;
    if (pct > scroll) scroll = pct;
  }

  function sameSite(host) {
    return SITE_HOSTS.indexOf(host) !== -1;
  }

  function classify(el) {
    var href = el.tagName === "A" ? el : null;
    var host = href ? href.hostname : "";
    var path = href ? href.pathname : "";
    var proto = href ? href.protocol : "";
    var same = !href || proto === "mailto:" || sameSite(host);
    var t;
    if (host === "get.llmsyscore.com" || el.hasAttribute("data-copy")) t = "install";
    else if (host === "demo.llmsyscore.com") t = "demo";
    else if (host === "github.com") t = "github";
    else if (href && same && proto !== "mailto:" && (path.indexOf("/docs/") === 0 || /(^|\/)docs\.html$/.test(path))) t = "docs";
    else if (el.hasAttribute("data-full")) t = "screenshot";
    else if (proto === "mailto:" || (href && same && /(^|\/)contact\.html$/.test(path))) t = "contact";
    else if (el.matches && el.matches(".term-tabs button")) t = "tab";
    else if (href && same) t = "page";
    else if (href) t = "external";
    else return null;
    return { t: t, h: href && !same ? host : "" };
  }

  document.addEventListener("click", function (e) {
    var el = e.target && e.target.closest ? e.target.closest("a, button") : null;
    if (!el || clicks.length >= MAX_CLICKS) return;
    var c = classify(el);
    if (c) clicks.push(c);
  }, true);

  window.addEventListener("scroll", sampleScroll, { passive: true });

  var vid = document.querySelector(".tour-video video");
  if (vid) {
    var lastPos = 0;
    var watched = 0;
    var played = false;
    var track = function () {
      var d = vid.currentTime - lastPos;
      if (d > 0 && d < 2) watched += d * 1000;
      lastPos = vid.currentTime;
      var pct = vid.duration > 0 ? Math.round(vid.currentTime / vid.duration * 100) : 0;
      if (!video) video = { ms: 0, pct: 0 };
      video.ms = Math.round(watched);
      if (pct > video.pct) video.pct = Math.min(100, pct);
    };
    vid.addEventListener("play", function () {
      lastPos = vid.currentTime;
      track();
      if (!played && clicks.length < MAX_CLICKS) clicks.push({ t: "video", h: "" });
      played = true;
    });
    vid.addEventListener("timeupdate", track);
    vid.addEventListener("pause", track);
    vid.addEventListener("ended", track);
  }

  function clip(s, max) {
    return String(s == null ? "" : s).replace(/[\u0000-\u001f\u007f]/g, "").slice(0, max);
  }

  function summary() {
    tick();
    sampleScroll();
    var body = {
      v: 1, visit: visit, page: page, n: n,
      path: clip(location.pathname, 200),
      ms: Math.round(ms), scroll: scroll,
      clicks: clicks.slice(0, MAX_CLICKS),
      screen: clip((screen.width || 0) + "x" + (screen.height || 0), 20),
      lang: clip(navigator.language, 35)
    };
    try { body.tz = clip(Intl.DateTimeFormat().resolvedOptions().timeZone, 64); } catch (e) {}
    if (n === 1) {
      if (document.referrer) body.referrer = clip(document.referrer, 500);
      var q = new URLSearchParams(location.search);
      ["ref", "utm_source", "utm_medium", "utm_campaign"].forEach(function (k) {
        if (q.get(k)) body[k] = clip(q.get(k), 100);
      });
    }
    if (video) body.video = { ms: video.ms, pct: video.pct };
    return JSON.stringify(body);
  }

  // Sends when anything but the visible time changed, or the visible time grew by MIN_MS since the last send; each send is numbered.
  function send() {
    var body = summary(), o = JSON.parse(body), t = o.ms;
    o.ms = 0;
    var sig = JSON.stringify(o);
    if (sig === lastSig && t - lastMs < MIN_MS) return;
    lastSig = sig;
    lastMs = t;
    o.ms = t;
    o.s = ++sends;
    body = JSON.stringify(o);
    var ok = false;
    try { ok = navigator.sendBeacon && navigator.sendBeacon(URL_, body); } catch (e) {}
    if (!ok) {
      try {
        fetch(URL_, {
          method: "POST", body: body, keepalive: true, mode: "no-cors", credentials: "omit",
          headers: { "Content-Type": "text/plain;charset=UTF-8" }
        }).catch(function () {});
      } catch (e) {}
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      send();
      shownAt = null;
    } else if (shownAt === null) {
      shownAt = Date.now();
    }
  });
  window.addEventListener("pagehide", function () {
    send();
    shownAt = null;
  });
})();
