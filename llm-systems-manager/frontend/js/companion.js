// Companion shell (#522): five-tab router, per-screen controllers, push
// opt-in, and the Actions control surface + confirm sheet. Classic IIFE.
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
      const n = (s.values || []).length;
      if (!n) return;
      const r = strip.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      const i = Math.round(f * (n - 1));
      out.textContent = s.label(i);
      out.classList.add('on');
      // Snap to the point, not the finger, and clamp the bubble on its own
      // half-width so the ends stay on screen.
      const pt = (s.pts || [])[i];
      const px = pt ? pt.x / 340 * r.width : f * r.width;
      const half = out.offsetWidth / 2;
      out.style.left = Math.max(half + 6, Math.min(r.width - half - 6, px)) + 'px';
      if (mark && pt) {
        const py = pt.y / 118 * r.height;
        mark.hidden = false;
        mark.style.left = px + 'px';
        mark.querySelector('.mdot').style.top = py + 'px';
        mark.querySelector('.mline').style.top = py + 'px';
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

  // ── Glance ────────────────────────────────────────────────────────────────
  const glance = {
    buf: [],
    hist: [],            // [{t: epochSec, v: fleet tok/s}] over 24 h
    histAt: 0,
    // Fleet tok/s history from the alarm engine; refreshed every 5 min, not
    // on the 2 s poll. Falls back to the live buffer when it returns nothing.
    async loadHistory() {
      const now = Date.now() / 1000;
      if (now - this.histAt < 300) return;
      this.histAt = now;
      try {
        const rows = await jfetch('/api/history?since_minutes=1440&max_rows=180');
        this.hist = (Array.isArray(rows) ? rows : []).map((r) => {
          const parts = [r.llama_tps, r.lms_tps, r.vllm_tps]
            .filter((v) => typeof v === 'number' && isFinite(v));
          return { t: CV.tsSeconds(r.ts), v: parts.reduce((a, b) => a + b, 0) };
        }).filter((p) => p.t != null);
      } catch (_) { /* keep whatever we had */ }
    },
    async refresh() {
      const [m, ls, lms, vllm, en] = await Promise.all([
        jfetch('/api/metrics').catch(() => ({})),
        jfetch('/api/llama-state').catch(() => ({})),
        jfetch('/api/lmstudio/metrics').catch(() => ({})),
        jfetch('/api/vllm/metrics').catch(() => ({})),
        jfetch('/api/energy/summary?days=1' + TZ_Q).catch(() => ({})),
      ]);
      setLive(ls.agent_online, ls.agent_age_s);
      const vm = CV.glance({ metrics: m, llama: ls, lms, vllm, energy: en });
      // Live buffer backs the strip only when history is unavailable.
      this.buf.push({ t: Date.now() / 1000, v: vm.hero.tps });
      if (this.buf.length > 120) this.buf.shift();
      this.loadHistory();
      this.render(vm);
    },
    // 24 h history when the alarm engine has it, else the live 2 s buffer.
    series() {
      return this.hist.length > 1 ? this.hist : this.buf;
    },
    render(vm) {
      $('glanceHeroN').innerHTML = `${esc(vm.hero.n)}<small>${esc(vm.hero.unit)}</small>`;
      $('glanceHeroL').textContent = vm.hero.label;
      const pts = this.series();
      const sp = CS.path(pts.map((p) => p.v), 340, 118,
        { padTop: stripPad('glanceStrip', 'glanceHeroL') });
      this.sp = sp;
      $('glanceSparkLine').setAttribute('d', sp.line);
      $('glanceSparkFill').setAttribute('d', sp.fill);
      $('glanceWin').textContent = this.hist.length > 1 ? 'last 24 h'
        : (this.buf.length * 2 < 90 ? 'live'
          : 'last ' + Math.round(this.buf.length * 2 / 60) + 'm');
      $('glanceProviders').innerHTML = vm.providers.map(providerRow).join('');
      $('glanceTiles').innerHTML = vm.tiles.map(tileEl).join('');
    },
  };

  // ── Alerts ────────────────────────────────────────────────────────────────
  const alerts = {
    filter: 'all', vm: { firing: [], earlier: [], counts: { badge: 0 } },
    row(a) {
      const ack = a.ackable
        ? `<button class="ackbtn" data-ack="${esc(a.id)}">Ack</button>` : '';
      return `<div class="alert"><div class="sev ${a.sev}">${esc(a.glyph)}</div>`
        + `<div class="atext"><div class="am">${esc(a.msg)}</div>`
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
        try { await this.refresh(); } finally {
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
  const admin = {
    async refresh() {
      const [health, agents, backup, auth] = await Promise.all([
        jfetch('/api/admin/system-health').catch(() => null),
        jfetch('/api/agents').catch(() => null),
        jfetch('/api/admin/backup-status').catch(() => null),
        jfetch('/api/admin/auth').catch(() => null),
      ]);
      const version = (document.querySelector('meta[name="mgr-version"]') || {}).content || '—';
      const gated = !health && !agents && !backup && !auth;
      const stale = ((agents || {}).agents || []).filter((a) => a.update_available);
      const vm = CV.admin({
        version, health: health || {}, agents: (agents || {}).agents || [],
        backup: backup || {}, auth: auth || {},
        agentUpdates: stale.length,
      });
      this.render(vm, gated);
      paintPush();
    },
    render(vm, gated) {
      $('adminManager').innerHTML = providerRow({
        status: 'ok', name: 'Manager',
        detail: vm.manager.version + (vm.manager.uptime ? ' · up ' + vm.manager.uptime : ''),
        rN: '', rUnit: vm.manager.updateNote,
      });
      if (gated) {
        const note = '<div class="prov"><span class="pstat idle"></span>'
          + '<div class="atxt"><div class="pn">Admin status hidden</div>'
          + '<div class="pd">needs an admin session from an allowed network</div></div></div>';
        $('adminAgents').innerHTML = note;
        $('adminRows').innerHTML = '';
        return;
      }
      $('adminAgents').innerHTML = vm.agents.map((a) => providerRow({
        status: a.status, name: a.name, detail: a.detail,
        rN: a.right, rUnit: a.rightSub, warn: a.warn,
      })).join('') || '<div class="prov"><span class="pstat idle"></span>'
        + '<div class="atxt"><div class="pn">No agents</div></div></div>';
      $('adminRows').innerHTML = vm.rows.map((r) => {
        const right = r.ok == null ? ''
          : `<span class="rowstat pstat ${r.ok ? 'ok' : 'idle'}"></span>`;
        return `<div class="arow"><div class="atxt"><div class="an">${esc(r.name)}</div>`
          + `<div class="ad">${esc(r.detail)}</div></div>${right}</div>`;
      }).join('');
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
    close() { this.gen++; $('sheet').hidden = true; $('sheetBody').onclick = null; },
    confirm(title, detail, label, danger, fn) {
      this.open(title,
        `<div class="sd">${esc(detail)}</div>`
        + `<button class="btn ${danger ? 'danger' : 'primary'}" data-go>${esc(label)}</button>`,
        (e) => { if (e.target.closest('[data-go]')) { this.close(); fn(); } });
    },
  };

  // ── Actions (control surface) ─────────────────────────────────────────────
  const actions = {
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
      const m = $('actionsMsg');
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
      $('actionsGatedNote').hidden = !vm.gated;
      $('actionsServices').innerHTML = vm.services.map((s) =>
        `<div class="arow"><span class="pstat ${s.status}"></span>`
        + `<div class="atxt"><div class="an">${esc(s.name)}</div>`
        + `<div class="ad">${esc(s.detail)}</div></div>`
        + `<button class="btn" data-svc="${esc(s.key)}"${s.canRestart ? '' : ' disabled'}>Restart</button></div>`).join('');
      // One row per provider; llama keeps the swap sheet, the others pin/unpin.
      $('actionsModel').innerHTML = (vm.models || []).map((r) =>
        `<div class="arow wrap"><div class="atxt"><div class="an">${esc(r.model || 'No model')}</div>`
        + `<div class="ad">${esc(r.detail)}</div></div>`
        + (r.canSwap ? '<button class="btn primary" data-swap>Swap…</button>'
          : (r.model ? `<button class="btn" data-pinprov="${esc(r.key)}">`
            + `${r.pinned ? 'Unpin' : 'Pin'}</button>` : ''))
        + '</div>').join('');
      // Pins on resident models already show inline above; list only the rest.
      const pins = (vm.pins || []).filter((p) => !p.resident);
      $('actionsPinsWrap').hidden = !pins.length;
      $('actionsPins').innerHTML = pins.map((p) =>
        `<div class="arow wrap"><div class="atxt"><div class="an">${esc(p.model)}</div>`
        + `<div class="ad">${esc(p.label + ' · → ' + p.host + ' · not loaded')}</div></div>`
        + `<button class="btn" data-unpin="${esc(p.provider)}" `
        + `data-unpin-model="${esc(p.model)}">Unpin</button></div>`).join('');

      const ap = vm.autopilot;
      $('actionsAutopilot').innerHTML = ap
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
      // "No pending agents" is only claimed when the agents read succeeded.
      $('actionsAgents').innerHTML = vm.pending.map((p) =>
        `<div class="card"><div class="an">${esc(p.name)} wants to join</div>`
        + `<div class="ad">${esc(p.detail)}</div>`
        + `<div class="approve"><button class="btn primary" data-approve="${esc(p.id)}">Approve</button>`
        + `<button class="btn danger" data-deny="${esc(p.id)}">Deny</button></div></div>`).join('')
        || (vm.agentsKnown ? '<div class="provwrap"><div class="arow"><div class="atxt">'
          + '<div class="an">No pending agents</div></div></div></div>' : '');
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
      $('scr-actions').onclick = (e) => {
        const svc = e.target.closest('[data-svc]');
        if (svc && !svc.disabled) return this.confirmRestart(svc.dataset.svc);
        if (e.target.closest('[data-swap]')) return this.openSwap();
        const pp = e.target.closest('[data-pinprov]');
        if (pp) return this.confirmProviderPin(pp.dataset.pinprov);
        const un = e.target.closest('[data-unpin]');
        if (un) return this.setPin(un.dataset.unpin, un.dataset.unpinModel, '');
        if (e.target.closest('[data-ap]')) return this.confirmAutopilot();
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
    $('btnEnable').disabled = !!sub || !ready;
    $('btnTest').disabled = !sub;
    try {
      const s = await jfetch('/api/companion/push/subscriptions');
      $('pushCount').textContent = s.count + (s.count === 1 ? ' device' : ' devices');
    } catch (_) { $('pushCount').textContent = '—'; }
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
    actions: { title: 'Actions', ctrl: actions, interval: 10000 },
    admin: { title: 'Admin', ctrl: admin, interval: 10000 },
  };
  let timer = null;
  let current = 'glance';

  const refreshCurrent = () => {
    const cfg = SCREENS[current];
    return cfg && cfg.ctrl ? Promise.resolve(cfg.ctrl.refresh()) : Promise.resolve();
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
      try { await refreshCurrent(); } catch (_) { /* screen shows its own state */ }
      ind.classList.remove('busy');
      busy = false;
    };
    document.addEventListener('touchend', end, { passive: true });
    document.addEventListener('touchcancel', end, { passive: true });
  }

  function show(tab) {
    const cfg = SCREENS[tab];
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
    await applyTheme();
    if ('serviceWorker' in navigator) {
      try { _reg = await navigator.serviceWorker.register('/sw.js', { scope: '/companion' }); }
      catch (_) { _reg = null; }
      const repaint = () => { if (!$('scr-admin').hidden) paintPush(); };
      navigator.serviceWorker.ready.then(repaint).catch(() => {});
      navigator.serviceWorker.addEventListener('controllerchange', repaint);
    }
    alerts.start();
    actions.start();
    blockZoom();
    initPullToRefresh();
    const clock = (t) => new Date(t * 1000)
      .toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
    attachScrub('glanceStrip', 'glanceReadout', 'glanceMarker', () => {
      const pts = glance.series();
      return {
        values: pts.map((p) => p.v),
        pts: (glance.sp || {}).pts,
        label: (i) => pts[i].v.toFixed(1) + ' tok/s · ' + clock(pts[i].t),
      };
    });
    attachScrub('energyStrip', 'energyReadout', 'energyMarker', () => ({
      values: energy.hourly.map((r) => energy.bucketWatts(r)),
      pts: (energy.sp || {}).pts,
      label: (i) => EN.fmtWatts(energy.bucketWatts(energy.hourly[i])) + ' · '
        + new Date(energy.hourly[i].hour_ts * 1000)
          .toLocaleTimeString('en', { hour: 'numeric' }),
    }));
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
    $('btnEnable').addEventListener('click', enablePush);
    $('btnTest').addEventListener('click', testPush);
    $('tabbar').querySelectorAll('.tab').forEach((b) =>
      b.addEventListener('click', () => show(b.dataset.tab)));
    show('glance');
    pollBadge();
    setInterval(pollBadge, 30000);
  });
})();
