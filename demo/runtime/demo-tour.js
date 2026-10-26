// Demo banner and highlights tour: a floating card that walks a first-time visitor through the tabs or phone screens.
(function (self) {
  if (self.DemoTour) return;
  var doc = self.document;
  var SEEN_KEY = 'lsmDemoTourSeen';
  var INSTALL_URL = 'https://get.llmsyscore.com/?ref=demo';
  var INSTALL_LABEL = 'Installation directions';
  var onPhone = /^\/phone\/?$/.test((self.location && self.location.pathname) || '');
  var TAB_BUTTONS = { overall: 'tabBtnOverall', dashboard: 'tabBtnDashboard', llm: 'tabBtnLlmControl',
    tools: 'tabBtnTools', events: 'tabBtnEvents', admin: 'tabBtnAdmin' };

  var deskSteps = [
    { tab: 'overall', selector: '#overallTab .ov-hero', title: 'Your whole fleet',
      text: 'This is the first page you see. The chart shows how fast all your machines answered over the last day, with their power use on top.' },
    { tab: 'overall', selector: '#ovAgentsStrip', title: 'Each machine',
      text: 'Every machine you add shows up here with the model it is running and how busy it is.' },
    { tab: 'dashboard', sub: 'subTabBtnDashEnergy', selector: '#dash-energy', title: 'Energy and cost',
      text: 'See how much power each machine uses and what it costs to run, by the hour and by the day.' },
    { tab: 'dashboard', sub: 'subTabBtnDashForecast', selector: '#dash-forecast', title: 'Forecast',
      text: 'Forecast looks back over the last two weeks and warns you early, for example when a disk is filling up or a machine keeps getting warmer.' },
    { tab: 'llm', sub: 'subTabBtnLlmLlamacpp', selector: '#secModels', title: 'Models',
      text: 'Pick a model, start or stop it, and see how fast it runs. LM Studio has its own tab at the top of this page.' },
    { tab: 'llm', sub: 'subTabBtnLlmTools', selector: '#llm-tools', title: 'Benchmarks and tuning',
      text: 'Measure how fast a model runs on each machine. Autotune tries different settings for you and keeps the fastest ones.' },
    { tab: 'events', selector: '#eventsTab', title: 'Alarms',
      text: 'Alarms tell you when something needs a look, like a machine running hot. New sample alarms show up here every minute or two.' },
    { tab: 'overall', selector: '#towerAside', tower: true, title: 'Ask Tower',
      text: 'Tower answers questions about your machines in plain words. Type here and try "Which machine is slow?" or "How do I get help?"' },
    { tab: 'overall', selector: '#settingsOverlay .sd', settings: true, title: 'Settings and themes',
      text: 'The cog at the top right opens Settings, where you pick a color theme, choose which cards show and set how often the page refreshes. Try a theme now.' },
    { tab: 'admin', selector: '#adminHealthCard', title: 'System health',
      text: 'See at a glance whether every part of the system is running, and which jobs are waiting or running.' },
    { tab: 'admin', sub: 'subTabBtnAdminAgents', selector: '#admin-agents', title: 'Add machines',
      text: 'Each machine runs a small helper program that reports to this dashboard. New machines ask to join here, and you approve them.' },
    { tab: 'admin', sub: 'subTabBtnAdminRouting', selector: '#admin-routing', title: 'Where requests go',
      text: 'Choose which machines answer questions for each model, and which one goes first. You can also keep a model on one machine.' },
    { tab: 'admin', sub: 'subTabBtnAdminJobs', selector: '#admin-jobs', title: 'Jobs',
      text: 'Long tasks like benchmarks and downloads are listed here, with who started them and how they went.' }
  ];

  var phoneSteps = [
    { screen: 'glance', selector: '#glanceStrip', title: 'Your fleet at a glance',
      text: 'The phone app opens here: how fast your machines are answering right now, each provider, and the power they use.' },
    { screen: 'alerts', selector: '#scr-alerts', title: 'Alerts',
      text: 'Alarms from every machine land here. Tap one to see what happened. On your own install they can also arrive as phone notifications.' },
    { screen: 'tower', selector: '#scr-tower', title: 'Ask Tower',
      text: 'Ask Tower about your machines in plain words, right from your phone.' },
    { screen: 'energy', selector: '#energyStrip', title: 'Energy and cost',
      text: 'Power use over the last day and what each day cost to run.' },
    { screen: 'models', selector: '#modelsLoaded', title: 'Models',
      text: 'See which models are loaded on which machine, and restart a provider or switch models.' }
  ];
  var steps = onPhone ? phoneSteps : deskSteps;

  var state = { index: -1, root: null, ring: null, card: null, timer: null, timer2: null, touched: false, opened: null, welcome: null, obs: null };

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

  function link(label, cls) {
    var a = el('a', cls, label);
    a.href = INSTALL_URL;
    a.target = '_blank';
    a.rel = 'noopener';
    return a;
  }

  // A same-tab link to the other version of the demo that records which way the visitor switched.
  function switchLink(label, cls, href, target, before) {
    var a = el('a', cls, label);
    a.href = href;
    a.addEventListener('click', function () {
      if (before) before();
      if (typeof self.DemoTrack === 'function') self.DemoTrack('click', { target: target, href: '' });
    });
    return a;
  }

  // A phone: a narrow window with a touch screen or a mobile browser; tablets count as desktops.
  function isPhone() {
    var w = self.innerWidth || 0;
    if (!w || w > 640) return false;
    var coarse = false;
    try { coarse = !!(self.matchMedia && self.matchMedia('(pointer: coarse)').matches); } catch (e) { /* no matchMedia */ }
    return coarse || /Mobi|Android|iPhone/.test((self.navigator && self.navigator.userAgent) || '');
  }

  function button(label, cls, fn) {
    var b = el('button', cls, label);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }

  // Switches with the dashboard's switchTab, or clicks the tab button when it is not there; phone steps click the tab bar.
  function showTab(tab, screen) {
    if (screen) {
      var t = doc.querySelector('#tabbar .tab[data-tab="' + screen + '"]');
      if (t) t.click();
      return;
    }
    if (typeof self.switchTab === 'function') {
      try { self.switchTab(tab); return; } catch (e) { /* fall back to the button */ }
    }
    var b = doc.getElementById(TAB_BUTTONS[tab]);
    if (b) b.click();
  }

  // Opens a step's sub-tab by clicking its button, the way a visitor would.
  function showSub(id) {
    var b = id && doc.getElementById(id);
    if (b) b.click();
  }

  // The step's element, or its nearest ancestor at least 40 px tall when the element itself is thinner.
  function targetOf(s) {
    var t = s && doc.querySelector(s.selector);
    while (t && t.parentElement && t !== doc.body && t.getBoundingClientRect().height < 40) {
      var p = t.parentElement;
      if (p === doc.body || p === doc.documentElement) break;
      t = p;
    }
    return t;
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
    if (typeof MutationObserver === 'function') {
      state.obs = new MutationObserver(function () { if (state.card) refresh(); });
      Object.keys(DRAWERS).forEach(function (k) {
        var ov = doc.getElementById(DRAWERS[k].overlay);
        if (ov) state.obs.observe(ov, { attributes: true, attributeFilter: ['class'] });
      });
    }
  }

  // Places the ring on the step's element and the card below it, or above when there is no room.
  function place() {
    if (!state.card) return;
    var s = steps[state.index];
    var target = targetOf(s);
    var r = target && target.getBoundingClientRect();
    var vw = self.innerWidth || 1024, vh = self.innerHeight || 768;
    var card = state.card, ring = state.ring;
    var dk = drawerOf(s);
    if (dk && !drawerShown(dk)) { if (state.opened === dk) state.opened = null; r = null; }
    if (!r || (!r.width && !r.height)) {
      ring.style.display = 'none';
      card.style.left = Math.max(12, (vw - card.offsetWidth) / 2) + 'px';
      card.style.top = Math.max(12, vh - card.offsetHeight - 24) + 'px';
      return;
    }
    var top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
    // The ring stays inside the window so all four sides show, even for a full-page section.
    var rl = Math.max(r.left - 4, 2), rr = Math.min(r.right + 4, vw - 2);
    var rt = Math.max(top - 4, 2), rb = Math.min(bottom + 4, vh - 2);
    ring.style.display = '';
    ring.style.left = rl + 'px';
    ring.style.top = rt + 'px';
    ring.style.width = Math.max(rr - rl, 0) + 'px';
    ring.style.height = Math.max(rb - rt, 0) + 'px';
    var p = cardSpot({ left: rl, right: rr, top: rt, bottom: rb }, card.offsetWidth, card.offsetHeight, vw, vh);
    card.style.top = p.y + 'px';
    card.style.left = p.x + 'px';
  }

  // Card position outside the ring: below, above, left or right of it, else the bottom of the window.
  function cardSpot(a, cw, ch, vw, vh) {
    var gap = 12, clampX = function (x) { return Math.min(Math.max(gap, x), Math.max(gap, vw - cw - gap)); };
    var clampY = function (y) { return Math.min(Math.max(gap, y), Math.max(gap, vh - ch - gap)); };
    if (a.bottom + gap + ch <= vh - gap) return { x: clampX(a.left), y: clampY(a.bottom + gap) };
    if (a.top - gap - ch >= gap) return { x: clampX(a.left), y: clampY(a.top - gap - ch) };
    if (a.left - gap - cw >= gap) return { x: a.left - gap - cw, y: clampY(a.bottom - ch) };
    if (a.right + gap + cw <= vw - gap) return { x: a.right + gap, y: clampY(a.bottom - ch) };
    return { x: Math.max(gap, (vw - cw) / 2), y: Math.max(gap, vh - ch - 24) };
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
    var right = el('div', 'demo-tour-nav-right');
    if (last) right.appendChild(link(INSTALL_LABEL, 'demo-btn demo-tour-install'));
    right.appendChild(button(last ? 'Done' : 'Next', last ? 'demo-btn demo-btn-ghost demo-tour-next' : 'demo-btn demo-tour-next', nextStep));
    nav.appendChild(right);
    var dk = drawerOf(s);
    if (dk && !drawerShown(dk)) {
      card.appendChild(button(DRAWERS[dk].label, 'demo-btn demo-btn-ghost ' + DRAWERS[dk].cls, function () { showDrawer(dk); render(); place(); }));
    }
    card.appendChild(nav);
  }

  // Drawers a step can open: Tower and the Settings drawer behind the cog.
  var DRAWERS = {
    tower: { overlay: 'towerOverlay', open: 'towerOpen', close: 'towerClose', label: 'Open Tower', cls: 'demo-tour-open-tower' },
    settings: { overlay: 'settingsOverlay', open: 'openSettings', close: 'closeSettings', label: 'Open Settings', cls: 'demo-tour-open-settings',
      section: 'sdAppearance' }
  };
  function drawerOf(s) { return s && s.tower ? 'tower' : s && s.settings ? 'settings' : null; }
  function drawerShown(k) { var o = doc.getElementById(DRAWERS[k].overlay); return !!(o && o.classList.contains('open')); }
  // Opens the step's drawer, or closes the one the tour opened when the step has none.
  function showDrawer(k) {
    if (state.opened && state.opened !== k) {
      var d = DRAWERS[state.opened];
      if (drawerShown(state.opened) && typeof self[d.close] === 'function') try { self[d.close]({ returnFocus: false }); } catch (e) { /* already closed */ }
      state.opened = null;
    }
    if (!k || drawerShown(k) || typeof self[DRAWERS[k].open] !== 'function') return;
    try { self[DRAWERS[k].open](); } catch (e) { /* drawer not ready */ }
    if (drawerShown(k)) state.opened = k;
    var sec = DRAWERS[k].section && doc.getElementById(DRAWERS[k].section);
    if (sec && typeof sec.scrollIntoView === 'function') sec.scrollIntoView({ block: 'start' });
  }

  // Scrolls the step's element into the window when any of it is off screen.
  function reveal() {
    var t = targetOf(steps[state.index]);
    if (!t || typeof t.scrollIntoView !== 'function') return;
    var r = t.getBoundingClientRect(), vh = self.innerHeight || 768;
    if (r.top < 0 || r.bottom > vh) t.scrollIntoView({ block: r.height < vh ? 'center' : 'start' });
  }

  // Re-renders a drawer step's card so its Open button follows the drawer, then places the ring.
  function refresh() {
    if (state.card && drawerOf(steps[state.index])) render();
    place();
  }

  function go(i) {
    state.index = Math.max(0, Math.min(steps.length - 1, i));
    showDrawer(drawerOf(steps[state.index]));
    showTab(steps[state.index].tab, steps[state.index].screen);
    showSub(steps[state.index].sub);
    render();
    place();
    clearTimeout(state.timer);
    clearTimeout(state.timer2);
    reveal();
    state.timer = setTimeout(function () { reveal(); refresh(); }, 400);
    state.timer2 = setTimeout(refresh, 1200);
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
    showDrawer(null);
    if (state.obs) { state.obs.disconnect(); state.obs = null; }
    clearTimeout(state.timer);
    clearTimeout(state.timer2);
    if (state.root && state.root.parentNode) state.root.parentNode.removeChild(state.root);
    state.root = state.ring = state.card = null;
    state.index = -1;
  }

  function nextStep() {
    if (state.index >= steps.length - 1) close(true);
    else { track('next', state.index + 1); go(state.index + 1); }
  }
  function backStep() { if (state.index > 0) { track('back', state.index - 1); go(state.index - 1); } }

  // Adds the demo notice above the header: install link, Tour, and a switch to the phone or desktop version.
  function banner() {
    if (!doc || !doc.body || doc.getElementById('demoBanner')) return;
    var b = el('div', 'demo-banner');
    b.id = 'demoBanner';
    b.appendChild(el('span', 'demo-banner-text', 'Demo with sample data'));
    var acts = el('span', 'demo-banner-actions');
    acts.appendChild(link(INSTALL_LABEL, 'demo-btn demo-banner-install'));
    acts.appendChild(button('Tour', 'demo-btn demo-btn-ghost demo-banner-tour', function () { start(0); }));
    acts.appendChild(onPhone
      ? switchLink('Desktop version', 'demo-btn demo-btn-ghost demo-banner-switch', '/dashboard', 'switch-desktop')
      : switchLink('Phone version', 'demo-btn demo-btn-ghost demo-banner-switch', '/phone', 'switch-phone'));
    b.appendChild(acts);
    doc.body.insertBefore(b, doc.body.firstChild);
    if (self.DemoPage && self.DemoPage.measure) self.DemoPage.measure();
  }

  function welcomeTrack(what) { if (typeof self.DemoTrack === 'function') self.DemoTrack('click', { target: 'welcome-' + what, href: '' }); }

  // Closes the welcome card and records the choice; 'tour' starts the tour, 'phone' leaves for the phone app.
  function leaveWelcome(choice) {
    if (!state.welcome) return;
    doc.removeEventListener('keydown', state.welcome.onKey, true);
    doc.removeEventListener('visibilitychange', state.welcome.onHide);
    self.removeEventListener('pagehide', state.welcome.onLeave);
    if (state.welcome.root.parentNode) state.welcome.root.parentNode.removeChild(state.welcome.root);
    state.welcome = null;
    if (choice !== 'phone') welcomeTrack(choice);
    if (choice === 'tour') start(0);
    markSeen();
  }

  // Centered first-visit card: the tour or the dashboard, or on a phone the phone app or the desktop version; Esc or a click outside closes it.
  function welcome() {
    if (!doc || !doc.body || state.welcome) return;
    var phone = isPhone(), stay = 'close';
    var root = el('div', 'demo-welcome');
    var card = el('div', 'demo-welcome-card');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-labelledby', 'demoWelcomeTitle');
    var title = el('h2', 'demo-welcome-title', 'Welcome to the LLM Systems Manager demo');
    title.id = 'demoWelcomeTitle';
    card.appendChild(title);
    var nav = el('div', 'demo-welcome-nav');
    var first;
    if (phone) {
      card.appendChild(el('p', 'demo-welcome-text',
        'This demo is best viewed on a desktop or tablet. You can still look around the desktop version here, or switch to the phone app, which is made for small screens.'));
      first = switchLink('Switch to the phone app', 'demo-btn demo-welcome-phone', '/phone', 'welcome-phone',
        function () { leaveWelcome('phone'); });
      nav.appendChild(first);
      nav.appendChild(button('Open the desktop version', 'demo-btn demo-btn-ghost demo-welcome-desktop', function () { leaveWelcome('desktop'); }));
    } else {
      card.appendChild(el('p', 'demo-welcome-text',
        'This is the real dashboard running on sample data. Everything works: click around, start models, run tools and ask Tower.'));
      first = button('Take the 1-minute tour', 'demo-btn demo-welcome-tour', function () { leaveWelcome('tour'); });
      nav.appendChild(first);
      nav.appendChild(button('Explore on my own', 'demo-btn demo-btn-ghost demo-welcome-explore', function () { leaveWelcome('explore'); }));
    }
    var inst = link(INSTALL_LABEL, 'demo-btn demo-btn-ghost demo-welcome-install');
    inst.addEventListener('click', function () { setTimeout(function () { leaveWelcome('install'); }, 0); });
    nav.appendChild(inst);
    card.appendChild(nav);
    root.appendChild(card);
    var downOutside = false;
    root.addEventListener('pointerdown', function (e) { downOutside = e.target === root; });
    root.addEventListener('click', function (e) { if (e.target === root && downOutside) leaveWelcome(stay); });
    var onKey = function (e) { if (e.key === 'Escape') { e.stopPropagation(); leaveWelcome(stay); } };
    doc.addEventListener('keydown', onKey, true);
    // Records leaving the page while the card is still open, once per card.
    var left = false, onLeave = function () {
      if (left || !state.welcome) return;
      left = true;
      welcomeTrack('left');
      if (self.DemoAnalytics && self.DemoAnalytics.flush) self.DemoAnalytics.flush(true);
    };
    var onHide = function () { if (doc.visibilityState === 'hidden') onLeave(); };
    doc.addEventListener('visibilitychange', onHide);
    self.addEventListener('pagehide', onLeave);
    doc.body.appendChild(root);
    state.welcome = { root: root, onKey: onKey, onHide: onHide, onLeave: onLeave };
    welcomeTrack('shown');
    try { first.focus({ preventScroll: true }); } catch (e) { /* no focus */ }
  }

  // Shows the banner, then on the dashboard the welcome card shortly after load on a first visit with no click or key yet.
  function init() {
    banner();
    if (onPhone || seen()) return;
    var begin = function () {
      setTimeout(function () { if (!state.touched && !seen() && !state.root) welcome(); }, 1200);
    };
    if (doc.readyState === 'complete') begin();
    else self.addEventListener('load', begin);
  }

  self.DemoTour = { steps: steps, phoneSteps: phoneSteps, onPhone: onPhone, isPhone: isPhone, start: start, close: close, next: nextStep, back: backStep,
    place: place, cardSpot: cardSpot, init: init, seen: seen, welcome: welcome, get index() { return state.index; }, SEEN_KEY: SEEN_KEY };

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
