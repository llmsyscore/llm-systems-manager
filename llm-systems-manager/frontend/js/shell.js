// Manager shell (#1432): sidebar navigation, toolbar title, the Overall filter and range, the All hosts range,
// and the Sidebar, Theme and Live switches.
(() => {
  const $ = (id) => document.getElementById(id);
  const side = $('side');
  if (!side) return;
  const NAV_KEY = 'mgr.nav';

  const TITLES = { overall: 'Overall', llm: 'LLM Control', tools: 'Tools', events: 'Events and metrics', admin: 'Admin' };
  // Dashboards pages with no sidebar item, and the item marked while each shows.
  const SUB_TITLES = { host: 'Host' }, SUB_MARK = { host: 'all' };
  const curLayout = () => (typeof layout !== 'undefined' && layout && typeof layout === 'object') ? layout : null;
  const dashSub = () => (typeof _subTabState !== 'undefined' && _subTabState) ? _subTabState.dashboard : '';

  // Kit store backed by layout.kit in the server-side layout.
  const STORE = {
    getItem(k) {
      const l = curLayout(), kit = l && l.kit;
      return kit && typeof kit === 'object' && Object.prototype.hasOwnProperty.call(kit, k) ? kit[k] : null;
    },
    setItem(k, v) {
      const l = curLayout();
      if (!l) return;
      if (!l.kit || typeof l.kit !== 'object') l.kit = {};
      l.kit[k] = String(v);
      if (typeof saveLayout === 'function') saveLayout();
    },
  };
  // Full-size panels redraw their charts to fit the full-size box; a refit evens out the host, Energy, Forecast and Manager page rows.
  // A double-click inside the Forecast findings browser does not open its panel full size.
  kitConfigure({ prefix: 'mgr', store: STORE, fullRedraw: true, dblSkip: '#fc-b-findings', onRefit: (root) => {
    if (typeof window.hpFit === 'function') window.hpFit(root);
    if (typeof window.enRefit === 'function') window.enRefit(root);
    if (typeof window.fcRefit === 'function') window.fcRefit(root);
    if (typeof window.mgRefit === 'function') window.mgRefit(root);
  } });
  kitWire(document);

  // Overall toolbar group: provider chips and the time range, pressed as window.ovState says.
  const ovBar = document.querySelector('#toolbar .tb-ov');
  const OV_PROV_KEY = 'mgr.ovProv', OV_RANGE_KEY = 'mgr.ovRange';
  function mcShellPaintOv() {
    if (!ovBar) return;
    const o = window.ovState || {};
    ovBar.querySelectorAll('[data-ov-prov]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.ovProv === (o.prov || 'all'))));
    ovBar.querySelectorAll('[data-ov-range]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.ovRange === (+o.range || 24))));
  }
  if (ovBar) {
    ovBar.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      let run = null;
      if (b.dataset.ovProv && typeof window.ovSetFilter === 'function') run = window.ovSetFilter(b.dataset.ovProv);
      else if (b.dataset.ovRange && typeof window.ovSetRange === 'function') run = window.ovSetRange(+b.dataset.ovRange);
      Promise.resolve(run).catch(() => {});
    });
  }
  // Saved filter and range, each kept only when a toolbar button carries that value.
  function restoreOv() {
    const o = window.ovState = window.ovState || { prov: 'all', range: 24 };
    if (!ovBar) return;
    const p = STORE.getItem(OV_PROV_KEY), r = STORE.getItem(OV_RANGE_KEY);
    if ([...ovBar.querySelectorAll('[data-ov-prov]')].some((b) => b.dataset.ovProv === p)) o.prov = p;
    if ([...ovBar.querySelectorAll('[data-ov-range]')].some((b) => b.dataset.ovRange === r)) o.range = +r;
    mcShellPaintOv();
  }

  // All hosts toolbar group: the time range, pressed as window.ahState says.
  const ahBar = document.querySelector('#toolbar .tb-ah');
  const AH_RANGE_KEY = 'mgr.ahRange';
  // Dashboards pages that use the All hosts range.
  const AH_SUBS = ['all', 'llamacpp', 'lmstudio', 'vllm', 'host', 'manager'];
  function mcShellPaintAh() {
    if (!ahBar) return;
    const r = +(window.ahState || {}).range || 24;
    ahBar.querySelectorAll('[data-ah-range]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.ahRange === r)));
  }
  if (ahBar) {
    ahBar.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-ah-range]');
      if (!b || typeof window.ahSetRange !== 'function') return;
      Promise.resolve(window.ahSetRange(+b.dataset.ahRange)).catch(() => {});
      if (typeof window.hpRender === 'function') window.hpRender();
    });
  }
  // Saved All hosts range, kept only when a toolbar button carries that value.
  function restoreAh() {
    const o = window.ahState = window.ahState || { range: 24 };
    if (!ahBar) return;
    const r = STORE.getItem(AH_RANGE_KEY);
    if ([...ahBar.querySelectorAll('[data-ah-range]')].some((b) => b.dataset.ahRange === r)) o.range = +r;
    mcShellPaintAh();
  }

  function mcShellMark(tab, sub) {
    if (ovBar) ovBar.hidden = tab !== 'overall';
    if (tab === 'dashboard' && !sub) sub = dashSub();
    if (ahBar) ahBar.hidden = !(tab === 'dashboard' && AH_SUBS.includes(sub));
    const mark = SUB_MARK[sub] || sub;
    side.querySelectorAll('[data-tab]').forEach((b) => {
      const on = b.dataset.tab === tab && (tab !== 'dashboard' || b.dataset.sub === mark);
      b.classList.toggle('on', on);
      if (on) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
  }

  function mcShellTitle(tab, sub) {
    let t = TITLES[tab];
    if (tab === 'dashboard') {
      if (!sub) sub = dashSub();
      const b = [...side.querySelectorAll('[data-tab="dashboard"][data-sub]')].find((x) => x.dataset.sub === sub);
      const lab = b && b.querySelector('.side-lab');
      t = 'Dashboards · ' + (lab ? lab.textContent.trim() : SUB_TITLES[sub] || '');
    }
    if (!t) return;
    const h = $('pageTitle');
    if (h) h.textContent = t;
    document.title = t + ' — LLM Systems Manager';
  }

  // Overlay open state, as in the devtools shell.
  function setNavOpen(on) {
    side.classList.toggle('open', on);
    $('navOpen').setAttribute('aria-expanded', String(on));
  }
  $('navOpen').onclick = () => setNavOpen(!side.classList.contains('open'));
  side.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tab],#towerNav');
    if (!b) return;
    if (b.dataset.tab) {
      if (b.dataset.sub && typeof _subTabState !== 'undefined') _subTabState.dashboard = b.dataset.sub;
      switchTab(b.dataset.tab);
      if (b.dataset.sub) switchSubTab('dashboard', b.dataset.sub);
    }
    setNavOpen(false);
    if (window.innerWidth < 900) $('navOpen').focus();
  });
  document.addEventListener('click', (e) => {
    if (side.classList.contains('open') && !e.target.closest('#side,#navOpen')) setNavOpen(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && side.classList.contains('open')) { setNavOpen(false); $('navOpen').focus(); }
  });
  // Widening to 900px or more closes the phone overlay.
  try {
    window.matchMedia('(min-width: 900px)').addEventListener('change', (e) => { if (e.matches) setNavOpen(false); });
  } catch (_) { /* no matchMedia */ }
  // At desktop width a collapsed sidebar opens while the mouse is over it or focus is inside it.
  const sideCollapsed = () => side.classList.contains('collapsed') && window.innerWidth >= 900;
  side.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse' && sideCollapsed()) setNavOpen(true); });
  side.addEventListener('pointerleave', (e) => {
    if (e.pointerType === 'mouse' && sideCollapsed() && side.classList.contains('open')) setNavOpen(false);
  });
  side.addEventListener('focusin', () => { if (sideCollapsed()) setNavOpen(true); });
  side.addEventListener('focusout', (e) => {
    const to = e.relatedTarget;
    if (sideCollapsed() && side.classList.contains('open') && !side.matches(':hover')
        && !(to && to.closest && to.closest('#side,#navOpen'))) setNavOpen(false);
  });

  function paintPin() {
    const pinned = !side.classList.contains('collapsed');
    $('navPin').setAttribute('aria-checked', String(pinned));
    $('navPinT').textContent = 'Sidebar: ' + (pinned ? 'Pinned' : 'Icons only');
  }
  $('navPin').onclick = () => {
    const v = side.classList.toggle('collapsed') ? 'collapsed' : 'pinned';
    STORE.setItem(NAV_KEY, v);
    try { localStorage.setItem(NAV_KEY, v); } catch (_) { /* storage blocked */ }
    setNavOpen(false);
    paintPin();
  };

  function mcShellPaintTheme() {
    const light = document.documentElement.getAttribute('data-theme') === 'light';
    $('themeBtn').setAttribute('aria-checked', String(light));
    $('themeT').textContent = 'Theme: ' + (light ? 'Light' : 'Dark');
  }
  $('themeBtn').onclick = () => {
    const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    const l = curLayout();
    if (l && l.themeFollowSystem) setThemeFollowSystem(false);
    applyTheme(next, true);
  };

  function paintLive() {
    const on = !(window.LivePause && window.LivePause.on);
    $('liveBtn').setAttribute('aria-checked', String(on));
    $('liveT').textContent = 'Live: ' + (on ? 'On' : 'Off');
  }
  $('liveBtn').onclick = () => { if (window.LivePause) window.LivePause.toggle(); };
  document.addEventListener('lsm:livepause', paintLive);

  // Fixed tabs sit below the toolbar: --mc-top follows its height.
  const tb = $('toolbar');
  if (tb && typeof ResizeObserver === 'function') {
    new ResizeObserver(() => document.documentElement.style.setProperty('--mc-top', tb.offsetHeight + 'px')).observe(tb);
  }

  // Once the layout has loaded, its saved sidebar state wins and refreshes the pre-paint copy.
  let inited = false;
  function mcShellInit() {
    if (inited) return;
    inited = true;
    const st = kitNav(STORE);
    side.classList.toggle('collapsed', st === 'collapsed');
    try { localStorage.setItem(NAV_KEY, st); } catch (_) { /* storage blocked */ }
    paintPin();
    mcShellPaintTheme();
    paintLive();
    restoreOv();
    restoreAh();
  }

  window.mcShellStore = STORE;
  window.mcShellPaintOv = mcShellPaintOv;
  window.mcShellPaintAh = mcShellPaintAh;
  window.mcShellMark = mcShellMark;
  window.mcShellTitle = mcShellTitle;
  window.mcShellPaintTheme = mcShellPaintTheme;
  window.mcShellInit = mcShellInit;
  paintPin();
  mcShellPaintTheme();
  paintLive();
  Promise.resolve(window.mcLayoutReady).then(mcShellInit);
})();
