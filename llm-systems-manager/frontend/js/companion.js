// Companion shell (#522): six-tab router, per-screen controllers, push
// opt-in, the Models control surface and the Admin/Settings screens. Classic IIFE.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const CV = window.CView, CS = window.CSpark, EN = window.EN;

  async function jfetch(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts));
    const body = await r.json().catch(() => ({}));
    if (r.status === 401 && body.auth_required) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname);
      throw new Error('auth');
    }
    if (!r.ok) throw Object.assign(new Error(body.error || r.status), { status: r.status });
    return body;
  }

  const jpost = (url, body, method) => jfetch(url, {
    method: method || 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  // Minutes east of UTC; anchors the days= energy window to the caller's local
  // midnight so "today" means today here, not the last 24 UTC hours.
  const TZ_Q = '&tz_offset_min=' + (-new Date().getTimezoneOffset());

  // Resolved from /api/me at boot; false until then so nothing admin-gated
  // fires before the answer arrives.
  let ADMIN = false;

  // ── theme + host liveness ────────────────────────────────────────────────
  // 'auto' follows the dashboard layout theme; 'dark'/'light' override it.
  const themePref = () => localStorage.getItem('companionTheme') || 'auto';

  async function applyTheme() {
    const pref = themePref();
    if (pref === 'dark' || pref === 'light') {
      document.documentElement.setAttribute('data-theme', pref);
    } else {
      try {
        const layout = await jfetch('/api/layout');
        if (layout && layout.theme)
          document.documentElement.setAttribute('data-theme', layout.theme);
      } catch (_) { /* default theme */ }
    }
    const bg = getComputedStyle(document.documentElement)
      .getPropertyValue('--bg-tabnav').trim();
    const meta = document.querySelector('meta[name="theme-color"]');
    if (bg && meta) meta.setAttribute('content', bg);
    const chips = $('themeChips');
    if (chips) {
      chips.querySelectorAll('.chip').forEach((c) =>
        c.classList.toggle('on', c.dataset.ctheme === pref));
    }
  }

  function setLive(online, ageS) {
    const dot = $('liveDot'), name = $('hostName');
    // Drive off age directly: agent_online flips at 30s, so a stale window is
    // only visible via age (30–90s = reconnecting, older/absent = offline).
    const s = (typeof ageS === 'number' && isFinite(ageS)) ? ageS : null;
    if (online === false && (s == null || s >= 90)) { dot.className = 'livedot down'; name.textContent = 'offline'; }
    else if (s != null && s >= 30) { dot.className = 'livedot stale'; name.textContent = 'reconnecting'; }
    else if (s == null && online == null) { dot.className = 'livedot down'; name.textContent = 'offline'; }
    else { dot.className = 'livedot'; name.textContent = 'connected'; }
  }

  // ── render helpers ───────────────────────────────────────────────────────
  function providerRow(p) {
    const suffix = p.rSuffix ? `<small>${esc(p.rSuffix)}</small>` : '';
    return `<div class="prov"><span class="pstat ${p.status}"></span>`
      + `<div class="atxt"><div class="pn">${esc(p.name)}</div>`
      + `<div class="pd">${esc(p.detail)}</div></div>`
      + `<div class="pr"><b${p.warn ? ' class="warn"' : ''}>${esc(p.rN)}${suffix}</b>`
      + `${esc(p.rUnit)}</div></div>`;
  }
  // viewBox-space top padding that clears the hero text at any breakpoint, so
  // the trend line can never run through the numbers.
  function stripPad(stripId, labelId) {
    const strip = $(stripId), label = $(labelId);
    if (!strip || !label) return 54;
    const sr = strip.getBoundingClientRect();
    if (!sr.height) return 54;
    const gap = label.getBoundingClientRect().bottom - sr.top;
    return Math.max(12, Math.min(100, (gap + 12) / sr.height * 118));
  }

  // Tap/drag a strip to read the value under the finger. series() returns
  // { values, label(i) }; marker + readout clear a moment after release.
  function attachScrub(stripId, readoutId, markerId, series) {
    const strip = $(stripId), out = $(readoutId), mark = $(markerId);
    if (!strip || !out) return;
    let timer = null;
    const at = (clientX) => {
      const s = series();
      // pts comes from the last render and values from the live series; an
      // async reload can leave them different lengths, so index the shorter.
      const n = Math.min((s.values || []).length,
        (s.pts && s.pts.length) || (s.values || []).length);
      if (!n) return;
      const r = strip.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      const i = Math.min(n - 1, Math.round(f * (n - 1)));
      const text = s.label(i);
      if (!text) return;
      out.textContent = text;
      out.classList.add('on');
      // Snap to the point, not the finger, and clamp the bubble on its own
      // half-width so the ends stay on screen.
      const pt = (s.pts || [])[i];
      const px = pt ? pt.x / 340 * r.width : f * r.width;
      const half = out.offsetWidth / 2;
      out.style.left = Math.max(half + 6, Math.min(r.width - half - 6, px)) + 'px';
      if (mark && pt) {
        const py = pt.y / 118 * r.height;
        // Second series (if any) gets its own dot; the guide line spans from
        // whichever point is higher down to the baseline.
        const pt2 = (s.pts2 || [])[i];
        const py2 = pt2 ? pt2.y / 118 * r.height : null;
        const dot2 = mark.querySelector('.mdot.alt');
        mark.hidden = false;
        mark.style.left = px + 'px';
        // :not(.alt) — the second-series dot precedes it in the DOM so the
        // primary paints on top, and a bare .mdot would match the wrong one.
        mark.querySelector('.mdot:not(.alt)').style.top = py + 'px';
        if (dot2) {
          dot2.hidden = py2 == null;
          if (py2 != null) dot2.style.top = py2 + 'px';
        }
        mark.querySelector('.mline').style.top =
          (py2 == null ? py : Math.min(py, py2)) + 'px';
      }
      clearTimeout(timer);
      timer = setTimeout(() => {
        out.classList.remove('on');
        if (mark) mark.hidden = true;
      }, 2800);
    };
    strip.addEventListener('pointerdown', (e) => { at(e.clientX); });
    strip.addEventListener('pointermove', (e) => { if (e.buttons) at(e.clientX); });
  }

  function tileEl(t) {
    const body = t.meter != null
      ? `<div class="meter"><i class="${t.hot ? 'hot' : ''}" style="width:${t.meter}%"></i></div>`
      : `<div class="sub">${esc(t.sub || '')}</div>`;
    return `<div class="tile"><div class="v">${esc(t.v)}<small>${esc(t.unit || '')}</small></div>`
      + `<div class="k">${esc(t.k)}</div>${body}</div>`;
  }

  // One 24 h mini trend card: window mean, sparkline, min–max range.
  function miniEl(t, i) {
    const sp = CS.path(t.pts.map((p) => p.v), 120, 34, { padTop: 5, padBottom: 5 });
    const n = (v) => (v == null ? '—' : v.toFixed(t.dp));
    return `<div class="mini" data-mini="${i}">`
      + `<div class="mh"><span class="mk">${esc(t.name)}</span>`
      + `<span class="mv">${esc(n(t.avg))}<small>${esc(t.unit)}</small></span></div>`
      + '<div class="mwrap"><svg viewBox="0 0 120 34" preserveAspectRatio="none" aria-hidden="true">'
      + `<path class="spark-fill" fill="url(#glanceGrad)" d="${esc(sp.fill)}"></path>`
      + `<path class="spark-line" vector-effect="non-scaling-stroke" d="${esc(sp.line)}"></path>`
      + '</svg><i class="mguide" hidden></i></div>'
      + `<div class="ms">${esc(miniRange(t))}</div></div>`;
  }
  const miniRange = (t) => (t.min == null ? '—'
    : 'avg · ' + t.min.toFixed(t.dp) + '–' + t.max.toFixed(t.dp) + ' ' + t.unit);

  // ── Glance ────────────────────────────────────────────────────────────────
  const glance = {
    buf: [],
    hist: [],            // [{t, v: gen tok/s, p: prompt tok/s}] over 24 h
    trends: [],          // mini trend cards, derived from the same rows
    histAt: 0,
    // Fleet history from the alarm engine, refreshed every 60 s on the 2 s
    // poll and immediately on an explicit refresh. Falls back to the live
    // buffer when it returns nothing. fleet=all aggregates ACROSS hosts —
    // the unscoped endpoint lets the last host writing a timestamp win.
    async loadHistory(force) {
      const now = Date.now() / 1000;
      if (!force && now - this.histAt < 60) return;
      const sum = (r, keys) => {
        const v = keys.map((k) => r[k]).filter((x) => typeof x === 'number' && isFinite(x));
        return v.length ? v.reduce((a, b) => a + b, 0) : 0;
      };
      try {
        const raw = await jfetch(
          '/api/history?since_minutes=1440&max_rows=180&fleet=all');
        const rows = Array.isArray(raw) ? raw : [];
        this.trends = CV.trends(rows);
        this.hist = rows.map((r) => ({
          t: CV.tsSeconds(r.ts),
          v: sum(r, ['llama_tps', 'lms_tps', 'vllm_tps']),
          p: sum(r, ['llama_pps', 'lms_pps', 'vllm_pps']),
        })).filter((x) => x.t != null);
        // Only latch the throttle on success, so a failed read can retry.
        this.histAt = now;
      } catch (_) { /* keep whatever we had */ }
    },
    async refresh(force) {
      const [m, ls, lms, vllm, en] = await Promise.all([
        jfetch('/api/metrics').catch(() => ({})),
        jfetch('/api/llama-state').catch(() => ({})),
        jfetch('/api/lmstudio/metrics').catch(() => ({})),
        jfetch('/api/vllm/metrics').catch(() => ({})),
        jfetch('/api/energy/summary?days=1' + TZ_Q).catch(() => ({})),
      ]);
      setLive(ls.agent_online, ls.agent_age_s);
      // Before the view model: the fleet tiles read the 24 h peak off it.
      await this.loadHistory(force);
      const vm = CV.glance({ metrics: m, llama: ls, lms, vllm, energy: en,
        hist: this.hist, trends: this.trends });
      // Live buffer backs the strip only when history is unavailable. Drop
      // samples older than the window so a backgrounded app doesn't redraw a
      // frozen trace when it comes forward.
      const nowS = Date.now() / 1000;
      this.buf.push({ t: nowS, v: vm.hero.tps, p: 0 });
      this.buf = this.buf.filter((s) => nowS - s.t < 300).slice(-150);
      this.render(vm);
      $('glanceUpdated').textContent = 'updated ' + new Date()
        .toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit',
          second: '2-digit' })
        + (this.histAt ? ' · trends ' + CV.clockAt(this.histAt) : '');
    },
    // 24 h history when the alarm engine has it, else the live 2 s buffer.
    series() {
      return this.hist.length > 1 ? this.hist : this.buf;
    },
    render(vm) {
      $('glanceHeroN').innerHTML = `${esc(vm.hero.n)}<small>${esc(vm.hero.unit)}</small>`;
      $('glanceHeroL').textContent = vm.hero.label;
      const pts = this.series();
      // Generation and prompt share one scale, else the two lines can't be
      // read against each other.
      const all = pts.map((p) => p.v).concat(pts.map((p) => p.p || 0))
        .filter((v) => typeof v === 'number' && isFinite(v));
      const scale = { min: Math.min(...all, 0), max: Math.max(...all, 1),
        padTop: stripPad('glanceStrip', 'glanceHeroL') };
      const sp = CS.path(pts.map((p) => p.v), 340, 118, scale);
      const pp = CS.path(pts.map((p) => p.p || 0), 340, 118, scale);
      this.sp = sp;
      this.pp = pts.some((p) => p.p > 0) ? pp : null;
      $('glanceSparkLine').setAttribute('d', sp.line);
      $('glanceSparkFill').setAttribute('d', sp.fill);
      $('glanceSparkPrompt').setAttribute('d', pp.line);
      $('glanceLegend').hidden = !pts.some((p) => p.p > 0);
      $('glanceWin').textContent = this.hist.length > 1 ? 'last 24 h'
        : (this.buf.length * 2 < 90 ? 'live'
          : 'last ' + Math.round(this.buf.length * 2 / 60) + 'm');
      $('glanceProviders').innerHTML = vm.providers.map(providerRow).join('');
      $('glanceFleet').innerHTML = vm.fleet.map(tileEl).join('');
      // Minis only change when history reloads (5 min); re-rendering them on
      // the 2 s poll would wipe an open scrub readout mid-touch.
      const key = this.trends.map((t) => t.key + ':' + t.pts.length + ':' + t.avg).join('|');
      if (key !== this._minKey) {
        this._minKey = key;
        $('glanceMinis').innerHTML = this.trends.map(miniEl).join('');
      }
      $('glanceTiles').innerHTML = vm.tiles.map(tileEl).join('');
    },
    // Tap a mini card to read the value under the finger; the sub line
    // carries the readout and reverts to the 24 h range on release.
    scrubMini(e) {
      const card = e.target.closest('[data-mini]');
      if (!card) return;
      const t = this.trends[+card.dataset.mini];
      if (!t || !t.pts.length) return;
      // Clear first: one shared timer, so moving to another card would
      // otherwise strand the previous card's readout.
      this.clearMiniScrub();
      const wrap = card.querySelector('.mwrap');
      const r = wrap.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
      const i = Math.min(t.pts.length - 1, Math.round(f * (t.pts.length - 1)));
      const p = t.pts[i];
      card.querySelector('.ms').textContent =
        p.v.toFixed(t.dp) + ' ' + t.unit + ' · ' + CV.clockAt(p.t);
      const g = card.querySelector('.mguide');
      g.hidden = false;
      g.style.left = (i / Math.max(1, t.pts.length - 1) * r.width) + 'px';
      clearTimeout(this._miniT);
      this._miniT = setTimeout(() => this.clearMiniScrub(), 2800);
    },
    clearMiniScrub() {
      $('glanceMinis').querySelectorAll('[data-mini]').forEach((c) => {
        const t = this.trends[+c.dataset.mini];
        if (t) c.querySelector('.ms').textContent = miniRange(t);
        c.querySelector('.mguide').hidden = true;
      });
    },
  };

  // ── Alerts ────────────────────────────────────────────────────────────────
  const alerts = {
    filter: 'all', vm: { firing: [], earlier: [], counts: { badge: 0 } },
    row(a) {
      const ack = a.ackable
        ? `<button class="ackbtn" data-ack="${esc(a.id)}">Ack</button>` : '';
      const rule = a.rule ? `<div class="aw rule">${esc(a.rule)}</div>` : '';
      return `<div class="alert"><div class="sev ${a.sev}">${esc(a.glyph)}</div>`
        + `<div class="atext"><div class="am">${esc(a.msg)}</div>${rule}`
        + `<div class="aw">${esc(a.meta)}</div>`
        + `<div class="sevword ${a.tone || a.sev}">${esc(a.word)}</div></div>${ack}</div>`;
    },
    async refresh() {
      const list = await jfetch('/api/alarm/alerts/?limit=100&include_closed=true')
        .catch(() => []);
      this.vm = CV.alerts(Array.isArray(list) ? list : (list.alerts || []));
      setBadge(this.vm.counts.badge);
      this.apply();
    },
    apply() {
      const f = this.filter, vm = this.vm;
      let firing = vm.firing;
      if (f === 'critical') firing = firing.filter((r) => r.sev === 'crit');
      else if (f === 'warning') firing = firing.filter((r) => r.sev === 'warn');
      else if (f === 'info') firing = firing.filter((r) => r.info);
      else if (f === 'resolved') firing = [];
      const showEarlier = (f === 'all' || f === 'resolved') && vm.earlier.length;
      $('alertsFiring').innerHTML = firing.map((r) => this.row(r)).join('');
      $('alertsEarlierWrap').hidden = !showEarlier;
      if (showEarlier) $('alertsEarlier').innerHTML = vm.earlier.map((r) => this.row(r)).join('');
      const empty = firing.length === 0 && !showEarlier;
      $('alertsEmpty').hidden = !empty;
      if (empty) {
        const label = { critical: 'No critical alerts', warning: 'No warning alerts',
          info: 'No info alerts', resolved: 'Nothing earlier today' }[f] || 'All clear';
        $('alertsEmpty').querySelector('.big').textContent = label;
      }
    },
    async ack(id) {
      try { await jfetch(`/api/alarm/alerts/${encodeURIComponent(id)}/acknowledge`,
        { method: 'POST' }); } catch (_) { /* refresh reflects state */ }
      this.refresh();
    },
    start() {
      $('alertChips').querySelectorAll('.chip').forEach((c) => {
        c.onclick = () => {
          this.filter = c.dataset.filter;
          $('alertChips').querySelectorAll('.chip').forEach((x) =>
            x.classList.toggle('on', x === c));
          this.apply();
        };
      });
      document.getElementById('scr-alerts').onclick = (e) => {
        const b = e.target.closest('[data-ack]');
        if (b) this.ack(b.dataset.ack);
      };
      $('alertsRefresh').onclick = async () => {
        const btn = $('alertsRefresh');
        btn.disabled = true;
        btn.classList.add('busy');
        try { await this.refresh(true); } finally {
          btn.disabled = false;
          btn.classList.remove('busy');
        }
      };
    },
  };

  function setBadge(n) {
    const b = $('alertBadge');
    if (n > 0) { b.textContent = n > 99 ? '99+' : n; b.hidden = false; }
    else b.hidden = true;
    const tab = $('tabbar').querySelector('.tab[data-tab="alerts"]');
    if (tab) tab.setAttribute('aria-label', n > 0 ? `Alerts, ${n} unread` : 'Alerts');
  }

  // ── Energy ────────────────────────────────────────────────────────────────
  const energy = {
    hourly: [],
    // Average watts for one hourly bucket. Wh over a full hour already IS
    // watts; only the in-progress bucket needs scaling by elapsed wall time
    // (observed_s can't be used — it is summed across every agent).
    bucketWatts(r, nowS) {
      if (!r) return null;
      const now = nowS == null ? Date.now() / 1000 : nowS;
      const elapsed = Math.max(60, Math.min(3600, now - r.hour_ts));
      return (r.energy_wh || 0) * 3600 / elapsed;
    },
    async refresh() {
      const [today, month, hourly, week] = await Promise.all([
        jfetch('/api/energy/summary?days=1' + TZ_Q).catch(() => ({})),
        jfetch('/api/energy/summary').catch(() => ({})),
        jfetch('/api/energy/hourly?hours=24').catch(() => ({})),
        jfetch('/api/energy/hourly?days=7').catch(() => ({})),
      ]);
      this.render(today, month, hourly, week);
    },
    render(today, month, hourly, week) {
      const tT = today.totals || {}, mT = month.totals || {};
      // Hero reads the newest hourly bucket — the window mean barely moves.
      // Wh/bucket is only average watts once divided by the hour actually
      // observed; the current bucket is partial and would read far too low.
      // A bucket younger than 5 min holds too little to scale up without
      // reading as a spike or a cliff, so drop it from the series entirely.
      const all = (hourly.rows || []).slice().sort((a, b) => a.hour_ts - b.hour_ts);
      const tail = all[all.length - 1];
      const rows = (all.length > 1 && tail && (Date.now() / 1000 - tail.hour_ts) < 300)
        ? all.slice(0, -1) : all;
      this.hourly = rows;
      const live = rows.length
        ? this.bucketWatts(rows[rows.length - 1]) : tT.avg_watts;
      $('energyHeroN').innerHTML = `${esc(EN.fmtWatts(live).replace(' W', ''))}<small>W</small>`;
      const watts = rows.map((r) => this.bucketWatts(r));
      const sp = CS.path(watts, 340, 118, { padTop: stripPad('energyStrip', 'energyHeroL') });
      this.sp = sp;
      $('energySparkLine').setAttribute('d', sp.line);
      $('energySparkFill').setAttribute('d', sp.fill);

      const price = today.price_kwh != null ? today.price_kwh : month.price_kwh;
      const p = this.projection(today, month, price);
      $('energyTiles').innerHTML = [
        { v: EN.fmtUsd(tT.cost_usd), unit: '', k: 'Today',
          sub: tT.kwh != null ? EN.fmtKwh(tT.kwh) + (price != null ? ' · $' + price + '/kWh' : '') : 'no telemetry' },
        { v: p.value, unit: p.unit, k: 'This month', sub: p.sub },
      ].map(tileEl).join('');

      $('energyHosts').innerHTML = this.hostRows(today);
      this.dayBars(week.rows || [], price, week.start_ts);
    },
    // 30-day cost projection over every host the accumulator saw: month-to-date
    // spend first, then measured fleet draw (month, then today) × price.
    projection(today, month, price) {
      const fin = (v) => (typeof v === 'number' && isFinite(v) && v > 0 ? v : null);
      const elapsed = fin((month.window || {}).elapsed_s);
      const mT = month.totals || {}, tT = today.totals || {};
      let proj = null, basis = null;
      if (fin(mT.cost_usd) && elapsed) {
        proj = mT.cost_usd / elapsed * 30 * 86400;
        basis = 'month-to-date run rate';
      } else if (fin(mT.avg_watts) && fin(price)) {
        proj = mT.avg_watts / 1000 * 720 * price;
        basis = EN.fmtWatts(mT.avg_watts) + ' avg this month';
      } else if (fin(tT.avg_watts) && fin(price)) {
        proj = tT.avg_watts / 1000 * 720 * price;
        basis = EN.fmtWatts(tT.avg_watts) + ' avg today';
      }
      if (proj == null) return { value: '—', unit: '', sub: 'no power telemetry yet' };
      const cov = mT.power_coverage_pct;
      return {
        value: proj >= 10 ? '$' + Math.round(proj) : EN.fmtUsd(proj),
        unit: ' proj',
        sub: basis + (cov != null && cov < 95
          ? ' · ' + Math.round(cov) + '% covered' : ''),
      };
    },
    hostRows(today) {
      const VIA = { psu: 'liquidctl', mac: 'powermetrics', gpu: 'gpu driver' };
      const rows = (today.hosts || []).map((h) => {
        const src = EN.sourceLabel(h.power_source);
        const w = EN.fmtWatts(h.avg_watts);
        return providerRow({
          status: h.has_power ? 'ok' : 'idle',
          name: h.hostname || (h.agent_id || '').slice(0, 10) || '?',
          detail: src ? src + ' · ' + (VIA[h.power_source] || 'agent') : 'no power telemetry',
          // Split the unit off so it isn't spaced by a wide monospace blank.
          rN: w.replace(/ W$/, ''), rSuffix: w.endsWith(' W') ? 'W' : '',
          rUnit: 'day avg',
        });
      });
      const t = today.totals || {};
      if (t.idle_cost_usd != null) {
        const el = (today.window || {}).elapsed_s;
        const perMo = el > 0 ? t.idle_cost_usd / el * 30 * 86400 : null;
        rows.push(providerRow({
          status: 'idle', name: 'Idle floor', detail: 'models loaded, no requests',
          rN: perMo != null ? '≈ $' + Math.round(perMo) : '—', rUnit: '/mo',
        }));
      }
      return rows.join('') || '<div class="prov"><span class="pstat idle"></span>'
        + '<div class="atxt"><div class="pn">No power telemetry</div>'
        + '<div class="pd">this window</div></div></div>';
    },
    days: [],
    dayKey: (d) => d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(),
    dayBars(rows, price, startTs) {
      // bucket hourly Wh by LOCAL calendar day, cost = kWh × price
      const byDay = new Map();
      (rows || []).forEach((r) => {
        const d = new Date(r.hour_ts * 1000);
        const key = this.dayKey(d);
        if (!byDay.has(key)) {
          const mid = new Date(d);
          mid.setHours(0, 0, 0, 0);
          byDay.set(key, { wh: 0, d, key, dayStart: Math.floor(mid.getTime() / 1000) });
        }
        byDay.get(key).wh += (r.energy_wh || 0);
      });
      // A trailing window opens mid-day, so its oldest day is a stub that would
      // draw at full weight. Drop days whose midnight predates the window.
      const todayKey = this.dayKey(new Date());
      const days = [...byDay.values()]
        .filter((v) => !startTs || v.dayStart >= startTs || v.key === todayKey)
        .sort((a, b) => a.dayStart - b.dayStart)
        .slice(-7)
        .map(({ wh, d, key }) => ({ cost: (wh / 1000) * (price || 0), wh,
          today: key === todayKey,
          wd: d.toLocaleDateString('en', { weekday: 'short' }).slice(0, 2),
          full: d.toLocaleDateString('en', { weekday: 'short', month: 'short', day: 'numeric' }) }));
      this.days = days;
      const svg = $('energyDayBars');
      if (!days.length) { svg.setAttribute('aria-label', 'No daily cost data'); svg.innerHTML = ''; return; }
      svg.setAttribute('aria-label', 'Cost per day, last ' + days.length + ' days; '
        + days.map((d) => (d.today ? 'today ' : d.wd + ' ') + EN.fmtUsd(d.cost)).join(', '));
      // Draw in CSS pixels (viewBox width = measured width) so bars keep their
      // size on a tablet instead of scaling with the card.
      const W = Math.max(240, Math.round(svg.getBoundingClientRect().width) || 312);
      svg.setAttribute('viewBox', `0 0 ${W} 92`);
      const n = days.length, slot = W / n, w = Math.max(10, Math.min(34, slot * 0.5));
      const max = Math.max(...days.map((x) => x.cost), 0.01);
      let out = `<line class="base" x1="0" y1="76.5" x2="${W}" y2="76.5"/>`;
      days.forEach((d, i) => {
        const h = Math.max(4, Math.round((d.cost / max) * 52));
        const x = i * slot + (slot - w) / 2, y = 76 - h;
        out += `<rect class="${d.today ? 'today' : ''}" x="${x.toFixed(1)}" y="${y}" `
          + `width="${w.toFixed(1)}" height="${h}" rx="4"/>`;
        out += `<text x="${(x + w / 2).toFixed(1)}" y="89" text-anchor="middle"`
          + `${d.today ? ' class="hot"' : ''}>${esc(d.today ? 'today' : d.wd)}</text>`;
        if (d.today) out += `<text x="${(x + w / 2).toFixed(1)}" y="${y - 4}" `
          + `text-anchor="middle" class="hot">${esc(EN.fmtUsd(d.cost))}</text>`;
      });
      svg.innerHTML = out;
    },
  };

  // ── Admin (read-only) ─────────────────────────────────────────────────────
  // Silent unless there is genuinely something to install — "up to date" on
  // one row and nothing on the next just read as an inconsistency.
  const releaseNote = (rel) => (rel && rel.enabled && rel.update_available
    ? (rel.latest || 'update') + ' available' : '');

  const admin = {
    async refresh() {
      // Non-admins can't read any of these; skipping the calls avoids a 403
      // storm and a role-denied warning in the manager log every 10 s.
      const none = () => Promise.resolve(null);
      const [health, agents, backup, auth, audit, rel] = await Promise.all([
        ADMIN ? jfetch('/api/admin/system-health').catch(() => null) : none(),
        ADMIN ? jfetch('/api/agents').catch(() => null) : none(),
        ADMIN ? jfetch('/api/admin/backup-status').catch(() => null) : none(),
        ADMIN ? jfetch('/api/admin/auth').catch(() => null) : none(),
        ADMIN ? jfetch('/api/admin/audit-log?limit=25').catch(() => null) : none(),
        jfetch('/api/companion/release').catch(() => null),
      ]);
      const version = (document.querySelector('meta[name="mgr-version"]') || {}).content || '—';
      const gated = !ADMIN || (!health && !agents && !backup && !auth);
      const stale = ((agents || {}).agents || []).filter((a) => a.update_available);
      const vm = CV.admin({
        version, health: health || {}, agents: (agents || {}).agents || [],
        backup: backup || {}, auth: auth || {},
        agentUpdates: stale.length,
        releaseNote: releaseNote(rel),
      });
      this.render(vm, gated, audit);
    },
    msg(t, bad) {
      const m = $('adminMsg');
      m.textContent = t || '';
      m.classList.toggle('bad', !!bad);
    },
    async act(label, req) {
      this.msg(label + '…');
      try {
        const r = await req();
        if (r && r.ok === false) throw new Error(r.error || 'failed');
        this.msg(label + ' ✓');
      } catch (e) { this.msg(label + ' failed: ' + (e && e.message || e), true); }
      this.refresh();
    },
    render(vm, gated, audit) {
      $('adminGatedNote').hidden = !gated;
      $('adminManager').innerHTML = vm.services.map((s) =>
        `<div class="arow"><span class="pstat ${s.status}"></span>`
        + `<div class="atxt"><div class="an">${esc(s.name)}</div>`
        + `<div class="ad">${esc(s.detail)}</div></div>`
        + (s.right ? `<div class="pr">${esc(s.right)}</div>` : '')
        + (s.canRestart
          ? `<button class="btn" data-svc="${esc(s.key)}">Restart</button>` : '')
        + '</div>').join('');

      const pend = vm.pending || [];
      $('adminPendingWrap').hidden = !pend.length;
      $('adminPending').innerHTML = pend.map((p) =>
        `<div class="card"><div class="an">${esc(p.name)} wants to join</div>`
        + `<div class="ad">${esc(p.detail)}</div>`
        + `<div class="approve"><button class="btn primary" data-approve="${esc(p.id)}">Approve</button>`
        + `<button class="btn danger" data-deny="${esc(p.id)}">Deny</button></div></div>`).join('');

      if (gated) {
        $('adminAgents').innerHTML = '';
        $('adminRows').innerHTML = '';
        $('adminAudit').innerHTML = '';
        return;
      }
      $('adminAgents').innerHTML = vm.agents.map((a) =>
        `<div class="arow wrap"><span class="pstat ${a.status}"></span>`
        + `<div class="atxt"><div class="an">${esc(a.name)}</div>`
        + `<div class="ad">${esc(a.detail)}</div></div>`
        + `<div class="pr"><b${a.warn ? ' class="warn"' : ''}>${esc(a.right)}</b>`
        + `${esc(a.rightSub)}</div>`
        + `<button class="btn sm" data-alog="${esc(a.id)}">Logs</button>`
        + `<button class="btn sm" data-arestart="${esc(a.id)}" `
        + `data-aname="${esc(a.name)}">Restart</button></div>`).join('')
        || '<div class="arow"><div class="atxt"><div class="an">No agents</div></div></div>';
      $('adminRows').innerHTML = vm.rows.map((r) => {
        const right = r.ok == null ? ''
          : `<span class="rowstat pstat ${r.ok ? 'ok' : 'down'}"></span>`;
        return `<div class="arow"><div class="atxt"><div class="an">${esc(r.name)}</div>`
          + `<div class="ad">${esc(r.detail)}</div></div>${right}</div>`;
      }).join('');

      const rows = CV.audit((audit || {}).entries);
      $('adminAudit').innerHTML = rows.map((r) =>
        `<div class="arow"><div class="atxt"><div class="an">${esc(r.name)}</div>`
        + `<div class="ad">${esc(r.detail)}</div></div>`
        + (r.ok == null ? ''
          : `<span class="rowstat pstat ${r.ok ? 'ok' : 'down'}"></span>`)
        + '</div>').join('')
        || '<div class="arow"><div class="atxt"><div class="an">No entries</div>'
          + '<div class="ad">admin actions are recorded here</div></div></div>';
    },
    // Static snapshot by default; live tail is opt-in because an open SSE
    // stream holds a manager worker and burns phone battery.
    async openLogs(id) {
      sheet.open('Agent log', '<div class="sd">loading…</div>');
      const gen = sheet.gen;
      let lines = [];
      try {
        const r = await jfetch(`/api/agents/${encodeURIComponent(id)}/log/tail`);
        lines = (r.lines || []).slice(-120);
      } catch (e) {
        if (gen === sheet.gen) {
          $('sheetBody').innerHTML = `<div class="sd">log read failed: ${esc(e && e.message || e)}</div>`;
        }
        return;
      }
      if (gen !== sheet.gen) return;
      $('sheetBody').innerHTML =
        `<pre class="logtail">${esc(lines.length ? lines.join('\n') : '(log is empty)')}</pre>`
        + '<button class="btn" data-tail>Start live tail</button>';
      // Scoped to the sheet: the element is transient, so no global id for it.
      const pre = $('sheetBody').querySelector('.logtail');
      pre.scrollTop = pre.scrollHeight;
      $('sheetBody').onclick = (e) => {
        const b = e.target.closest('[data-tail]');
        if (b) this.toggleTail(id, b);
      };
    },
    toggleTail(id, btn) {
      if (this._es) { this.stopTail(btn); return; }
      const pre = $('sheetBody').querySelector('.logtail');
      btn.textContent = 'Stop live tail';
      btn.classList.add('danger');
      const es = new EventSource(`/api/agents/${encodeURIComponent(id)}/log/stream`);
      this._es = es;
      this._esGen = sheet.gen;
      es.onmessage = (ev) => {
        if (sheet.gen !== this._esGen) { this.stopTail(); return; }
        if (!pre.isConnected) { this.stopTail(); return; }
        pre.textContent += (pre.textContent ? '\n' : '') + ev.data;
        // Keep the buffer bounded; a chatty agent would grow it forever.
        const ls = pre.textContent.split('\n');
        if (ls.length > 400) pre.textContent = ls.slice(-400).join('\n');
        pre.scrollTop = pre.scrollHeight;
      };
      es.onerror = () => { this.stopTail(btn); };
    },
    stopTail(btn) {
      if (this._es) { this._es.close(); this._es = null; }
      if (btn) { btn.textContent = 'Start live tail'; btn.classList.remove('danger'); }
    },
    start() {
      $('scr-admin').onclick = (e) => {
        const svc = e.target.closest('[data-svc]');
        if (svc) {
          const COPY = {
            manager: 'The dashboard and this app will briefly disconnect.',
            alarm_engine: 'Alert evaluation pauses while the engine restarts.',
          };
          return sheet.confirm('Restart ' + svc.previousElementSibling.querySelector('.an').textContent,
            COPY[svc.dataset.svc] || 'The service restarts now.', 'Restart', true,
            () => this.act('restart', () => jfetch(
              `/api/admin/service/${encodeURIComponent(svc.dataset.svc)}/restart`,
              { method: 'POST' })));
        }
        const lg = e.target.closest('[data-alog]');
        if (lg) return this.openLogs(lg.dataset.alog);
        const rs = e.target.closest('[data-arestart]');
        if (rs) {
          return sheet.confirm('Restart agent on ' + rs.dataset.aname,
            'The agent process restarts. Metrics from this host pause until it '
            + 'reconnects; the provider it manages keeps running.', 'Restart', true,
            () => this.act('restart agent', () => jfetch(
              `/api/agents/${encodeURIComponent(rs.dataset.arestart)}/restart`,
              { method: 'POST' })));
        }
        const ok = e.target.closest('[data-approve]');
        if (ok) {
          return sheet.confirm('Approve agent',
            'Issue a token and admit this agent to the fleet.', 'Approve', false,
            () => this.act('approve', () => jfetch(
              `/api/agents/${encodeURIComponent(ok.dataset.approve)}/approve`, { method: 'POST' })));
        }
        const no = e.target.closest('[data-deny]');
        if (no) {
          return sheet.confirm('Deny agent',
            'Mark this agent disabled. It can re-register later.', 'Deny', true,
            () => this.act('deny', () => jfetch(
              `/api/agents/${encodeURIComponent(no.dataset.deny)}/disable`, { method: 'POST' })));
        }
      };
    },
  };

  // ── Settings ──────────────────────────────────────────────────────────────

  const settings = {
    async refresh() {
      const [me, rel] = await Promise.all([
        jfetch('/api/me').catch(() => null),
        jfetch('/api/companion/release').catch(() => null),
      ]);
      $('settingsUser').textContent = me
        ? ((me.user || me.username || 'signed in')
           + (me.role ? ' · ' + me.role : '')) : 'signed in';
      this.rel = rel;
      $('settingsRelease').innerHTML = rel
        ? '<div class="arow"><div class="atxt"><div class="an">Check for new releases</div>'
          + `<div class="ad">${esc(this.detail(rel))}</div></div>`
          + `<button class="switch" role="switch" aria-checked="${!!rel.enabled}" `
          + `aria-label="Release check" data-rel${ADMIN ? '' : ' disabled'}><i></i></button></div>`
        : '<div class="arow"><div class="atxt"><div class="an">Check for new releases</div>'
          + '<div class="ad">unavailable</div></div></div>';
      paintPush();
    },
    detail(rel) {
      const base = rel.installed
        ? 'installed ' + rel.installed + (rel.ahead ? ' +' + rel.ahead + ' commits' : '')
        : (rel.install_kind || 'unknown') + ' install · build ' + (rel.build || '—');
      if (!rel.enabled) return base + ' · off — asks github.com when on';
      if (rel.error) return base + ' · check failed: ' + rel.error;
      if (rel.update_available === null) return base + ' · ' + (rel.note || 'no verdict');
      if (rel.update_available) return base + ' · ' + (rel.latest || '—') + ' available';
      return base + ' · latest ' + (rel.latest || '—');
    },
    start() {
      $('scr-settings').onclick = async (e) => {
        const t = e.target.closest('[data-rel]');
        if (!t || t.disabled) return;
        const next = t.getAttribute('aria-checked') !== 'true';
        $('settingsMsg').textContent = next ? 'enabling…' : 'disabling…';
        try {
          await jpost('/api/companion/release', { enabled: next }, 'PUT');
          $('settingsMsg').textContent = next
            ? 'release check on' : 'release check off';
        } catch (err) {
          $('settingsMsg').textContent = 'failed: ' + (err && err.message || err);
        }
        this.refresh();
      };
    },
  };

  // ── confirm sheet ─────────────────────────────────────────────────────────
  // gen increments on every open/close; async fills compare it to detect the
  // sheet being closed or repurposed while their fetch was in flight.
  const sheet = {
    gen: 0,
    open(title, bodyHtml, onclick) {
      this.gen++;
      $('sheetTitle').textContent = title;
      $('sheetBody').innerHTML = bodyHtml;
      $('sheetBody').onclick = onclick || null;
      $('sheet').hidden = false;
      $('sheetCancel').focus();
    },
    close() {
      this.gen++;
      $('sheet').hidden = true;
      $('sheetBody').onclick = null;
      if (admin._es) admin.stopTail();
    },
    confirm(title, detail, label, danger, fn) {
      this.open(title,
        `<div class="sd">${esc(detail)}</div>`
        + `<button class="btn ${danger ? 'danger' : 'primary'}" data-go>${esc(label)}</button>`,
        (e) => { if (e.target.closest('[data-go]')) { this.close(); fn(); } });
    },
  };

  // ── Actions (control surface) ─────────────────────────────────────────────
  const models = {
    vm: null, ap: null,
    async refresh() {
      const [ls, lms, vllm, health, ap, ag] = await Promise.all([
        jfetch('/api/llama-state').catch(() => ({})),
        jfetch('/api/lmstudio/metrics').catch(() => null),
        jfetch('/api/vllm/metrics').catch(() => null),
        jfetch('/api/admin/system-health').catch(() => null),
        jfetch('/api/autopilot').catch(() => null),
        jfetch('/api/agents').catch(() => null),
      ]);
      setLive(ls.agent_online, ls.agent_age_s);
      // Last GET payload; confirmAutopilot re-reads fresh before its PUT.
      this.ap = ap;
      const version = (document.querySelector('meta[name="mgr-version"]') || {}).content || '—';
      this.vm = CV.actions({ llama: ls, lms, vllm, health, autopilot: ap, agents: ag, version });
      this.render(this.vm);
    },
    msg(t, bad) {
      const m = $('modelsMsg');
      m.textContent = t || '';
      m.classList.toggle('bad', !!bad);
    },
    async act(label, req) {
      this.msg(label + '…');
      try {
        const r = await req();
        if (r && r.ok === false) throw new Error(r.error || 'failed');
        this.msg(label + ' ✓' + (r && r.note ? ' · ' + r.note : ''));
      } catch (e) { this.msg(label + ' failed: ' + (e && e.message || e), true); }
      this.refresh();
    },
    render(vm) {
      $('modelsGatedNote').hidden = !vm.gated;
      $('modelsServices').innerHTML = vm.services.map((s) =>
        `<div class="arow"><span class="pstat ${s.status}"></span>`
        + `<div class="atxt"><div class="an">${esc(s.name)}</div>`
        + `<div class="ad">${esc(s.detail)}</div></div>`
        + `<button class="btn" data-svc="${esc(s.key)}"${s.canRestart ? '' : ' disabled'}>Restart</button></div>`).join('');
      // One row per provider; llama keeps the swap sheet, the others pin/unpin.
      $('modelsLoaded').innerHTML = (vm.models || []).map((r) =>
        `<div class="arow wrap"><div class="atxt"><div class="an">${esc(r.model || 'No model')}</div>`
        + `<div class="ad">${esc(r.detail)}</div></div>`
        + (r.canSwap ? '<button class="btn primary" data-swap>Swap…</button>'
          : (r.model ? `<button class="btn" data-pinprov="${esc(r.key)}">`
            + `${r.pinned ? 'Unpin' : 'Pin'}</button>` : ''))
        + '</div>').join('');
      // Pins on resident models already show inline above; list only the rest.
      const pins = (vm.pins || []).filter((p) => !p.resident);
      $('modelsPinsWrap').hidden = !pins.length;
      $('modelsPins').innerHTML = pins.map((p) =>
        `<div class="arow wrap"><div class="atxt"><div class="an">${esc(p.model)}</div>`
        + `<div class="ad">${esc(p.label + ' · → ' + p.host + ' · not loaded')}</div></div>`
        + `<button class="btn" data-unpin="${esc(p.provider)}" `
        + `data-unpin-model="${esc(p.model)}">Unpin</button></div>`).join('');

      const ap = vm.autopilot;
      $('modelsAutopilot').innerHTML = ap
        ? '<div class="arow"><div class="atxt"><div class="an">Model autopilot</div>'
          + `<div class="ad">${esc((ap.on ? 'active' : 'off') + ' · ' + ap.detail)}</div></div>`
          + `<button class="switch" role="switch" aria-checked="${ap.on}" `
          + 'aria-label="Autopilot" data-ap><i></i></button></div>'
          + ap.entries.map((e) =>
            `<div class="arow"><div class="atxt"><div class="an">${esc(e.model)}</div>`
            + `<div class="ad">${esc(e.detail)}</div></div>`
            + `<div class="pr"><b${e.warn ? ' class="warn"' : ''}>${esc(e.right)}</b>`
            + `${esc(e.rightSub)}</div></div>`).join('')
          + ap.settings.map((s) =>
            `<div class="arow"><div class="atxt"><div class="an">${esc(s.name)}</div>`
            + `<div class="ad">${esc(s.detail)}</div></div></div>`).join('')
        : '<div class="arow"><div class="atxt"><div class="an">Model autopilot</div>'
          + '<div class="ad">needs admin</div></div></div>';
    },
    confirmRestart(key) {
      const COPY = {
        llama: 'In-flight inference requests will be dropped while the unit restarts.',
        lms: 'The LM Studio server restarts; loaded models reload after it returns.',
        vllm: 'The vLLM unit restarts; its model reloads after it returns.',
        manager: 'The dashboard and this app will briefly disconnect.',
        alarm_engine: 'Alert evaluation pauses while the engine restarts.',
      };
      const ROUTE = {
        llama: '/api/llm/server/restart',
        lms: '/api/lmstudio/server/restart',
        vllm: '/api/vllm/server/restart',
      };
      const s = ((this.vm || {}).services || []).find((x) => x.key === key) || { name: key };
      const req = ROUTE[key]
        ? () => jfetch(ROUTE[key], { method: 'POST' })
        : () => jfetch(`/api/admin/service/${encodeURIComponent(key)}/restart`, { method: 'POST' });
      sheet.confirm('Restart ' + s.name, COPY[key] || 'The service restarts now.',
        'Restart', true, () => this.act('restart ' + s.name, req));
    },
    confirmAutopilot() {
      const snap = ((this.ap || {}).state) || { enabled: false, entries: [], hosts: {} };
      const next = !snap.enabled;
      sheet.confirm((next ? 'Enable' : 'Disable') + ' autopilot',
        next ? 'The reconciler will start placing declared models automatically.'
          : 'Model placement stops; loaded models stay as they are.',
        next ? 'Enable' : 'Disable', !next,
        // Re-reads full state at confirm time; the PUT flips only `enabled`.
        () => this.act('autopilot', async () => {
          const cur = ((await jfetch('/api/autopilot')).state) || snap;
          return jpost('/api/autopilot', Object.assign({}, cur, { enabled: next }), 'PUT');
        }));
    },
    // Pin/unpin for the non-llama providers, via /api/admin/<provider>-pins.
    setPin(provider, model, agentId) {
      const on = !!agentId;
      sheet.confirm((on ? 'Pin ' : 'Unpin ') + model,
        on ? 'Gateway requests for this model always route to this host.'
          : 'Gateway requests for this model go back to pool routing.',
        on ? 'Pin' : 'Unpin', false,
        () => this.act(on ? 'pin' : 'unpin',
          () => jpost(`/api/admin/${encodeURIComponent(provider)}-pins`,
            { model_id: model, agent_id: agentId || '' })));
    },
    confirmProviderPin(key) {
      const r = ((this.vm || {}).models || []).find((x) => x.key === key);
      if (!r || !r.model) return;
      this.setPin(key, r.model, r.pinned ? '' : r.agentId);
    },
    // Loads a model, surfacing the proxy's pin-override routing headers.
    async loadModel(id) {
      const r = await fetch('/api/llm/load', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: id }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || r.status);
      if (r.headers.get('X-Routing-Override') === 'pin') {
        body.note = 'routed to ' + (r.headers.get('X-Proxied-To') || 'its pinned host')
          + ' (pinned)';
      }
      return body;
    },
    async openSwap() {
      sheet.open('Swap model', '<div class="sd">loading model list…</div>');
      const gen = sheet.gen;
      let list = [];
      try { list = (await jfetch('/api/llm/models')).data || []; }
      catch (e) {
        if (gen === sheet.gen) {
          $('sheetBody').innerHTML = `<div class="sd">model list failed: ${esc(e && e.message || e)}</div>`;
        }
        return;
      }
      // The sheet was closed or repurposed while the list was loading.
      if (gen !== sheet.gen) return;
      const vm = this.vm || {};
      const m = vm.model || {};
      const cur = m.name;
      const rows = list.map((mm) => {
        const st = ((mm.status || {}).value || '').toLowerCase();
        const isCur = mm.id === cur || st === 'loaded' || st === 'loading';
        return `<button class="arow" data-model="${esc(mm.id)}"${isCur ? ' disabled' : ''}>`
          + `<div class="atxt"><div class="an">${esc(mm.id)}</div></div>`
          + `<div class="pr">${esc(isCur ? (st || 'loaded') : st)}</div></button>`;
      }).join('') || '<div class="sd">no models configured</div>';
      const unload = m.resident
        ? `<button class="btn danger" data-unload>Unload ${esc(cur)}</button>` : '';
      const pin = (vm.primaryLlamaId && m.resident)
        ? `<button class="btn" data-pin>${m.pinned ? 'Unpin' : 'Pin'} ${esc(cur)}</button>` : '';
      $('sheetBody').innerHTML = `<div class="sheetlist">${rows}</div>${unload}${pin}`;
      $('sheetBody').onclick = (e) => {
        const b = e.target.closest('[data-model]');
        if (b && !b.disabled) {
          const id = b.dataset.model;
          sheet.confirm('Load ' + id,
            'The current model is swapped out; the first request may be slow while it loads.',
            'Load', false, () => this.act('load', () => this.loadModel(id)));
        } else if (e.target.closest('[data-unload]')) {
          sheet.confirm('Unload ' + cur,
            'Frees VRAM; requests for this model will fail until it is reloaded.',
            'Unload', true, () => this.act('unload',
              () => jpost('/api/llm/unload', { model: cur })));
        } else if (e.target.closest('[data-pin]')) {
          sheet.confirm((m.pinned ? 'Unpin ' : 'Pin ') + cur,
            m.pinned ? 'Gateway requests for this model go back to pool routing.'
              : 'Gateway requests for this model always route to the primary llama host.',
            m.pinned ? 'Unpin' : 'Pin', false,
            () => this.act(m.pinned ? 'unpin' : 'pin', () => jpost('/api/admin/llama-pins',
              { model_id: cur, agent_id: m.pinned ? '' : vm.primaryLlamaId })));
        }
      };
    },
    start() {
      $('scr-models').onclick = (e) => {
        const svc = e.target.closest('[data-svc]');
        if (svc && !svc.disabled) return this.confirmRestart(svc.dataset.svc);
        if (e.target.closest('[data-swap]')) return this.openSwap();
        const pp = e.target.closest('[data-pinprov]');
        if (pp) return this.confirmProviderPin(pp.dataset.pinprov);
        const un = e.target.closest('[data-unpin]');
        if (un) return this.setPin(un.dataset.unpin, un.dataset.unpinModel, '');
        if (e.target.closest('[data-ap]')) return this.confirmAutopilot();
      };
    },
  };

  // ── service worker + push (Admin) ─────────────────────────────────────────
  let _reg = null;
  const standalone = () =>
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
    || window.navigator.standalone === true;

  async function subscription() {
    if (!_reg || !_reg.pushManager) return null;
    try { return await _reg.pushManager.getSubscription(); } catch (_) { return null; }
  }
  async function paintPush() {
    const ready = !!(_reg && _reg.active);
    const perm = ('Notification' in window) ? Notification.permission : 'unsupported';
    const sub = await subscription();
    $('pushStatus').textContent = !ready ? 'service worker installing…'
      : perm === 'denied' ? 'blocked — re-allow in site settings'
        : sub ? 'subscribed' : (standalone() ? 'not subscribed' : 'add to Home Screen first (iOS)');
    // One button, two states: the same control that opted this device in
    // takes it back out again.
    const btn = $('btnEnable');
    btn.textContent = sub ? 'Disable' : 'Enable';
    btn.classList.toggle('danger', !!sub);
    btn.disabled = !ready;
    $('btnTest').disabled = !sub;
    try {
      const s = await jfetch('/api/companion/push/subscriptions');
      $('pushCount').textContent = s.count + (s.count === 1 ? ' device' : ' devices');
    } catch (_) { $('pushCount').textContent = '—'; }
  }
  // Drops this device only: unsubscribe locally, then remove the stored
  // endpoint so the manager stops sending to a subscription that is gone.
  async function disablePush() {
    const sub = await subscription();
    if (!sub) { paintPush(); return; }
    $('pushMsg').textContent = 'unregistering…';
    try {
      await sub.unsubscribe().catch(() => {});
      await jpost('/api/companion/push/unsubscribe', { endpoint: sub.endpoint });
      $('pushMsg').textContent = 'this device will no longer be notified';
    } catch (err) {
      $('pushMsg').textContent = 'unregister failed: ' + (err && err.message || err);
    }
    paintPush();
  }

  async function togglePush() {
    return (await subscription()) ? disablePush() : enablePush();
  }

  async function enablePush() {
    if (!('Notification' in window) || !_reg || !_reg.pushManager) {
      $('pushMsg').textContent = 'push needs an installed app (iOS 16.4+)'; return;
    }
    if (await Notification.requestPermission() !== 'granted') { paintPush(); return; }
    let sub = null;
    try {
      await navigator.serviceWorker.ready;
      const key = (await jfetch('/api/companion/push/public-key')).key;
      sub = await _reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: window.PushUtil.urlB64ToUint8Array(key),
      });
      const res = await jfetch('/api/companion/push/subscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
      });
      if (!res.ok) { await sub.unsubscribe().catch(() => {}); $('pushMsg').textContent = res.error || 'failed'; }
      else $('pushMsg').textContent = 'subscribed ✓';
    } catch (err) {
      if (sub) await sub.unsubscribe().catch(() => {});
      $('pushMsg').textContent = 'subscribe failed: ' + (err && err.message || err);
    }
    paintPush();
  }
  async function testPush() {
    $('pushMsg').textContent = 'sending…';
    try {
      const sub = await subscription();
      const res = await jfetch('/api/companion/push/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub ? { endpoint: sub.endpoint } : {}),
      });
      $('pushMsg').textContent = res.sent ? `sent ${res.sent} ✓` : (res.error || `failed ${res.failed}`);
    } catch (err) { $('pushMsg').textContent = 'send failed: ' + (err && err.message || err); }
  }

  // ── router ────────────────────────────────────────────────────────────────
  const SCREENS = {
    glance: { title: 'LLM Systems Manager', ctrl: glance, interval: 2000 },
    alerts: { title: 'Alerts', ctrl: alerts, interval: 15000 },
    energy: { title: 'Energy', ctrl: energy, interval: 30000 },
    models: { title: 'Models', ctrl: models, interval: 10000 },
    admin: { title: 'Admin', ctrl: admin, interval: 10000, admin: true },
    settings: { title: 'Settings', ctrl: settings, interval: 0 },
  };
  let timer = null;
  let current = 'glance';

  // Deep link from a push notification: /companion?tab=alerts.
  function tabFromUrl(search) {
    const t = new URLSearchParams(search == null ? location.search : search).get('tab');
    return (t && SCREENS[t]) ? t : null;
  }

  // force=true on an explicit refresh: controllers skip their own throttles so
  // a pull-to-refresh really re-reads everything, not just the cheap polls.
  const refreshCurrent = (force) => {
    const cfg = SCREENS[current];
    return cfg && cfg.ctrl ? Promise.resolve(cfg.ctrl.refresh(force)) : Promise.resolve();
  };

  // iOS Safari ignores user-scalable=no, so pinch has to be refused directly:
  // the WebKit gesture events plus any multi-touch move.
  function blockZoom() {
    ['gesturestart', 'gesturechange', 'gestureend'].forEach((n) =>
      document.addEventListener(n, (e) => e.preventDefault(), { passive: false }));
    document.addEventListener('touchmove', (e) => {
      if (e.touches.length > 1 && e.cancelable) e.preventDefault();
    }, { passive: false });
  }

  // Pull-to-refresh: only from the top of the active screen, only on a
  // downward drag, and never while the confirm sheet is up.
  function initPullToRefresh() {
    const THRESHOLD = 62, MAX = 96;
    const ind = $('ptr');
    let startY = null, dy = 0, busy = false;
    const active = () => document.querySelector('.screen:not([hidden])');

    document.addEventListener('touchstart', (e) => {
      const scr = active();
      if (busy || !scr || e.touches.length !== 1 || !$('sheet').hidden
          || scr.scrollTop > 0) { startY = null; return; }
      startY = e.touches[0].clientY;
      dy = 0;
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
      if (startY == null) return;
      const scr = active();
      dy = e.touches[0].clientY - startY;
      if (dy <= 0 || !scr || scr.scrollTop > 0) { startY = null; dy = 0; ind.style.opacity = ''; ind.style.transform = ''; return; }
      const pull = Math.min(dy * 0.5, MAX);
      ind.style.opacity = String(Math.min(1, pull / THRESHOLD));
      ind.style.transform = `translateY(${pull - 56}px)`;
      ind.classList.toggle('armed', pull >= THRESHOLD);
      if (e.cancelable) e.preventDefault();
    }, { passive: false });

    const end = async () => {
      if (startY == null) return;
      const fire = dy * 0.5 >= THRESHOLD;
      startY = null; dy = 0;
      ind.style.opacity = ''; ind.style.transform = '';
      ind.classList.remove('armed');
      if (!fire || busy) return;
      busy = true;
      ind.classList.add('busy');
      try { await refreshCurrent(true); } catch (_) { /* screen shows its own state */ }
      ind.classList.remove('busy');
      busy = false;
    };
    document.addEventListener('touchend', end, { passive: true });
    document.addEventListener('touchcancel', end, { passive: true });
  }

  function show(tab) {
    let cfg = SCREENS[tab];
    // Defensive: the tab button is hidden for non-admins, but the router must
    // refuse the screen too rather than trusting the button.
    if (cfg && cfg.admin && !ADMIN) { tab = 'glance'; cfg = SCREENS[tab]; }
    if (!cfg) return;
    current = tab;
    if (timer) { clearInterval(timer); timer = null; }
    Object.keys(SCREENS).forEach((t) => { $('scr-' + t).hidden = t !== tab; });
    $('tabbar').querySelectorAll('.tab').forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle('on', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    $('appTitle').textContent = cfg.title;
    // Liveness is a Home-screen statement; on other tabs it just adds noise.
    document.querySelector('.hostchip').hidden = tab !== 'glance';
    if (cfg.ctrl) {
      cfg.ctrl.refresh();
      // Paused while the confirm sheet is open or the app is backgrounded.
      if (cfg.interval) timer = setInterval(() => {
        if (document.visibilityState === 'visible' && $('sheet').hidden) cfg.ctrl.refresh();
      }, cfg.interval);
    }
  }

  async function pollBadge() {
    try {
      // only_active = active + acknowledged, the same set the Alerts screen's
      // firing count uses (CV.alerts then drops info-category).
      const list = await jfetch('/api/alarm/alerts/?only_active=true&limit=100');
      const arr = Array.isArray(list) ? list : (list.alerts || []);
      setBadge(CV.alerts(arr).counts.badge);
    } catch (_) { /* leave badge as-is */ }
  }

  document.addEventListener('DOMContentLoaded', async () => {
    // Fail closed: /api/me is session-readable and reports admin_access =
    // is_admin && admin_ip, the same pair the server-side gate checks.
    try {
      const me = await jfetch('/api/me');
      ADMIN = me.admin_access === true;
    } catch (_) { ADMIN = false; }
    if (!ADMIN) {
      const tab = $('tabbar').querySelector('.tab[data-tab="admin"]');
      if (tab) tab.hidden = true;
    }
    await applyTheme();
    if ('serviceWorker' in navigator) {
      try { _reg = await navigator.serviceWorker.register('/sw.js', { scope: '/companion' }); }
      catch (_) { _reg = null; }
      const repaint = () => { if (!$('scr-settings').hidden) paintPush(); };
      navigator.serviceWorker.ready.then(repaint).catch(() => {});
      navigator.serviceWorker.addEventListener('controllerchange', repaint);
      // Tapping an alarm notification lands here when the app is already open.
      navigator.serviceWorker.addEventListener('message', (e) => {
        const d = e.data || {};
        if (d.type !== 'lsm-open' || !d.url) return;
        const t = tabFromUrl(new URL(d.url, location.origin).search);
        if (t) show(t);
      });
    }
    alerts.start();
    models.start();
    admin.start();
    settings.start();
    blockZoom();
    initPullToRefresh();
    const clock = (t) => new Date(t * 1000)
      .toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
    attachScrub('glanceStrip', 'glanceReadout', 'glanceMarker', () => {
      const pts = glance.series();
      return {
        values: pts.map((p) => p.v),
        pts: (glance.sp || {}).pts,
        pts2: (glance.pp || {}).pts,
        label: (i) => {
          const p = pts[i];
          if (!p) return '';
          return 'gen ' + p.v.toFixed(1)
            + (p.p > 0 ? ' · prompt ' + p.p.toFixed(1) : '')
            + ' tok/s · ' + clock(p.t);
        },
      };
    });
    attachScrub('energyStrip', 'energyReadout', 'energyMarker', () => ({
      values: energy.hourly.map((r) => energy.bucketWatts(r)),
      pts: (energy.sp || {}).pts,
      label: (i) => {
        const r = energy.hourly[i];
        if (!r) return '';
        return EN.fmtWatts(energy.bucketWatts(r)) + ' · '
          + new Date(r.hour_ts * 1000).toLocaleTimeString('en', { hour: 'numeric' });
      },
    }));
    $('glanceMinis').addEventListener('pointerdown', (e) => glance.scrubMini(e));
    $('glanceMinis').addEventListener('pointermove', (e) => {
      if (e.buttons) glance.scrubMini(e);
    });
    $('energyDayBars').addEventListener('pointerdown', (e) => {
      const svg = $('energyDayBars'), days = energy.days || [];
      if (!days.length) return;
      const r = svg.getBoundingClientRect();
      const i = Math.max(0, Math.min(days.length - 1,
        Math.floor((e.clientX - r.left) / (r.width / days.length))));
      const bars = svg.querySelectorAll('rect');
      bars.forEach((b, k) => b.classList.toggle('sel', k === i));
      const out = $('energyBarReadout');
      out.textContent = (days[i].today ? 'today' : days[i].full) + ' · '
        + EN.fmtUsd(days[i].cost) + ' · ' + EN.fmtKwh(days[i].wh / 1000);
      out.classList.add('on');
      clearTimeout(energy._barT);
      energy._barT = setTimeout(() => {
        out.classList.remove('on');
        bars.forEach((b) => b.classList.remove('sel'));
      }, 2800);
    });
    $('sheet').addEventListener('click', (e) => {
      if (e.target.closest('[data-sheet-close]')) sheet.close();
    });
    $('themeChips').addEventListener('click', (e) => {
      const c = e.target.closest('[data-ctheme]');
      if (!c) return;
      if (c.dataset.ctheme === 'auto') localStorage.removeItem('companionTheme');
      else localStorage.setItem('companionTheme', c.dataset.ctheme);
      applyTheme();
    });
    $('btnEnable').addEventListener('click', togglePush);
    $('btnTest').addEventListener('click', testPush);
    $('tabbar').querySelectorAll('.tab').forEach((b) =>
      b.addEventListener('click', () => show(b.dataset.tab)));
    show(tabFromUrl() || 'glance');
    pollBadge();
    setInterval(pollBadge, 30000);
  });
})();
