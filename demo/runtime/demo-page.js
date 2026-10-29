// Demo in-page helpers: a stand-in for the alarm engine's live WebSocket.
(function (self) {
  if (self.__demoWsInstalled) return;
  self.__demoWsInstalled = true;

  var RealWebSocket = self.WebSocket;
  var FIRST_MIN_MS = 90000, FIRST_MAX_MS = 120000, MIN_GAP_MS = 45000, MAX_GAP_MS = 90000;

  // True for the URLs the dashboard and the alarm frame dial in the demo.
  function isDemoUrl(url) {
    var u;
    try { u = new URL(String(url), self.location && self.location.href); } catch (e) { return false; }
    return u.hostname === 'demo.invalid' || u.pathname === '/ws/alarm';
  }

  function frame(event, data, nowMs) {
    return JSON.stringify({ event: event, data: data, ts: new Date(nowMs).toISOString().replace('Z', '+00:00') });
  }

  function FakeSocket(url) {
    this.url = String(url);
    this.readyState = 0;
    this.protocol = '';
    this.extensions = '';
    this.bufferedAmount = 0;
    this.binaryType = 'blob';
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    this._listeners = {};
    this._timer = null;
    this._sent = 0;
    var ws = this;
    setTimeout(function () {
      if (ws.readyState !== 0) return;
      ws.readyState = 1;
      ws._fire('open', {});
      ws._schedule();
    }, 0);
  }
  FakeSocket.CONNECTING = 0;
  FakeSocket.OPEN = 1;
  FakeSocket.CLOSING = 2;
  FakeSocket.CLOSED = 3;
  FakeSocket.prototype.CONNECTING = 0;
  FakeSocket.prototype.OPEN = 1;
  FakeSocket.prototype.CLOSING = 2;
  FakeSocket.prototype.CLOSED = 3;

  FakeSocket.prototype.addEventListener = function (type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  };
  FakeSocket.prototype.removeEventListener = function (type, fn) {
    var l = this._listeners[type];
    if (l) this._listeners[type] = l.filter(function (f) { return f !== fn; });
  };
  FakeSocket.prototype.dispatchEvent = function (ev) { this._fire(ev.type, ev); return true; };
  FakeSocket.prototype._fire = function (type, ev) {
    ev.type = type;
    ev.target = ev.currentTarget = this;
    var h = this['on' + type];
    if (typeof h === 'function') h.call(this, ev);
    (this._listeners[type] || []).slice().forEach(function (fn) { fn.call(this, ev); }, this);
  };
  FakeSocket.prototype._message = function (text) {
    if (this.readyState === 1) this._fire('message', { data: text, origin: '' });
  };

  // Alert updates: first after 90–120 s, then every 45–90 s; every second one also sends the toast.
  FakeSocket.prototype._schedule = function () {
    var ws = this;
    var lo = ws._sent ? MIN_GAP_MS : FIRST_MIN_MS, hi = ws._sent ? MAX_GAP_MS : FIRST_MAX_MS;
    var gap = lo + Math.floor(Math.random() * (hi - lo + 1));
    ws._timer = setTimeout(function () {
      if (ws.readyState !== 1) return;
      var n = ws._sent++;
      if (self.DemoAlarms) {
        self.DemoAlarms.firingFrames(Date.now(), Math.floor(n / 2), n % 2 === 1).forEach(function (f) {
          ws._message(JSON.stringify(f));
        });
      }
      ws._schedule();
    }, gap);
  };

  // Mirrors the engine's replies: subscribe/unsubscribe are silent, ping gets pong.
  FakeSocket.prototype.send = function (text) {
    if (this.readyState === 0) throw new Error('InvalidStateError: still connecting');
    if (this.readyState !== 1) return;
    var ws = this, msg, now = Date.now();
    try { msg = JSON.parse(text); } catch (e) { msg = null; }
    var reply;
    if (!msg || typeof msg !== 'object') reply = frame('error', { message: 'Invalid JSON' }, now);
    else if (msg.action === 'subscribe' || msg.action === 'unsubscribe') reply = null;
    else if (msg.action === 'ping') reply = frame('pong', { ts: new Date(now).toISOString() }, now);
    else reply = frame('error', { message: 'Unknown action: ' + (msg.action || '') }, now);
    if (reply) setTimeout(function () { ws._message(reply); }, 0);
  };

  FakeSocket.prototype.close = function (code, reason) {
    if (this.readyState >= 2) return;
    var ws = this;
    ws.readyState = 2;
    clearTimeout(ws._timer);
    setTimeout(function () {
      ws.readyState = 3;
      ws._fire('close', { code: code || 1000, reason: reason || '', wasClean: true });
    }, 0);
  };

  function DemoWebSocket(url, protocols) {
    if (isDemoUrl(url)) return new FakeSocket(url);
    if (!RealWebSocket) throw new Error('WebSocket is not available');
    return protocols === undefined ? new RealWebSocket(url) : new RealWebSocket(url, protocols);
  }
  DemoWebSocket.CONNECTING = 0;
  DemoWebSocket.OPEN = 1;
  DemoWebSocket.CLOSING = 2;
  DemoWebSocket.CLOSED = 3;
  if (RealWebSocket) DemoWebSocket.prototype = RealWebSocket.prototype;

  self.WebSocket = DemoWebSocket;
  self.DemoWs = { isDemoUrl: isDemoUrl, FakeSocket: FakeSocket, RealWebSocket: RealWebSocket };
})(self);

