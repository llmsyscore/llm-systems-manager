// Demo banner and highlights tour: a floating card that walks a first-time visitor through the tabs.
(function (self) {
  if (self.DemoTour) return;
  var doc = self.document;
  var SEEN_KEY = 'lsmDemoTourSeen';
  var INSTALL_URL = 'https://get.llmsyscore.com/?ref=demo';
  var TAB_BUTTONS = { overall: 'tabBtnOverall', dashboard: 'tabBtnDashboard', llm: 'tabBtnLlmControl',
    tools: 'tabBtnTools', events: 'tabBtnEvents', admin: 'tabBtnAdmin' };

  var steps = [
    { tab: 'overall', selector: '#overallTab .ov-hero', title: 'Your whole fleet',
      text: 'This is the first page you see. The chart shows how fast all your machines answered over the last day, with their power use on top.' },
    { tab: 'overall', selector: '#ovAgentsStrip', title: 'Each machine',
      text: 'Every machine you add shows up here with the model it is running and how busy it is.' },
    { tab: 'llm', selector: '#llm-llamacpp', title: 'Models',
      text: 'Pick a model, start or stop it, and see how fast it runs. LM Studio has its own tab at the top of this page.' },
    { tab: 'events', selector: '#alarmEngineIframe', title: 'Alarms',
      text: 'Alarms tell you when something needs a look, like a machine running hot. New sample alarms show up here every minute or two.' },
    { tab: 'overall', selector: '#towerBtn', title: 'Ask Tower',
      text: 'Tower answers questions about your machines in plain words. Open it and try "Which machine is slow?"' },
    { tab: 'admin', selector: '#adminHealthCard', title: 'Admin',
      text: 'Here you add machines, manage users and change settings. In the demo you can look around, but changes are turned off.' }
  ];

  var state = { index: -1, root: null, ring: null, card: null, timer: null, touched: false };

  function seen() {
    try { return !!self.localStorage.getItem(SEEN_KEY); } catch (e) { return false; }
  }
  function markSeen() {
    try { self.localStorage.setItem(SEEN_KEY, '1'); } catch (e) { /* storage blocked */ }
  }

  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function button(label, cls, fn) {
    var b = el('button', cls, label);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }

  // Switches with the dashboard's switchTab, or clicks the tab button when it is not there.
  function showTab(tab) {
    if (typeof self.switchTab === 'function') {
      try { self.switchTab(tab); return; } catch (e) { /* fall back to the button */ }
    }
    var b = doc.getElementById(TAB_BUTTONS[tab]);
    if (b) b.click();
  }

  function build() {
    var root = el('div', 'demo-tour');
    root.style.pointerEvents = 'none';
    var ring = el('div', 'demo-tour-ring');
    ring.style.pointerEvents = 'none';
    var card = el('div', 'demo-tour-card');
    card.style.pointerEvents = 'auto';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Demo tour');
    root.appendChild(ring);
    root.appendChild(card);
    doc.body.appendChild(root);
    state.root = root; state.ring = ring; state.card = card;
  }

  // Places the ring on the step's element and the card below it, or above when there is no room.
  function place() {
    if (!state.card) return;
    var s = steps[state.index];
    var target = s && doc.querySelector(s.selector);
    var r = target && target.getBoundingClientRect();
    var vw = self.innerWidth || 1024, vh = self.innerHeight || 768;
    var card = state.card, ring = state.ring;
    if (!r || (!r.width && !r.height)) {
      ring.style.display = 'none';
      card.style.left = Math.max(12, (vw - card.offsetWidth) / 2) + 'px';
      card.style.top = Math.max(12, vh - card.offsetHeight - 24) + 'px';
      return;
    }
    var top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
    ring.style.display = '';
    ring.style.left = (r.left - 4) + 'px';
    ring.style.top = (top - 4) + 'px';
    ring.style.width = (r.width + 8) + 'px';
    ring.style.height = (Math.max(bottom - top, 0) + 8) + 'px';
    var ch = card.offsetHeight, cw = card.offsetWidth;
    var y = bottom + 12;
    if (y + ch > vh - 12) y = top - ch - 12;
    if (y < 12) y = Math.max(12, vh - ch - 24);
    card.style.top = y + 'px';
    card.style.left = Math.min(Math.max(12, r.left), Math.max(12, vw - cw - 12)) + 'px';
  }

  function render() {
    var s = steps[state.index], card = state.card;
    card.textContent = '';
    var head = el('div', 'demo-tour-head');
    head.appendChild(el('span', 'demo-tour-step', (state.index + 1) + ' of ' + steps.length));
    head.appendChild(button('Close', 'demo-btn demo-btn-ghost demo-tour-close', close));
    card.appendChild(head);
    card.appendChild(el('div', 'demo-tour-title', s.title));
    card.appendChild(el('p', 'demo-tour-text', s.text));
    var nav = el('div', 'demo-tour-nav');
    var back = button('Back', 'demo-btn demo-btn-ghost demo-tour-back', backStep);
    back.disabled = state.index === 0;
    nav.appendChild(back);
    var last = state.index === steps.length - 1;
    nav.appendChild(button(last ? 'Done' : 'Next', 'demo-btn demo-tour-next', nextStep));
    card.appendChild(nav);
  }

  function go(i) {
    state.index = Math.max(0, Math.min(steps.length - 1, i));
    showTab(steps[state.index].tab);
    render();
    place();
    clearTimeout(state.timer);
    state.timer = setTimeout(place, 400);
  }

  function track(action, step) { if (typeof self.DemoTrack === 'function') self.DemoTrack('tour', { action: action, step: step }); }

  function start(i) {
    if (!doc || !doc.body) return;
    track(seen() ? 'restart' : 'start', i || 0);
    if (!state.root) build();
    go(i || 0);
  }

  function close(finished) {
    if (state.index >= 0) track(finished === true ? 'finish' : 'close', state.index);
    markSeen();
    clearTimeout(state.timer);
    if (state.root && state.root.parentNode) state.root.parentNode.removeChild(state.root);
    state.root = state.ring = state.card = null;
    state.index = -1;
  }

  function nextStep() {
    if (state.index >= steps.length - 1) close(true);
    else { track('next', state.index + 1); go(state.index + 1); }
  }
  function backStep() { if (state.index > 0) { track('back', state.index - 1); go(state.index - 1); } }

  // Adds the one-line demo notice with the install link and the Tour button above the header.
  function banner() {
    if (!doc || !doc.body || doc.getElementById('demoBanner')) return;
    var b = el('div', 'demo-banner');
    b.id = 'demoBanner';
    var msg = el('span', 'demo-banner-msg');
    msg.appendChild(el('span', 'demo-banner-text', 'Demo with sample data. '));
    var a = el('a', 'demo-banner-link', 'Install it on your own machines →');
    a.href = INSTALL_URL;
    a.target = '_blank';
    a.rel = 'noopener';
    msg.appendChild(a);
    b.appendChild(msg);
    b.appendChild(button('Tour', 'demo-btn demo-btn-ghost demo-banner-tour', function () { start(0); }));
    doc.body.insertBefore(b, doc.body.firstChild);
    if (self.DemoPage && self.DemoPage.measure) self.DemoPage.measure();
  }

  // Shows the banner, then starts the tour shortly after load on a first visit with no click or key yet.
  function init() {
    banner();
    if (seen()) return;
    var begin = function () {
      setTimeout(function () { if (!state.touched && !seen() && !state.root) start(0); }, 1200);
    };
    if (doc.readyState === 'complete') begin();
    else self.addEventListener('load', begin);
  }

  self.DemoTour = { steps: steps, start: start, close: close, next: nextStep, back: backStep,
    place: place, init: init, seen: seen, get index() { return state.index; }, SEEN_KEY: SEEN_KEY };

  if (doc && self.addEventListener) {
    var touch = function () { state.touched = true; };
    doc.addEventListener('pointerdown', touch, { capture: true, once: true });
    doc.addEventListener('keydown', touch, { capture: true, once: true });
    self.addEventListener('resize', place);
    doc.addEventListener('scroll', place, { capture: true, passive: true });
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
    else init();
  }
})(self);
