// Records what a visitor does in the demo and sends it in batches to the telemetry receiver.
(function (self) {
  var ENDPOINT = self.DEMO_ANALYTICS_URL || 'https://edge.llmsysmanager.com/v1/demo';
  var SESSION_KEY = 'lsmDemoSession';
  var FLUSH_MS = 15000;
  var MAX_BATCH = 50;
  var doc = self.document;
  var inFrame = self.parent && self.parent !== self;
  if (inFrame || !doc) return;

  function now() { return Date.now(); }
  function hex16() {
    var c = self.crypto, out = '';
    if (c && c.getRandomValues) {
      var a = new Uint8Array(8);
      c.getRandomValues(a);
      for (var i = 0; i < a.length; i++) out += (a[i] < 16 ? '0' : '') + a[i].toString(16);
      return out;
    }
    for (var j = 0; j < 16; j++) out += Math.floor(Math.random() * 16).toString(16);
    return out;
  }

  // One session per browser tab: kept in sessionStorage so the hard-reload hop keeps it.
  function loadSession() {
    try {
      var raw = self.sessionStorage.getItem(SESSION_KEY);
      var s = raw && JSON.parse(raw);
      if (s && /^[0-9a-f]{16}$/.test(s.id) && typeof s.t0 === 'number') return { id: s.id, t0: s.t0, seq: s.seq || 0, fresh: false };
    } catch (e) { /* storage blocked */ }
    return { id: hex16(), t0: now(), seq: 0, fresh: true };
  }
  var session = loadSession();
  function save() {
    try { self.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ id: session.id, t0: session.t0, seq: session.seq })); } catch (e) { /* storage blocked */ }
  }
  save();

  var queue = [];
  var timer = null;
  function track(type, fields) {
    var ev = { t: now() - session.t0, type: String(type) };
    if (fields) for (var k in fields) if (Object.prototype.hasOwnProperty.call(fields, k) && fields[k] !== undefined) ev[k] = fields[k];
    queue.push(ev);
    if (queue.length >= MAX_BATCH) flush(false);
    else if (timer === null) timer = setTimeout(function () { timer = null; flush(false); }, FLUSH_MS);
  }

  // Sends queued events; on page exit it uses sendBeacon so the last batch still leaves.
  function flush(leaving) {
    if (timer !== null) { clearTimeout(timer); timer = null; }
    while (queue.length) {
      var body = JSON.stringify({ v: 1, session: session.id, seq: session.seq, release: self.DEMO_RELEASE || '',
        events: queue.splice(0, MAX_BATCH) });
      session.seq += 1;
      save();
      var sent = false;
      var nav = self.navigator;
      if (leaving && nav && nav.sendBeacon) {
        try { sent = nav.sendBeacon(ENDPOINT, new Blob([body], { type: 'text/plain;charset=UTF-8' })); } catch (e) { sent = false; }
      }
      if (!sent && typeof self.fetch === 'function') {
        try {
          self.fetch(ENDPOINT, { method: 'POST', body: body, keepalive: true, mode: 'no-cors', credentials: 'omit',
            headers: { 'Content-Type': 'text/plain;charset=UTF-8' } }).catch(function () {});
        } catch (e) { /* offline */ }
      }
    }
  }

  function params() {
    var out = {};
    try {
      var q = new URLSearchParams(self.location.search);
      ['ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'].forEach(function (k) {
        if (q.get(k)) out[k] = q.get(k);
      });
    } catch (e) { /* no URLSearchParams */ }
    return out;
  }

  // Remembers the landing page and campaign tags on the bootstrap page, which then hops to /dashboard.
  var LANDING_KEY = 'lsmDemoLanding';
  function landing() {
    try {
      var raw = self.sessionStorage.getItem(LANDING_KEY);
      if (raw) return JSON.parse(raw);
    } catch (e) { /* storage blocked */ }
    var m = doc.querySelector && doc.querySelector('meta[name="lsm-landing"]');
    var l = { landing: (self.location.pathname || '') + (self.location.search || ''), referrer: doc.referrer || '', p: params(),
      lid: (m && m.getAttribute('content')) || '' };
    try { self.sessionStorage.setItem(LANDING_KEY, JSON.stringify(l)); } catch (e) { /* storage blocked */ }
    return l;
  }
  var land = landing();

  function startEvent() {
    var scr = self.screen || {};
    var dark = false;
    try { dark = !!(self.matchMedia && self.matchMedia('(prefers-color-scheme: dark)').matches); } catch (e) { /* no matchMedia */ }
    var f = { referrer: land.referrer, landing: land.landing, screen: (scr.width || 0) + 'x' + (scr.height || 0),
      viewport: (self.innerWidth || 0) + 'x' + (self.innerHeight || 0), lang: (self.navigator && self.navigator.language) || '',
      tz: '', theme: dark ? 'dark' : 'light', lid: land.lid || undefined };
    try { f.tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* no Intl */ }
    for (var k in land.p) f[k] = land.p[k];
    track('start', f);
  }

  // The loading page sends a landing event when the dashboard does not open (left, slow, unsupported, failed), when it opens
  // after "Still loading" (slow_open), or when a visitor clicks one of its links (screenshots, install); a click is kept.
  function loadingPage() {
    var shown = 'left', last = null, clicked = false, perf = self.performance, t0 = (perf && perf.timeOrigin) || now();
    function report(reason) {
      if (shown === 'open' || clicked || reason === last) return;
      last = reason;
      var f = { reason: reason, total_ms: Math.round(now() - t0), lid: land.lid || undefined, referrer: land.referrer, landing: land.landing };
      for (var k in land.p) f[k] = land.p[k];
      track('landing', f);
      flush(true);
    }
    self.DemoLanding = function (state) {
      if (state === 'open') { if (shown === 'slow') report('slow_open'); shown = state; }
      else if (state === 'slow') shown = state;
      else if (state === 'retry') report(state);
      else if (state === 'unsupported' || state === 'failed') { shown = state; report(state); }
      else if (state === 'screenshots' || state === 'install') { report(state); clicked = true; }
    };
    doc.addEventListener('visibilitychange', function () { if (doc.visibilityState === 'hidden') report(shown); });
    self.addEventListener('pagehide', function () { report(shown); });
    self.DemoTrack = function () {};
    self.DemoAnalytics = { flush: flush, session: session, ENDPOINT: ENDPOINT, landing: land };
  }

  // The bootstrap page records the landing; the dashboard and the phone app record the session.
  var isDashboard = /^\/(dashboard|phone)(\/|$)/.test(self.location.pathname || '');
  if (!isDashboard) { loadingPage(); return; }
  var started = false;
  try { started = self.sessionStorage.getItem('lsmDemoStarted') === session.id; } catch (e) { /* storage blocked */ }
  if (!started) {
    startEvent();
    try { self.sessionStorage.setItem('lsmDemoStarted', session.id); } catch (e) { /* storage blocked */ }
    flush(false);
  }

  var tab = 'overall', tabSince = now();
  function onTab(name) {
    if (!name || name === tab) return;
    var t = now();
    track('tab', { tab: name, prev: tab, dwell_ms: t - tabSince });
    tab = name;
    tabSince = t;
  }
  doc.addEventListener('lsm:tab', function (e) { onTab(e && e.detail && e.detail.tab); });

  function targetOf(href) {
    if (/get\.llmsysmanager\.com/.test(href)) return 'install';
    if (/#screenshots/.test(href)) return 'gallery';
    if (/github\.com/.test(href)) return 'repo';
    return 'other';
  }
  doc.addEventListener('click', function (e) {
    var el = e.target;
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var tag = n.getAttribute && n.getAttribute('data-demo-track');
      if (tag) { track('click', { target: tag, href: '' }); return; }
      if (n.id && n.id.indexOf('subTabBtn') === 0) { track('subtab', { tab: tab, sub: n.id.slice(9) }); return; }
      if (n.classList && n.classList.contains('demo-banner-tour')) { track('click', { target: 'tour-button', href: '' }); return; }
      if (n.tagName === 'A' && n.href && n.host !== self.location.host) { track('click', { target: targetOf(n.href), href: n.href }); return; }
    }
  }, true);

  // Records each time the Tower drawer opens on the dashboard.
  function watchTower() {
    var o = doc.getElementById('towerOverlay');
    if (!o || typeof self.MutationObserver !== 'function' || /^\/phone/.test(self.location.pathname || '')) return;
    var wasOpen = o.classList.contains('open');
    new self.MutationObserver(function () {
      var open = o.classList.contains('open');
      if (open && !wasOpen) track('click', { target: 'tower-open', href: '' });
      wasOpen = open;
    }).observe(o, { attributes: true, attributeFilter: ['class'] });
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', watchTower);
  else watchTower();

  // Watches the page's own requests: refused changes, started benchmarks, and Tower questions with the canned answer key.
  var realFetch = self.fetch;
  if (typeof realFetch === 'function') {
    self.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var p = realFetch.apply(this, arguments);
      var method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (method !== 'GET' && method !== 'HEAD') {
        var path = url.split('?')[0].replace(/^https?:\/\/[^/]+/, '');
        p.then(function (r) {
          if (!r || !r.clone) return;
          if (r.status === 200 && method === 'POST' && path === '/api/benchmark/live/run') {
            return r.clone().json().then(function (d) { if (d && d.ok) track('click', { target: 'bench-start', href: '' }); });
          }
          if (r.status !== 403) return;
          return r.clone().json().then(function (d) {
            if (d && d.error === 'Not available in the demo') track('refused', { key: method + ' ' + path });
          });
        }).catch(function () {});
      }
      if (self.DemoTourAsking !== true && /\/api\/tower\/threads\/[^/]+\/messages$/.test(url.split('?')[0]) && init && init.body) {
        var text = '';
        try { text = String(JSON.parse(init.body).text || ''); } catch (e) { /* not JSON */ }
        p.then(function (r) { return r.clone().json(); }).then(function (d) {
          var m = /^demo-run-([a-z]+)-/.exec((d && d.run_id) || '');
          track('tower', { text: text.slice(0, 200), answer: m ? (m[1] === 'other' ? 'fallback' : m[1]) : 'fallback' });
        }).catch(function () {});
      }
      return p;
    };
  }

  // Install cards inside tool frames report themselves to this page.
  self.addEventListener('message', function (e) {
    if (!e || !e.data || e.data.type !== 'demo-card') return;
    if (self.location && e.origin !== self.location.origin) return;
    track('card', { panel: String(e.data.panel || '') });
  });

  // One end event per exit: hidden and pagehide together count once, until the page is shown again.
  var gone = false;
  function leave(reason) {
    if (gone) return;
    gone = true;
    var t = now();
    track('end', { total_ms: t - session.t0, tab: tab, dwell_ms: t - tabSince, reason: reason });
    flush(true);
  }
  doc.addEventListener('visibilitychange', function () { if (doc.visibilityState === 'hidden') leave('hidden'); else gone = false; });
  self.addEventListener('pagehide', function () { leave('pagehide'); });
  self.addEventListener('pageshow', function () { gone = false; });

  self.DemoTrack = track;
  self.DemoAnalytics = { track: track, flush: flush, session: session, ENDPOINT: ENDPOINT, onTab: onTab };
})(self);