// Demo in-page layer: uncontrolled-reload guard, refusal toasts, miss warnings and terminal cover cards.
(function (self) {
  if (self.__demoPageInstalled) return;
  self.__demoPageInstalled = true;

  var REFUSED_TEXT = 'Not available in the demo';
  var INSTALL_URL = 'https://get.llmsysmanager.com/?ref=demo';
  var PANEL_IDS = ['terminalPanel', 'lmsTerminalPanel', 'vllmTerminalPanel'];
  var path = (self.location && self.location.pathname) || '';
  var inFrame = path.indexOf('/alarm/') === 0 || path === '/alarm';
  var sw = self.navigator && self.navigator.serviceWorker;

  if (path === '/dashboard') try { self.sessionStorage.removeItem('lsmDemoReturn'); } catch (e) { /* storage blocked */ }
  // An uncontrolled page (hard reload, first visit) goes through the bootstrap page, which returns to /phone.
  if (!inFrame && sw && !sw.controller && self.location && self.location.replace) {
    try { if (path === '/phone' || path === '/phone/') self.sessionStorage.setItem('lsmDemoReturn', '/phone'); } catch (e) { /* storage blocked */ }
    self.location.replace('/');
    return;
  }

  // A dashboard whose worker is replaced by a new release reloads once onto the new files.
  var reloading = false;
  if (!inFrame && sw && sw.controller && sw.addEventListener) {
    sw.addEventListener('controllerchange', function () {
      if (reloading || !self.location || !self.location.reload) return;
      reloading = true;
      self.location.reload();
    });
  }

  function track(type, fields) { if (typeof self.DemoTrack === 'function') self.DemoTrack(type, fields); }

  var lastToast = -Infinity;
  var waitTimer = null;
  // Shows the refusal toast at most once every 2 s; before _toastErr exists it waits up to 10 s for it.
  function refused() {
    var now = Date.now();
    if (now - lastToast < 2000) return;
    if (typeof self._toastErr !== 'function') {
      if (waitTimer === null) waitForToast(40);
      return;
    }
    lastToast = now;
    self._toastErr(REFUSED_TEXT);
  }
  function waitForToast(left) {
    waitTimer = setTimeout(function () {
      waitTimer = null;
      if (typeof self._toastErr === 'function') refused();
      else if (left > 1) waitForToast(left - 1);
    }, 250);
  }

  // Misses warn; refusals toast here, or in the alarm frame are posted to the parent dashboard.
  function onWorkerMessage(e) {
    var d = e && e.data;
    if (!d || typeof d !== 'object') return;
    if (d.type === 'demo-miss') {
      console.warn('demo miss', d.method, d.path);
    } else if (d.type === 'demo-error') {
      console.error('demo error', d.method, d.path);
    } else if (d.type === 'demo-refused') {
      if (!inFrame) refused();
      else if (self.parent && self.parent !== self && self.location) {
        self.parent.postMessage({ type: 'demo-refused', key: d.key }, self.location.origin);
      }
    }
  }
  if (sw && sw.addEventListener) sw.addEventListener('message', onWorkerMessage);
  if (!inFrame && self.addEventListener) {
    self.addEventListener('message', function (e) {
      if (!e || !e.data || e.data.type !== 'demo-refused') return;
      if (self.location && e.origin !== self.location.origin) return;
      track('refused', { key: e.data.key });
      refused();
    });
  }

  // Builds the "works on your own install" card that covers a panel.
  function card(doc) {
    var c = doc.createElement('div');
    c.className = 'demo-cover';
    var box = doc.createElement('div');
    box.className = 'demo-cover-card';
    var p = doc.createElement('p');
    p.textContent = 'This works on your own install.';
    var a = doc.createElement('a');
    a.className = 'demo-btn';
    a.href = INSTALL_URL;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Installation directions';
    box.appendChild(p);
    box.appendChild(a);
    c.appendChild(box);
    return c;
  }

  // True when the element has a layout box; without layout, when no ancestor computes to display:none.
  function isShown(el) {
    if (el.getClientRects && el.getClientRects().length > 0) return true;
    var view = el.ownerDocument && el.ownerDocument.defaultView;
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var cs = view && view.getComputedStyle ? view.getComputedStyle(n) : n.style;
      if (cs.display === 'none') return false;
    }
    return true;
  }

  // Adds the cover card to a watched panel once it is visible.
  function cover(el) {
    if (!el || PANEL_IDS.indexOf(el.id) < 0 || !isShown(el)) return;
    if (el.querySelector(':scope > .demo-cover')) return;
    if (!el.style.position) el.style.position = 'relative';
    el.appendChild(card(el.ownerDocument));
    track('card', { panel: 'terminal' });
  }

  function watchPanels(doc) {
    var all = function () { PANEL_IDS.forEach(function (id) { cover(doc.getElementById(id)); }); };
    all();
    if (typeof self.MutationObserver !== 'function') return;
    new self.MutationObserver(all)
      .observe(doc.documentElement, { attributes: true, attributeFilter: ['style', 'class'], subtree: true });
  }

  // Sets --demo-panel-top to the tab bar's bottom edge so the full-height tab panels start below it.
  function measure() {
    var d = self.document;
    var nav = d && d.querySelector('.tab-nav');
    if (!nav || !nav.getBoundingClientRect) return;
    var bottom = nav.getBoundingClientRect().bottom + (self.scrollY || 0);
    if (bottom > 0) d.documentElement.style.setProperty('--demo-panel-top', Math.round(bottom) + 'px');
  }

  function watchLayout(doc) {
    measure();
    if (self.addEventListener) {
      self.addEventListener('load', measure);
      self.addEventListener('resize', measure);
    }
    var nav = doc.querySelector('.tab-nav');
    if (nav && typeof self.ResizeObserver === 'function') {
      var ro = new self.ResizeObserver(measure);
      ro.observe(nav);
      if (doc.body) ro.observe(doc.body);
    }
  }

  var doc = self.document;
  if (!inFrame && doc) {
    var ready = function () { watchPanels(doc); watchLayout(doc); };
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', ready);
    else ready();
  }

  self.DemoPage = { refused: refused, onWorkerMessage: onWorkerMessage, cover: cover, measure: measure,
    PANEL_IDS: PANEL_IDS, INSTALL_URL: INSTALL_URL };
})(self);
