// Companion shell (#522): five-tab router + per-screen controllers, the
// service-worker/push opt-in (Admin screen), and the Actions control surface
// with its bottom confirm sheet. IIFE — classic global scope.
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

  // ── theme + host liveness ────────────────────────────────────────────────
  async function applyTheme() {
    try {
      const layout = await jfetch('/api/layout');
      if (layout && layout.theme)
        document.documentElement.setAttribute('data-theme', layout.theme);
    } catch (_) { /* default theme */ }
    const bg = getComputedStyle(document.documentElement)
      .getPropertyValue('--bg-tabnav').trim();
    const meta = document.querySelector('meta[name="theme-color"]');
    if (bg && meta) meta.setAttribute('content', bg);
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
    return `<div class="prov"><span class="pstat ${p.status}"></span>`
      + `<div class="atxt"><div class="pn">${esc(p.name)}</div>`
      + `<div class="pd">${esc(p.detail)}</div></div>`
      + `<div class="pr"><b${p.warn ? ' class="warn"' : ''}>${esc(p.rN)}</b>${esc(p.rUnit)}</div></div>`;
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
    async refresh() {
      const [m, ls, lms, vllm, en] = await Promise.all([
        jfetch('/api/metrics').catch(() => ({})),
        jfetch('/api/llama-state').catch(() => ({})),
        jfetch('/api/lmstudio/metrics').catch(() => ({})),
        jfetch('/api/vllm/metrics').catch(() => ({})),
        jfetch('/api/energy/summary?days=1').catch(() => ({})),
      ]);
      setLive(ls.agent_online, ls.agent_age_s);
      const vm = CV.glance({ metrics: m, llama: ls, lms, vllm, energy: en });
      // Buffer the hero's rate (whichever provider it is) so the strip and the
      // hero always agree.
      this.buf.push(vm.hero.tps);
      if (this.buf.length > 120) this.buf.shift();
      this.render(vm);
    },
    render(vm) {
      $('glanceHeroN').innerHTML = `${esc(vm.hero.n)} <small>${esc(vm.hero.unit)}</small>`;
      $('glanceHeroL').textContent = vm.hero.label;
      const sp = CS.path(this.buf, 340, 118);
      $('glanceSparkLine').setAttribute('d', sp.line);
      $('glanceSparkFill').setAttribute('d', sp.fill);
      const secs = this.buf.length * 2;
      $('glanceWin').textContent = secs < 90 ? 'live'
        : 'last ' + Math.round(secs / 60) + 'm';
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
        + `<div class="sevword ${a.sev}">${esc(a.word)}</div></div>${ack}</div>`;
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
      else if (f === 'resolved') firing = [];
      const showEarlier = (f === 'all' || f === 'resolved') && vm.earlier.length;
      $('alertsFiring').innerHTML = firing.map((r) => this.row(r)).join('');
      $('alertsEarlierWrap').hidden = !showEarlier;
      if (showEarlier) $('alertsEarlier').innerHTML = vm.earlier.map((r) => this.row(r)).join('');
      const empty = firing.length === 0 && !showEarlier;
      $('alertsEmpty').hidden = !empty;
      if (empty) {
        const label = { critical: 'No critical alerts', warning: 'No warning alerts',
          resolved: 'Nothing earlier today' }[f] || 'All clear';
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
    async refresh() {
      const [today, month, hourly, week] = await Promise.all([
        jfetch('/api/energy/summary?days=1').catch(() => ({})),
        jfetch('/api/energy/summary').catch(() => ({})),
        jfetch('/api/energy/hourly?hours=24').catch(() => ({})),
        jfetch('/api/energy/hourly?days=7').catch(() => ({})),
      ]);
      this.render(today, month, hourly, week);
    },
    render(today, month, hourly, week) {
      const tT = today.totals || {}, mT = month.totals || {};
      $('energyHeroN').innerHTML = `${esc(EN.fmtWatts(tT.avg_watts).replace(' W', ''))} <small>W</small>`;
      // 24h watts strip: each hourly bucket's Wh over ~1h ≈ average watts
      const watts = (hourly.rows || []).map((r) => r.energy_wh);
      const sp = CS.path(watts, 340, 118);
      $('energySparkLine').setAttribute('d', sp.line);
      $('energySparkFill').setAttribute('d', sp.fill);

      const price = today.price_kwh;
      const elapsed = (month.window || {}).elapsed_s;
      const proj = (mT.cost_usd != null && elapsed > 0)
        ? mT.cost_usd / elapsed * 30 * 86400 : null;
      $('energyTiles').innerHTML = [
        { v: EN.fmtUsd(tT.cost_usd), unit: '', k: 'Today',
          sub: tT.kwh != null ? EN.fmtKwh(tT.kwh) + (price != null ? ' · $' + price + '/kWh' : '') : 'no telemetry' },
        { v: proj != null ? '$' + Math.round(proj) : '—', unit: proj != null ? ' proj' : '',
          k: 'This month', sub: '30-day at current mix' },
      ].map(tileEl).join('');

      $('energyHosts').innerHTML = this.hostRows(today);
      this.dayBars(week.rows || [], price);
    },
    hostRows(today) {
      const rows = (today.hosts || []).map((h) => {
        const src = EN.sourceLabel(h.power_source);
        return providerRow({
          status: h.has_power ? 'ok' : 'idle',
          name: h.hostname || (h.agent_id || '').slice(0, 10) || '?',
          detail: src ? src + (h.power_source === 'psu' ? ' · liquidctl' : ' · powermetrics') : 'no power telemetry',
          rN: EN.fmtWatts(h.avg_watts), rUnit: 'avg',
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
    dayBars(rows, price) {
      // bucket hourly Wh by LOCAL calendar day, cost = kWh × price
      const byDay = new Map();
      (rows || []).forEach((r) => {
        const d = new Date(r.hour_ts * 1000);
        const key = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
        if (!byDay.has(key)) byDay.set(key, { wh: 0, d });
        byDay.get(key).wh += (r.energy_wh || 0);
      });
      const days = [...byDay.values()].slice(-7)
        .map(({ wh, d }) => ({ cost: (wh / 1000) * (price || 0),
          wd: d.toLocaleDateString('en', { weekday: 'short' }).slice(0, 2) }));
      const svg = $('energyDayBars');
      if (!days.length) { svg.setAttribute('aria-label', 'No daily cost data'); svg.innerHTML = ''; return; }
      svg.setAttribute('aria-label', 'Cost per day, last ' + days.length + ' days; '
        + days.map((d, i) => (i === days.length - 1 ? 'today ' : '') + EN.fmtUsd(d.cost)).join(', '));
      const max = Math.max(...days.map((d) => d.cost), 0.01);
      const n = days.length, gap = 14, w = (312 - gap) / n - gap, x0 = gap;
      let out = '';
      days.forEach((d, i) => {
        const h = Math.max(4, Math.round((d.cost / max) * 52));
        const x = x0 + i * ((312 - gap) / n), y = 76 - h;
        const today = i === n - 1;
        out += `<rect class="${today ? 'today' : ''}" x="${x.toFixed(0)}" y="${y}" `
          + `width="${w.toFixed(0)}" height="${h}" rx="4"/>`;
        out += `<text x="${(x + w / 2).toFixed(0)}" y="89" text-anchor="middle"${today ? ' class="hot"' : ''}>${esc(today ? 'today' : d.wd)}</text>`;
        if (today) out += `<text x="${(x + w / 2).toFixed(0)}" y="${y - 4}" text-anchor="middle" class="hot">${esc(EN.fmtUsd(d.cost))}</text>`;
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
      const vm = CV.admin({
        version, health: health || {}, agents: (agents || {}).agents || [],
        backup: backup || {}, auth: auth || {},
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
  const sheet = {
    open(title, bodyHtml, onclick) {
      $('sheetTitle').textContent = title;
      $('sheetBody').innerHTML = bodyHtml;
      $('sheetBody').onclick = onclick || null;
      $('sheet').hidden = false;
      $('sheetCancel').focus();
    },
    close() { $('sheet').hidden = true; $('sheetBody').onclick = null; },
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
      const [ls, health, ap, ag] = await Promise.all([
        jfetch('/api/llama-state').catch(() => ({})),
        jfetch('/api/admin/system-health').catch(() => null),
        jfetch('/api/autopilot').catch(() => null),
        jfetch('/api/agents').catch(() => null),
      ]);
      setLive(ls.agent_online, ls.agent_age_s);
      // Full autopilot state kept for the PUT: the API validates the whole
      // body, so the toggle must send entries+hosts back, never {enabled} alone.
      this.ap = ap;
      const version = (document.querySelector('meta[name="mgr-version"]') || {}).content || '—';
      this.vm = CV.actions({ llama: ls, health, autopilot: ap, agents: ag, version });
      this.render(this.vm);
    },
    msg(t, bad) {
      const m = $('actionsMsg');
      m.textContent = t || '';
      m.style.color = bad ? 'var(--crit)' : 'var(--fg-muted)';
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
    render(vm) {
      $('actionsGatedNote').hidden = !vm.gated;
      $('actionsServices').innerHTML = vm.services.map((s) =>
        `<div class="arow"><span class="pstat ${s.status}"></span>`
        + `<div class="atxt"><div class="an">${esc(s.name)}</div>`
        + `<div class="ad">${esc(s.detail)}</div></div>`
        + `<button class="btn" data-svc="${esc(s.key)}"${s.canRestart ? '' : ' disabled'}>Restart</button></div>`).join('');
      const m = vm.model;
      $('actionsModel').innerHTML =
        `<div class="arow"><div class="atxt"><div class="an">${esc(m.name || 'No model')}</div>`
        + `<div class="ad">${esc(m.detail)}</div></div>`
        + `<button class="btn primary" data-swap>Swap…</button></div>`;
      $('actionsAutopilot').innerHTML = vm.autopilot
        ? `<div class="arow"><div class="atxt"><div class="an">Model autopilot</div>`
          + `<div class="ad">${esc((vm.autopilot.on ? 'active' : 'off') + ' · ' + vm.autopilot.detail)}</div></div>`
          + `<button class="switch" role="switch" aria-checked="${vm.autopilot.on}" `
          + 'aria-label="Autopilot" data-ap><i></i></button></div>'
        : '<div class="arow"><div class="atxt"><div class="an">Model autopilot</div>'
          + '<div class="ad">needs admin</div></div></div>';
      $('actionsAgents').innerHTML = vm.pending.map((p) =>
        `<div class="card"><div class="an" style="font-size:13px;font-weight:600">${esc(p.name)} wants to join</div>`
        + `<div class="ad" style="font-family:var(--mono);font-size:10px;color:var(--fg-muted);margin-top:2px">${esc(p.detail)}</div>`
        + `<div class="approve"><button class="btn primary" data-approve="${esc(p.id)}">Approve</button>`
        + `<button class="btn danger" data-deny="${esc(p.id)}">Deny</button></div></div>`).join('')
        || (vm.gated ? '' : '<div class="provwrap"><div class="arow"><div class="atxt">'
          + '<div class="an">No pending agents</div></div></div></div>');
    },
    confirmRestart(key) {
      const s = ((this.vm || {}).services || []).find((x) => x.key === key) || { name: key };
      const req = key === 'llama'
        ? () => jfetch('/api/llm/server/restart', { method: 'POST' })
        : () => jfetch(`/api/admin/service/${encodeURIComponent(key)}/restart`, { method: 'POST' });
      sheet.confirm('Restart ' + s.name,
        key === 'llama' ? 'In-flight inference requests will be dropped while the unit restarts.'
          : key === 'manager' ? 'The dashboard and this app will briefly disconnect.'
            : 'Alert evaluation pauses while the engine restarts.',
        'Restart', true, () => this.act('restart ' + s.name, req));
    },
    confirmAutopilot() {
      const cur = ((this.ap || {}).state) || { enabled: false, entries: [], hosts: {} };
      const next = !cur.enabled;
      sheet.confirm((next ? 'Enable' : 'Disable') + ' autopilot',
        next ? 'The reconciler will start placing declared models automatically.'
          : 'Model placement stops; loaded models stay as they are.',
        next ? 'Enable' : 'Disable', !next,
        () => this.act('autopilot', () => jfetch('/api/autopilot', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.assign({}, cur, { enabled: next })),
        })));
    },
    async openSwap() {
      sheet.open('Swap model', '<div class="sd">loading model list…</div>');
      let list = [];
      try { list = (await jfetch('/api/llm/models')).data || []; }
      catch (e) {
        $('sheetBody').innerHTML = `<div class="sd">model list failed: ${esc(e && e.message || e)}</div>`;
        return;
      }
      const vm = this.vm || {};
      const cur = (vm.model || {}).name;
      const rows = list.map((mm) => {
        const st = ((mm.status || {}).value || '').toLowerCase();
        const isCur = mm.id === cur || st === 'loaded' || st === 'loading';
        return `<button class="arow" data-model="${esc(mm.id)}"${isCur ? ' disabled' : ''}>`
          + `<div class="atxt"><div class="an">${esc(mm.id)}</div></div>`
          + `<div class="pr">${esc(isCur ? (st || 'loaded') : st)}</div></button>`;
      }).join('') || '<div class="sd">no models configured</div>';
      const unload = (vm.model || {}).resident
        ? `<button class="btn danger" data-unload>Unload ${esc(cur)}</button>` : '';
      const pin = (vm.primaryLlamaId && (vm.model || {}).resident)
        ? `<button class="btn" data-pin>${(vm.model || {}).pinned ? 'Unpin' : 'Pin'} ${esc(cur)}</button>` : '';
      $('sheetBody').innerHTML = `<div class="sheetlist">${rows}</div>${unload}${pin}`;
      $('sheetBody').onclick = (e) => {
        const b = e.target.closest('[data-model]');
        if (b && !b.disabled) {
          const id = b.dataset.model;
          sheet.confirm('Load ' + id,
            'The current model is swapped out; the first request may be slow while it loads.',
            'Load', false, () => this.act('load', () => jfetch('/api/llm/load', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ model: id }) })));
        } else if (e.target.closest('[data-unload]')) {
          sheet.confirm('Unload ' + cur,
            'Frees VRAM; requests for this model will fail until it is reloaded.',
            'Unload', true, () => this.act('unload', () => jfetch('/api/llm/unload', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ model: cur }) })));
        } else if (e.target.closest('[data-pin]')) {
          const pinned = (vm.model || {}).pinned;
          sheet.confirm((pinned ? 'Unpin ' : 'Pin ') + cur,
            pinned ? 'Gateway requests for this model go back to pool routing.'
              : 'Gateway requests for this model always route to the primary llama host.',
            pinned ? 'Unpin' : 'Pin', false,
            () => this.act(pinned ? 'unpin' : 'pin', () => jfetch('/api/admin/llama-pins', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ model_id: cur, agent_id: pinned ? '' : vm.primaryLlamaId }) })));
        }
      };
    },
    start() {
      $('scr-actions').onclick = (e) => {
        const svc = e.target.closest('[data-svc]');
        if (svc && !svc.disabled) return this.confirmRestart(svc.dataset.svc);
        if (e.target.closest('[data-swap]')) return this.openSwap();
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
    glance: { title: 'LLM Systems', ctrl: glance, interval: 2000 },
    alerts: { title: 'Alerts', ctrl: alerts, interval: 15000 },
    energy: { title: 'Energy', ctrl: energy, interval: 30000 },
    actions: { title: 'Actions', ctrl: actions, interval: 10000 },
    admin: { title: 'Admin', ctrl: admin, interval: 10000 },
  };
  let timer = null;

  function show(tab) {
    const cfg = SCREENS[tab];
    if (!cfg) return;
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
      if (cfg.interval) timer = setInterval(() => {
        if (document.visibilityState === 'visible') cfg.ctrl.refresh();
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
    $('sheet').addEventListener('click', (e) => {
      if (e.target.closest('[data-sheet-close]')) sheet.close();
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
