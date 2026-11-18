// Dashboards · All hosts on the panel kit (#1432): a Right now table and Compare over time charts for every host.
// Panels fill from the pure transforms in js/lib/hosts-view.js (window.HV); every host's history comes from one
// /api/history/hosts request and is cached per host for the visit.
(function () {
  const RANGE_KEY = 'mgr.ahRange';
  const POLL_MS = 60000, LIVE_MIN = 5, REUSE_MS = 10 * 60000, FULL_RETRY_MS = 5 * 60000, BAD_PCT = 85;
  const PROVIDER_FOR = { llamacpp: 'llama', lmstudio: 'lms', vllm: 'vllm' };
  const HIDDEN_NOTE = "Some hosts hidden · click a host's chip to show it";

  // Time range of the page.
  window.ahState = window.ahState || { range: 24 };

  const S = {
    hosts: null, hostsFailed: false, needFull: false, fullAt: 0, slots: new Map(), rows: new Map(), range: null, fetchedAt: 0, seeded: false,
    active: false, timer: null,
    gen: 0, run: null, again: undefined, seen: new Set(), autoOff: new Set(), chosen: new Set(),
    last: {}, dressed: false, laidOut: false,
  };

  function range() {
    const r = +(window.ahState || {}).range;
    return [6, 12, 24].includes(r) ? r : 24;
  }
  function visible() {
    return !!document.getElementById('dash-all')
      && (typeof _activeTab === 'undefined' || _activeTab === 'dashboard')
      && (typeof _subTabState === 'undefined' || !_subTabState || _subTabState.dashboard === 'all');
  }
  async function fetchJson(url) {
    const r = await fetch(url);
    if (!r || !r.ok) throw new Error('HTTP ' + (r && r.status));
    return r.json();
  }
  function histUrl(minutes, maxRows) {
    return '/api/history/hosts?since_minutes=' + minutes + (maxRows ? '&max_rows=' + maxRows : '');
  }
  // Rows asked per host for a range: three per chart bucket, so each bucket mean averages real samples.
  function rowsWanted(h) { return 3 * Math.round(h * 3600000 / OV.rangeBucketMs(h)); }
  // The next fetch in minutes, or null for the whole range: after a partial or failed read the whole range at most
  // every 5 minutes with LIVE_MIN windows between, else LIVE_MIN while the last good fetch is at most 4 minutes old.
  function nextMinutes() {
    const now = Date.now();
    if (S.needFull) return now - S.fullAt >= FULL_RETRY_MS ? null : LIVE_MIN;
    return S.fetchedAt && now - S.fetchedAt <= (LIVE_MIN - 1) * 60000 ? LIVE_MIN : null;
  }

  // Panel writes that skip a body, table or note equal to the last one written.
  function fill(id, html) {
    const el = document.getElementById(id), was = S.last[id];
    if (!el || (was && was.el === el && was.html === html)) return;
    S.last[id] = { el, html };
    kitFill(id, html);
  }
  function setRows(id, table) {
    const t = JSON.stringify(table), el = document.getElementById(id), was = S.last['t:' + id];
    if (!el || (was && was.el === el && was.t === t)) return;
    S.last['t:' + id] = { el, t };
    kitSetRows(id, table);
  }
  function note(id, text) {
    const el = document.getElementById(id), panel = el && el.closest('.panel'), n = panel && panel.querySelector('.p-hd .p-note');
    if (n) n.title = text;
    if (n ? n.textContent === text : !text) return;
    kitNote(id, text);
    const m = panel && panel.querySelector('.p-hd .p-note');
    if (m) m.title = text;
  }
  const esc = (v) => kitEsc(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const num = (v) => (typeof v === 'number' && isFinite(v) ? Math.round(v * 10) / 10 : '');

  // ---- Right now ----
  function pctCell(v) {
    if (typeof v !== 'number' || !isFinite(v)) return '<td class="ah-v"><span class="ah-t">—</span></td>';
    const w = Math.max(0, Math.min(100, v));
    return '<td class="ah-v' + (v >= BAD_PCT ? ' bad' : '') + '"><span class="ah-bar"><i style="width:' + w.toFixed(1)
      + '%"></i></span><span class="ah-t">' + esc(HV.fmtPct(v)) + '</span></td>';
  }
  // Host cell: dot and name on one line (cut with an ellipsis), then "offline" and the open-host button on a muted line.
  function hostCell(r) {
    const dot = '<span class="ov-dot' + (r.online ? ' ok' : '') + '"></span>';
    const name = '<span class="ah-name" title="' + esc(r.name) + '">' + esc(r.name) + '</span>';
    const notes = (r.online ? [] : ['<span class="ah-off">offline</span>'])
      .concat(r.page ? [] : ['<button type="button" class="ah-none" data-ah-host="' + esc(r.id) + '">No LLM server · open host</button>']);
    const line = r.page
      ? '<button type="button" class="ah-go" data-ah-go="' + esc(r.id) + '">' + dot + name + '</button>'
      : '<span class="ah-h">' + dot + name + '</span>';
    return '<td class="ah-host">' + line + (notes.length ? '<div class="ah-sub">' + notes.join(' ') + '</div>' : '') + '</td>';
  }
  function paintNow() {
    if (!S.hosts || !S.hosts.length) {
      fill('ah-b-now', empty(S.hosts ? 'No hosts reported yet.' : S.hostsFailed ? 'Could not load hosts.' : 'Loading…'));
      setRows('ah-b-now', { cols: ['Host', 'State', 'CPU %', 'Memory %', 'Disk / %'], rows: [] });
      return;
    }
    const rows = HV.nowRows(S.hosts, S.rows, Date.now());
    fill('ah-b-now', '<div class="ah-scroll"><table class="ah-now"><thead><tr><th>Host</th><th>CPU</th><th>Memory</th><th>Disk /</th></tr></thead><tbody>'
      + rows.map(r => '<tr data-ah-id="' + esc(r.id) + '"' + (r.page ? ' data-ah-page="' + esc(r.page) + '" class="go"' : '') + '>'
        + hostCell(r) + pctCell(r.cpu) + pctCell(r.ram) + pctCell(r.disk) + '</tr>').join('') + '</tbody></table></div>');
    setRows('ah-b-now', { cols: ['Host', 'State', 'CPU %', 'Memory %', 'Disk / %'],
      rows: rows.map(r => [r.name, r.online ? 'online' : 'offline', num(r.cpu), num(r.ram), num(r.disk)]) });
  }

  // ---- Compare over time ----
  const seriesKey = (m, name) => 'ah-' + m.key + '|' + name;
  // Panel body id of a metric: ah-b- and its card id without the ah- prefix.
  const bodyId = (m) => 'ah-b-' + m.card.replace(/^ah-/, '');
  // Each host a chart shows for the first time in a visit starts hidden when it is outside the busiest,
  // unless the operator picked it.
  function hideQuiet(m, c) {
    c.series.forEach(s => {
      const k = seriesKey(m, s.name);
      if (S.seen.has(k)) return;
      S.seen.add(k);
      if (S.chosen.has(k)) return;
      if (c.busiest.includes(s.id)) {
        if (S.autoOff.has(k)) { delete KIT_SERIES_OFF[k]; S.autoOff.delete(k); }
      } else { KIT_SERIES_OFF[k] = true; S.autoOff.add(k); }
    });
  }
  function noteFor(m, c) {
    return c.series.some(s => KIT_SERIES_OFF[seriesKey(m, s.name)]) ? HIDDEN_NOTE : '';
  }
  // A host's colour slot, given in first-seen order (1–5 cycling) and kept while the page is open.
  function slotOf(id) {
    if (!S.slots.has(id)) S.slots.set(id, (S.slots.size % 5) + 1);
    return S.slots.get(id);
  }
  function chartTable(labels, series) {
    return { cols: ['Time'].concat(series.map(s => s.name)),
             rows: labels.map((l, i) => [l].concat(series.map(s => num(s.values[i])))) };
  }
  const same = (a, b) => (+a || 0) === (+b || 0);
  // How many whole buckets the drawn chart is behind the new one when every drawn bucket is unchanged; 0 when not.
  function aheadBy(was, labels, series) {
    const n = labels.length, old = was.labels || [];
    if (old.length !== n || !was.series || was.series.length !== series.length
        || was.series.some((s, i) => s.name !== series[i].name)) return 0;
    for (let k = 1; k < n; k++) {
      if (old[k] !== labels[0]) continue;
      const ok = old.slice(k).every((l, i) => l === labels[i])
        && series.every((s, j) => was.series[j].values.slice(k).every((v, i) => same(v, s.values[i])));
      return ok ? k : 0;
    }
    return 0;
  }
  function drawChart(m, c) {
    const id = bodyId(m), cid = 'ah-' + m.key, body = document.getElementById(id);
    const ts = body && body.querySelector('.ts[data-kit-ts]'), was = ts ? kitTsData(ts) : null;
    const series = c.series.map(s => ({ name: s.name, values: s.values, slot: s.slot, legend: '' }));
    const k = was ? aheadBy(was, c.labels, series) : 0;
    if (k) {
      const n = c.labels.length;
      for (let i = n - k; i < n; i++) kitTsAppend(body, cid, c.labels[i], series.map(s => s.values[i]));
      S.last[id] = null;
    } else {
      const opts = { id: cid, labels: c.labels, series, zoom: true, keep: c.labels.length, w: kitWidth(id), height: 170, gaps: 'break' };
      if (m.ymax) opts.ymax = m.ymax;
      const z = was && was.z, a = z ? c.labels.indexOf(z.from) : -1;
      if (a >= 0 && c.labels.indexOf(z.to) > a) opts.z = { from: z.from, to: z.to };
      fill(id, tsHtml(opts));
    }
    setRows(id, chartTable(c.labels, series));
  }
  function paintCompare() {
    const h = range(), now = Date.now();
    HV.METRICS.forEach(m => {
      const id = bodyId(m);
      const c = S.hosts && S.seeded ? HV.compare(S.hosts, S.rows, m.key, h, now, slotOf) : null;
      if (!c || !c.series.length) {
        fill(id, empty(c || (S.hosts && !S.hosts.length) ? 'Nothing in this period.'
          : !S.hosts && S.hostsFailed ? 'Could not load hosts.' : 'Loading…'));
        setRows(id, { cols: ['Time'], rows: [] });
        note(id, '');
        return;
      }
      hideQuiet(m, c);
      drawChart(m, c);
      note(id, noteFor(m, c));
    });
  }
  // Redraws the compare notes after a legend chip changed which hosts show.
  function paintNotes() {
    if (!visible() || !S.hosts || !S.seeded) return;
    const h = range(), now = Date.now();
    HV.METRICS.forEach(m => {
      const c = HV.compare(S.hosts, S.rows, m.key, h, now, slotOf);
      if (c.series.length) note(bodyId(m), noteFor(m, c));
    });
  }

  // A host row with an LLM server opens that provider's page with the host picked, a host with none opens the host page;
  // a legend chip is remembered as a choice.
  function onClick(ev) {
    const t = ev.target && ev.target.closest ? ev.target : null;
    const chip = t && t.closest('button[data-kit-series]');
    if (chip) {
      S.chosen.add(chip.getAttribute('data-kit-series'));
      setTimeout(paintNotes, 0);
      return;
    }
    const open = t && t.closest('button[data-ah-host]');
    if (open) {
      if (typeof hpSetHost === 'function') hpSetHost('host', open.getAttribute('data-ah-host'));
      if (typeof switchSubTab === 'function') switchSubTab('dashboard', 'host');
      return;
    }
    const tr = t && t.closest('tr[data-ah-page]');
    if (!tr) return;
    const page = tr.getAttribute('data-ah-page'), provider = PROVIDER_FOR[page];
    if (typeof _selectAgent === 'function' && provider) _selectAgent(provider, tr.getAttribute('data-ah-id'));
    if (typeof switchSubTab === 'function') switchSubTab('dashboard', page);
  }

  // Paints every panel from the cached hosts and rows.
  function ahRender() {
    if (!visible() || typeof HV === 'undefined') return;
    const root = document.getElementById('dash-all');
    if (!S.dressed) {
      S.dressed = true;
      kitDress(root);
      root.querySelectorAll('.panel > .p-hd > .p-note').forEach(n => { n.title = n.textContent; });
      root.addEventListener('click', onClick);
    }
    if (!S.laidOut) { S.laidOut = true; kitLayout('allhosts', root, window.mcShellStore); }
    paintNow();
    paintCompare();
  }

  // ---- Fetching ----
  // Host names shared by several hosts get a short agent id suffix, so labels and chart chips stay apart.
  function uniqueNames(list) {
    const count = {};
    list.forEach(h => { count[h.name] = (count[h.name] || 0) + 1; });
    return list.map(h => (count[h.name] > 1 ? Object.assign({}, h, { name: h.name + ' (' + String(h.id).slice(0, 6) + ')' }) : h));
  }
  // The host list; a failed fetch keeps the last list (none before the first success) and marks the failure.
  async function loadHosts(gen) {
    try {
      const list = uniqueNames(HV.hostList(await fetchJson('/api/agents/hosts')));
      if (gen !== S.gen) return;
      S.hosts = list;
      S.hostsFailed = false;
      const ids = new Set(list.map(x => x.id));
      [...S.rows.keys()].forEach(id => { if (!ids.has(id)) S.rows.delete(id); });
    } catch (_) {
      if (gen === S.gen) S.hostsFailed = true;
    }
  }
  // Every host's rows since `minutes` in one request, merged into each host's cache by hostname; null minutes,
  // or a listed host with no cache yet, fetches the whole range in place of the cache.
  // A failed request, or an answer listing failed fields, keeps the cache (hosts with none take what arrived),
  // leaves fetchedAt and makes the next fetch the whole range. True when a whole-range answer arrived.
  async function loadHistory(minutes, gen) {
    const h = S.range, list = S.hosts || [], now = Date.now();
    if (!list.length) { S.seeded = true; return false; }
    const full = minutes == null || list.some(x => !S.rows.has(x.id));
    if (full) S.fullAt = now;
    let got;
    try {
      got = await fetchJson(full ? histUrl(h * 60, rowsWanted(h)) : histUrl(minutes));
    } catch (_) {
      if (gen === S.gen) { S.seeded = true; if (full) S.needFull = true; }
      return false;
    }
    if (gen !== S.gen) return false;
    const byHost = (got && got.hosts && typeof got.hosts === 'object') ? got.hosts : {};
    const partial = Array.isArray(got && got.failed) && got.failed.length > 0;
    const cut = now - h * 3600000;
    list.forEach(x => {
      if (partial && S.rows.has(x.id)) return;
      const fresh = Object.prototype.hasOwnProperty.call(byHost, x.hostname) ? byHost[x.hostname] : [];
      S.rows.set(x.id, HV.mergeRows(full ? [] : S.rows.get(x.id), fresh, cut));
    });
    S.seeded = true;
    // Only a clean whole-range read clears the retry; a partial read of any kind sets it.
    if (full || partial) S.needFull = partial;
    if (!partial) S.fetchedAt = now;
    return full;
  }
  // Fetches the hosts and then their history, then repaints; a call while one runs runs once more after it.
  function refresh(minutes) {
    if (S.run) {
      S.again = S.again === undefined ? minutes : (S.again == null || minutes == null ? null : Math.max(S.again, minutes));
      return S.run;
    }
    const gen = S.gen;
    S.run = (async () => {
      await loadHosts(gen);
      if (gen !== S.gen) return;
      const full = S.hosts ? await loadHistory(minutes, gen) : false;
      if (gen !== S.gen) return;
      ahRender();
      // A whole-range answer comes in coarse buckets, so the last 5 minutes follow at once for Right now.
      if (full) {
        await loadHistory(LIVE_MIN, gen);
        if (gen === S.gen) ahRender();
      }
    })().catch(() => {}).finally(() => {
      S.run = null;
      const m = S.again;
      S.again = undefined;
      if (m !== undefined && S.active) refresh(m);
    });
    return S.run;
  }
  // The minute poll: the last 5 minutes, or the whole range after a longer gap; idle in a hidden browser tab.
  function tick() {
    if (S.active && visible() && !document.hidden) refresh(nextMinutes());
  }
  // Catch-up for a return to the browser tab or the end of a Live pause, while the page is open.
  function ahRefresh() {
    return S.active && visible() ? refresh(nextMinutes()) : Promise.resolve();
  }
  function stopPoll() {
    if (S.timer != null) clearInterval(S.timer);
    S.timer = null;
  }

  // Page entry: paints from the cache, then fetches the hosts and their history and starts the minute poll.
  // Within 10 minutes of the last fetch the cached rows stay and only the gap is fetched.
  function ahEnter() {
    if (!visible() || typeof HV === 'undefined') return Promise.resolve();
    if (S.active) { ahRender(); return S.run || Promise.resolve(); }
    S.active = true;
    S.seen = new Set();
    const reuse = S.fetchedAt && Date.now() - S.fetchedAt < REUSE_MS && S.range === range();
    if (!reuse) { S.hosts = null; S.hostsFailed = false; S.needFull = false; S.fullAt = 0; S.rows = new Map(); S.fetchedAt = 0; S.seeded = false; S.range = range(); }
    ahRender();
    stopPoll();
    S.timer = window.LivePause ? window.LivePause.every(tick, POLL_MS) : setInterval(tick, POLL_MS);
    return refresh(reuse ? nextMinutes() : null);
  }
  // Page exit: stops the poll and drops answers still on the way.
  function ahLeave() {
    if (!S.active) return;
    S.active = false;
    S.gen++;
    S.again = undefined;
    stopPoll();
  }

  // Time range (6, 12 or 24 h): saves it and refetches every host's history for it.
  function ahSetRange(n) {
    n = +n;
    if (![6, 12, 24].includes(n)) return Promise.resolve();
    window.ahState = window.ahState || {};
    const changed = range() !== n;
    window.ahState.range = n;
    const s = window.mcShellStore;
    if (s && typeof s.setItem === 'function' && s.getItem(RANGE_KEY) !== String(n)) s.setItem(RANGE_KEY, String(n));
    if (typeof window.mcShellPaintAh === 'function') window.mcShellPaintAh();
    if (!changed) return Promise.resolve();
    S.range = n;
    S.seeded = false;
    S.fetchedAt = 0;
    S.seen = new Set();
    if (!S.active) { S.rows = new Map(); return Promise.resolve(); }
    S.gen++;
    ahRender();
    return refresh(null);
  }

  // Forgets the operator's chart chip picks, so the busiest-five default applies again, and repaints.
  function ahResetChoices() {
    S.chosen.clear();
    S.autoOff.clear();
    S.seen = new Set();
    Object.keys(KIT_SERIES_OFF).forEach(k => { if (k.indexOf('ah-') === 0) delete KIT_SERIES_OFF[k]; });
    S.last = {};
    ahRender();
  }

  window.ahEnter = ahEnter;
  window.ahRefresh = ahRefresh;
  window.ahResetChoices = ahResetChoices;
  window.ahLeave = ahLeave;
  window.ahSetRange = ahSetRange;
  window.ahRender = ahRender;
})();
