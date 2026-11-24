// Overall page on the panel kit (#1432): Fleet, Throughput, LLM servers, Power and energy, Watch list and Pinned rows.
// Panels fill from the pure transforms in js/lib/overall-view.js (window.OV); the pinned grid is foundation.js's.
(function () {
  const TP = 'ov-tp';
  const SLOW_MS = 60000;
  const SLOT = { 'llama.cpp': 1, 'LM Studio': 2, 'vLLM': 3, 'Prompt (all)': 4 };
  const FILTER_LABEL = { all: 'All providers', llama: 'llama.cpp', lms: 'LM Studio', vllm: 'vLLM' };
  const SRV_NONE = { llama: 'No llama.cpp hosts', lms: 'No LM Studio hosts', vllm: 'No vLLM hosts' };
  const TONES = ['ok', 'sl', 'warn', 'bad', 'off'];
  const PROV_KEY = 'mgr.ovProv', RANGE_KEY = 'mgr.ovRange';
  const FC_TOP = 6, ALERTS_TOP = 5, GAP_RESEED = 3;
  const HOURLY_SUM = ['energy_wh', 'active_energy_wh', 'tokens_gen', 'tokens_prompt', 'observed_s', 'active_s'];

  // Provider filter and time range of the page.
  window.ovState = window.ovState || { prov: 'all', range: 24 };

  const S = {
    aggs: { llama: null, lms: null, vllm: null }, llamaState: null, lms: null, llmModels: null,
    summary1: null, summary7: null, hourly168: null, hourly48: null, hourlyProv: null, hourlyBy: {}, hourlyWait: null,
    tower: null, insights: null, slowEnded: false, modelsAt: 0, modelsRun: null, last: {},
    rows: null, histTried: false, histFailed: false, tp: null, tpKey: null, tpPainted: false, live: null, updated: 0, tpHistLast: null,
    slowAt: 0, slowRun: null, histGen: 0, dressed: false, laidOut: false, srvTried: false,
  };

  function st() {
    const o = window.ovState || {};
    return { prov: Object.prototype.hasOwnProperty.call(FILTER_LABEL, o.prov) ? o.prov : 'all',
             range: [6, 12, 24].includes(+o.range) ? +o.range : 24 };
  }
  function visible() {
    return (typeof _activeTab === 'undefined' || _activeTab === 'overall') && !!document.getElementById('overallTab');
  }
  function ids() { return OV.hostIds(window._agentsByProvider || {}, st().prov); }
  // A history fetch has ended: one succeeded, or the last one failed. After a failure a live tick may draw
  // the one-point chart (live data exists); a later successful load clears the failure and draws the full chart.
  function histEnded() { return S.histTried || S.histFailed; }
  // The hourly energy of the chosen filter is still being fetched.
  function hourlyLoading() { return S.hourlyWait != null && S.hourlyWait === st().prov; }
  async function getJson(url) {
    try {
      const r = await fetch(url);
      return r && r.ok ? await r.json() : null;
    } catch (_) { return null; }
  }

  // Panel writes that skip a body, table or note equal to the last one written, so focus and clicks survive ticks.
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
  // Header notes carry their full text as a tooltip, since Overall truncates them to keep the header on one line.
  function noteEl(id) {
    const el = document.getElementById(id), panel = el && el.closest('.panel');
    return panel && panel.querySelector('.p-hd .p-note');
  }
  function note(id, text) {
    const n = noteEl(id);
    if (n && n.textContent === text) return;
    kitNote(id, text);
    const m = noteEl(id);
    if (m) m.title = text;
  }
  const esc = (v) => kitEsc(v);
  const empty = (text) => '<div class="ov-empty">' + esc(text) + '</div>';
  const sub = (text) => (text ? '<div class="k-sub">' + esc(text) + '</div>' : '');
  const lab = (text) => '<div class="ov-lab">' + esc(text) + '</div>';
  const watts = (v) => (+v || 0).toFixed(1) + ' W';
  const wh = (v) => { v = +v || 0; return (Math.abs(v) < 10 ? v.toFixed(1) : String(Math.round(v))) + ' Wh'; };
  const tokens = (v) => OV.fmtShort(+v || 0);
  const more = (n) => (n > 0 ? '<div class="ov-more">' + n + ' more</div>' : '');
  function pill(label, tone, title) {
    return '<span class="ov-pill' + (TONES.includes(tone) ? ' ' + tone : '') + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>'
      + esc(label) + '</span>';
  }
  function kvRows(rows, badLabel) {
    return rows.map(r => '<dt>' + esc(r[0]) + '</dt><dd' + (badLabel && r[0] === badLabel ? ' class="ov-bad"' : '')
      + ' title="' + esc(r[1]) + '">' + esc(r[1]) + '</dd>').join('');
  }
  // Epoch ms → "5 min ago", "3 h ago" or "2 d ago"; '' when unreadable.
  function ago(ms, now) {
    if (typeof ms !== 'number' || !isFinite(ms)) return '';
    const s = Math.max(0, (now - ms) / 1000);
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + ' min ago'
      : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
  }
  // Epoch ms → "20 min" or "3 h" from now; '' when unreadable or past.
  function until(ms, now) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms <= now) return '';
    const s = (ms - now) / 1000;
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + ' min' : Math.round(s / 3600) + ' h';
  }
  function ageText(s) {
    if (typeof s !== 'number' || !isFinite(s)) return '';
    return typeof LMPeaks !== 'undefined' ? LMPeaks.agoText(s * 1000) : Math.round(s) + 's ago';
  }

  // ---- Throughput ----
  function tpChart() {
    const b = document.getElementById('ov-b-tp');
    return b ? b.querySelector('.ts[data-kit-ts]') : null;
  }
  // Time, then each series' peak and average t/s.
  function tpTable(o) {
    const series = o.series || [], r2 = (v) => Math.round((+v || 0) * 100) / 100;
    return { cols: ['Time'].concat(...series.map(s => [s.name + ' peak t/s', s.name + ' avg t/s'])),
             rows: (o.labels || []).map((l, i) => [l].concat(...series.map(s => [r2(s.values[i]), r2((s.avg || [])[i])]))) };
  }
  // Legend chips: each provider's tokens generated in the last 24 h, the prompt chip its prompt tokens.
  function tpLegends() { return OV.tpLegend(S.summary1, window._agentsByProvider || {}, st().prov); }
  // Chart series: Prompt in its own band on top, the providers stacked in the Generation band; values are peaks, avgs means.
  function tpSeries(names, values, avgs) {
    const legend = tpLegends();
    return names.map((name, k) => {
      const prompt = /^Prompt \(/.test(name);
      return Object.assign({ name, values: values[k], avg: avgs[k], slot: SLOT[name] || (prompt ? 4 : undefined), legend: legend[k] },
        prompt ? {} : { band: 'Generation' });
    });
  }
  // Draws the chart; a zoom z is kept when both its labels are still drawn.
  function drawTp(labels, series, keep, z) {
    const opts = { id: TP, labels, series, keep, zoom: true, w: kitWidth('ov-b-tp'), height: 260,
      bars: 'peak', split: true, idle: 'Idle · no requests in this range' };
    const a = z ? labels.indexOf(z.from) : -1;
    if (a >= 0 && labels.indexOf(z.to) > a) opts.z = { from: z.from, to: z.to };
    S.last['ov-b-tp'] = null;
    kitFill('ov-b-tp', tsHtml(opts));
    setRows('ov-b-tp', tpTable(opts));
  }
  // Puts the latest token counts in the drawn chart's legend chips.
  function tpLegendSync() {
    const ts = tpChart(), d = ts && kitTsData(ts);
    if (!d || !d.series) return;
    const legend = tpLegends();
    if (d.series.every((x, k) => x.legend === legend[k])) return;
    d.series.forEach((x, k) => { x.legend = legend[k]; });
    kitRedrawTs(ts, d, d.w);
  }
  // Draws Throughput from history; a new filter or range restarts the live bucket and drops the zoom, a reseed keeps it.
  // With no history rows it shows the live bucket alone once a history fetch has ended, else a sentence.
  function paintTp() {
    const o = st(), key = o.prov + '|' + o.range, same = S.tpKey === key;
    if (!same) { S.tpKey = key; S.live = null; }
    const was = same && tpChart() ? kitTsData(tpChart()) : null;
    S.tpPainted = true;
    S.tpHistLast = null;
    const tp = OV.throughput(S.rows || [], o.range, o.prov, Date.now());
    S.tp = tp;
    const names = tp.series.map(x => x.name);
    if (!(S.rows && S.rows.length)) {
      if (S.live && histEnded()) {
        const n = Math.max(1, +S.live.n || 1), sums = S.live.sums || S.live.values;
        drawTp([OV.rangeLabel(S.live.key, o.range)], tpSeries(names, S.live.values.map(v => [v]), sums.map(v => [(+v || 0) / n])), tp.keep);
      } else {
        fill('ov-b-tp', empty(S.histTried ? 'Nothing reported yet' : 'Loading…'));
        setRows('ov-b-tp', { cols: ['Time'], rows: [] });
      }
      return;
    }
    const series = tpSeries(names, tp.series.map(x => x.values.slice()), tp.series.map(x => x.avg.slice()));
    const last = tp.labels.length - 1;
    S.tpHistLast = last >= 0 ? { label: tp.labels[last], avgs: tp.series.map(x => +x.avg[last] || 0) } : null;
    if (S.live && last >= 0 && OV.rangeLabel(S.live.key, o.range) === tp.labels[last]) {
      const n = Math.max(1, +S.live.n || 1), sums = S.live.sums || S.live.values;
      series.forEach((x, k) => {
        x.values[last] = Math.max(+x.values[last] || 0, +S.live.values[k] || 0);
        x.avg[last] = Math.max(+x.avg[last] || 0, (+sums[k] || 0) / n);
      });
    }
    drawTp(tp.labels, series, tp.keep, was && was.z);
  }
  // Folds a tick into the newest bucket (same label keeps its peak, else appends); a gap over GAP_RESEED buckets reseeds.
  // Before Throughput is painted or a history fetch has ended only the bucket is kept; with no chart the tick draws it.
  function liveTick() {
    const o = st(), r = OV.liveBucket(S.live, Date.now(), o.range, S.aggs, o.prov);
    if (r.gap > GAP_RESEED && S.tpPainted) { S.live = null; ovLoad().catch(() => {}); return; }
    S.live = r.state;
    const body = document.getElementById('ov-b-tp');
    if (!body || !S.tpPainted) return;
    const pts = (r.append || []).concat([r.update]);
    if (!tpChart()) {
      if (!histEnded()) return;
      const tp = OV.throughput([], o.range, o.prov, Date.now()), names = tp.series.map(x => x.name);
      drawTp(pts.map(p => p.label), tpSeries(names, names.map((n, k) => pts.map(p => p.values[k])), names.map((n, k) => pts.map(p => p.avgs[k]))), tp.keep);
      return;
    }
    let grew = false;
    pts.forEach(p => {
      const ts = tpChart(), d = ts && kitTsData(ts);
      if (!d || !d.series) return;
      const labels = d.labels || [], i = labels.length - 1;
      if (i >= 0 && labels[i] === p.label) {
        d.series.forEach((x, k) => {
          x.values[i] = Math.max(+x.values[i] || 0, +p.values[k] || 0);
          const h = S.tpHistLast && S.tpHistLast.label === p.label ? +S.tpHistLast.avgs[k] || 0 : 0;
          if (Array.isArray(x.avg)) x.avg[i] = Math.max(h, +p.avgs[k] || 0);
        });
        kitRedrawTs(ts, d, d.w);
      } else if (kitTsAppend(body, TP, p.label, p.values, p.avgs)) grew = true;
    });
    const d = grew && kitTsData(tpChart());
    if (d) setRows('ov-b-tp', tpTable(d));
  }
  function tpNote() {
    const o = st();
    note('ov-b-tp', FILTER_LABEL[o.prov] + ' · last ' + o.range + ' h'
      + (S.updated ? ' · Updated ' + new Date(S.updated).toLocaleTimeString() : ''));
  }
  // Throughput as drawn now, live points included, for the Peak speed tile.
  function tpNow() {
    const ts = tpChart(), d = ts && kitTsData(ts);
    if (!d || !d.series) return S.tp;
    return { labels: d.labels, series: d.series, keep: d.keep, power: (S.tp && S.tp.power) || [], rangeH: S.tp && S.tp.rangeH };
  }

  // ---- Fleet tiles ----
  // Value and unit as one text, so kpiBody shows "5 / 5" or "11.5 W"; no data stays null (–).
  function kpiValue(k) {
    if (k.value == null) return null;
    const v = typeof k.value === 'number' ? kitFmt(k.value) : String(k.value);
    return k.unit ? v + ' ' + k.unit : v;
  }
  // A KPI body's HTML with a Loading… change slot appended to its value row when asked.
  function kpiHtmlWith(h, loading) {
    if (!loading) return h;
    const box = document.createElement('div');
    box.innerHTML = h;
    const row = box.querySelector('.k-row');
    if (row) row.insertAdjacentHTML('beforeend', '<span class="k-delta flat">Loading…</span>');
    return box.innerHTML;
  }
  // While the filter's hourly energy loads, the Tokens and Energy tiles show Loading… where their change would be.
  function paintKpis() {
    const wait = hourlyLoading();
    OV.fleetKpis({ aggs: S.aggs, summary1: S.summary1, hourly48: S.hourly48, tp: tpNow(), ids: ids() }).forEach(k => {
      const hourly = k.id === 'k-tok' || k.id === 'k-en';
      fill('ov-b-' + k.id, kpiHtmlWith(kpiBody({ value: kpiValue(k), sub: k.sub, delta: wait && hourly ? null : k.delta, spark: k.spark }),
        wait && hourly));
    });
  }

  // ---- LLM servers ----
  // Host name of the agent the llama dashboard shows: the picked one, else the default, else the only one.
  function llamaHost() {
    const list = (window._agentsByProvider || {}).llama || [], sel = (window._selectedAgents || {}).llama;
    const a = list.find(x => x && x.agent_id === sel) || list.find(x => x && x.is_default) || (list.length === 1 ? list[0] : null);
    return a && a.hostname ? String(a.hostname).replace(/\.local$/, '') : '';
  }
  // The llama.cpp request line from the llama dashboard's latest sample, named by its host, when it has one.
  function llamaRequests() {
    const ll = window._latestMetric && window._latestMetric.llama;
    if (!ll || typeof ll.requests_processing !== 'number' || typeof ll.requests_deferred !== 'number') return null;
    const kv = typeof ll.kv_cache_usage_ratio === 'number' ? ' · KV ' + Math.round(ll.kv_cache_usage_ratio * 100) + '% full' : '';
    const host = llamaHost();
    return [host ? 'Requests on ' + host : 'Requests', ll.requests_processing + ' running · ' + ll.requests_deferred + ' waiting' + kv];
  }
  function serverHtml(k, p) {
    const kv = p.kv.slice(), rows = p.table.rows.slice();
    const req = k === 'llama' ? llamaRequests() : null;
    if (req) {
      const at = kv.findIndex(r => r[0] === 'Context'), m = kv.findIndex(r => r[0] === 'Model');
      kv.splice((at >= 0 ? at : m) + 1, 0, req);
      rows.push(req);
    }
    let h = '<dl class="ov-kv"><dt>State</dt><dd>' + pill(p.state.label, p.state.tone) + (p.hot ? ' ' + pill('GPU hot', 'bad') : '')
      + (p.state.detail ? ' ' + esc(p.state.detail) : '') + '</dd>' + kvRows(kv, p.swapHigh ? 'Memory' : null) + '</dl>';
    if (p.pills.length) h += lab('GPU power policy') + '<div class="ov-pills">' + p.pills.map(x => pill(x.label, x.tone)).join('') + '</div>';
    if (p.power) {
      h += lab(p.power.title) + splitHtml(p.power.split, { fmt: watts }) + sub(p.power.sub);
    }
    if (p.models.length) {
      h += lab('Models on this server') + '<ul class="ov-models">' + p.models.map(x => '<li><span class="ov-m" title="'
        + esc(x.title) + '">' + esc(x.label) + (x.sub ? '<small>' + esc(x.sub) + '</small>' : '') + '</span>'
        + pill(x.status, x.tone) + '</li>').join('') + '</ul>';
    }
    if (p.hint) h += '<div class="ov-hint">' + esc(p.hint) + '</div>';
    return { html: h, table: { cols: p.table.cols, rows } };
  }
  // The llama.cpp preset list, fetched at most once a minute while the llama.cpp panel is shown.
  function refreshModels() {
    if (S.modelsRun || (S.modelsAt && Date.now() - S.modelsAt < SLOW_MS)) return;
    S.modelsAt = Date.now();
    S.modelsRun = getJson('/api/llm/models').then(v => {
      S.modelsRun = null;
      if (v == null) return;
      S.llmModels = v;
      if (visible()) paintServers();
    });
  }
  // Under a provider filter only that provider's panel shows, with its "No … hosts" sentence when it has none;
  // under All with no panel shown the row says Loading… until the first server poll, then that none has reported.
  function paintServers() {
    const panels = OV.serverPanels({ llamaState: S.llamaState, llmModels: S.llmModels, lms: S.lms, aggs: S.aggs,
      caps: (window._pollCfg || {}).agents, llamaHost: llamaHost(), now: Date.now() });
    const prov = st().prov;
    let shown = 0;
    ['llama', 'lms', 'vllm'].forEach(k => {
      const id = 'ov-b-srv-' + k, body = document.getElementById(id), p = panels[k];
      if (!body) return;
      const sec = body.closest('.panel');
      if (sec) sec.hidden = prov === 'all' ? !p : k !== prov;
      if (sec && !sec.hidden) shown++;
      if (!p) {
        fill(id, empty(SRV_NONE[k]));
        setRows(id, { cols: ['Field', 'Value'], rows: [] });
        return;
      }
      const out = serverHtml(k, p);
      fill(id, out.html);
      setRows(id, out.table);
      note(id, p.note);
      const row = body.closest('section.row');
      if (k === 'llama' && sec && !sec.hidden && !(row && (row.hidden || row.classList.contains('folded')))) refreshModels();
    });
    // While waiting, a fleet with server hosts keeps a typical panel height free (ov-wait).
    const none = document.getElementById('ov-srv-none');
    if (!none) return;
    const wait = !S.srvTried && shown === 0, text = wait ? 'Loading…' : 'No LLM servers reported yet.';
    const byProv = window._agentsByProvider || {};
    none.hidden = shown > 0;
    none.classList.toggle('ov-wait', wait && ['llama', 'lms', 'vllm'].some(k => (byProv[k] || []).length > 0));
    if (none.textContent !== text) none.textContent = text;
  }

  // ---- Power and energy ----
  function paintEnergy7() {
    if (hourlyLoading() || slowLoading(S.hourly168)) {
      fill('ov-b-energy7', empty('Loading…'));
      setRows('ov-b-energy7', { cols: ['From', 'Idle Wh', 'Generating Wh'], rows: [] });
      return;
    }
    // Three-hour steps; idle draws faint under generating with no gap between them.
    const e = OV.energyPerHour(S.hourly168, 3, { wrap: true });
    const series = [{ name: 'Idle', values: e.series[0].values, slot: 5, dim: true }, { name: 'Generating', values: e.series[1].values, slot: 2 }];
    fill('ov-b-energy7', tsHtml({ id: 'ov-en7', labels: e.labels, series, mode: 'stack', joined: true, w: kitWidth('ov-b-energy7'), height: 190 }));
    setRows('ov-b-energy7', { cols: ['From', 'Idle Wh', 'Generating Wh'],
      rows: e.labels.map((l, i) => [l, series[0].values[i], series[1].values[i]]) });
  }
  // A slow panel whose payload has not landed yet while no slow refresh has ended shows Loading….
  function slowLoading(payload) { return payload == null && !S.slowEnded; }
  function loadingPanel(id, cols) {
    fill(id, empty('Loading…'));
    setRows(id, { cols, rows: [] });
  }
  function paintEnergyHosts() {
    if (slowLoading(S.summary7)) { loadingPanel('ov-b-energy-hosts', ['Host', 'Wh']); return; }
    const e = OV.energyByHost(S.summary7, ids());
    fill('ov-b-energy-hosts', rankHtml(e.rows, { unit: 'Wh', dim: 'Host', fmt: wh }) + sub(e.sub));
    setRows('ov-b-energy-hosts', { cols: ['Host', 'Wh'], rows: e.rows.map(r => [r.label, r.v]) });
  }
  // Today's split body (generating vs idle, then by model) of a days=1 summary for the host ids (null: all).
  function ovSplitBody(summary1, scope) {
    const e = OV.energySplit(summary1, scope);
    let html = lab('Generating vs idle') + splitHtml(e.split, { unit: 'Wh', fmt: wh });
    if (e.models.length) html += lab('By model') + splitHtml(e.models, { unit: 'Wh', fmt: wh });
    return { html, cols: ['Where', 'Wh'], rows: e.split.concat(e.models).map(r => [r.label, r.v]) };
  }
  function paintEnergySplit() {
    if (slowLoading(S.summary1)) { loadingPanel('ov-b-energy-split', ['Where', 'Wh']); return; }
    const b = ovSplitBody(S.summary1, ids());
    fill('ov-b-energy-split', b.html);
    setRows('ov-b-energy-split', { cols: b.cols, rows: b.rows });
  }
  // Power now body of the provider aggregates for the host ids (null: all); a GPU power spark shows for all hosts only.
  function ovPowerBody(aggs, byProvider, scope, sparkPower) {
    const p = OV.powerNow(aggs, byProvider || {}, scope);
    const rows = p.rows.map(r => ({ label: r.label + ' · ' + r.sub, v: r.v, val: r.val }));
    let html = rankHtml(rows, { dim: 'Host', fmt: watts });
    if (p.noSensor) html += sub(p.noSensor + (p.noSensor === 1 ? ' host has' : ' hosts have') + ' no power sensor.');
    if (!scope && sparkPower && sparkPower.length) {
      html += lab('GPU power, last ' + st().range + ' h') + '<div class="ov-spark">' + kpiBody({ value: null, spark: sparkPower }) + '</div>';
    }
    return { html, cols: ['Host', 'Watts', 'Source'], rows: p.rows.map(r => [r.label, r.v, r.sub]), note: p.rows.length ? watts(p.total) : '' };
  }
  function paintPowerNow() {
    const b = ovPowerBody(S.aggs, window._agentsByProvider || {}, ids(), S.tp && S.tp.power);
    fill('ov-b-power-now', b.html);
    setRows('ov-b-power-now', { cols: b.cols, rows: b.rows });
    note('ov-b-power-now', b.note);
  }
  function paintHostsTokens() {
    if (slowLoading(S.summary1)) { loadingPanel('ov-b-hosts-tokens', ['Host', 'Tokens']); return; }
    const list = OV.hostsByTokens(S.summary1, ids());
    fill('ov-b-hosts-tokens', rankHtml(list, { dim: 'Host', fmt: tokens }));
    setRows('ov-b-hosts-tokens', { cols: ['Host', 'Tokens'], rows: list.map(r => [r.label, r.v]) });
  }

  // ---- Watch list ----
  const goLink = (to, text) => '<button type="button" class="ov-link" data-ov-go="' + to + '">' + esc(text) + '</button>';
  // Open Events and metrics goes to that tab, Open Forecast to Dashboards › Forecast.
  function onGo(ev) {
    const b = ev.target && ev.target.closest ? ev.target.closest('[data-ov-go]') : null;
    if (!b || typeof switchTab !== 'function') return;
    if (b.getAttribute('data-ov-go') === 'events') { switchTab('events'); return; }
    if (typeof _subTabState !== 'undefined') _subTabState.dashboard = 'forecast';
    switchTab('dashboard');
    if (typeof switchSubTab === 'function') switchSubTab('dashboard', 'forecast');
  }
  // Open findings, most severe then newest, top FC_TOP and "N more"; Tower's digest line under them.
  function paintForecast() {
    const now = Date.now(), snap = typeof forecastSnapshot === 'function' ? forecastSnapshot() : null;
    const f = OV.forecastFeed(snap, ids());
    const digest = snap && snap.enabled !== false && typeof FC !== 'undefined' ? FC.shortDigest(snap.digest) : '';
    let h = f.open.length ? feedHtml(f.open.slice(0, FC_TOP), now) + more(f.open.length - FC_TOP) : empty('No open findings');
    if (digest) h += '<div class="ov-act">' + esc('Tower: ' + digest) + '</div>';
    if (f.cleared.length) h += lab('Recently cleared') + feedHtml(f.cleared, now);
    const next = until(f.nextAt, now);
    h += sub([f.sub, next ? 'next run in ' + next : ''].filter(Boolean).join(' · ')) + goLink('forecast', 'Open Forecast');
    fill('ov-b-forecast', h);
    setRows('ov-b-forecast', { cols: ['Status', 'Finding', 'Detail'],
      rows: f.open.map(r => ['Open', r.title, r.detail]).concat(digest ? [['Tower', digest, '']] : [])
        .concat(f.cleared.map(r => [r.kind, r.title, r.detail])) });
    const last = ago(f.lastAt, now);
    note('ov-b-forecast', f.note + (last ? ' · ' + last : ''));
  }
  // Tower body: the newest insight with its action and checks, then Tower's model and activity.
  function ovTowerBody(tower, insights) {
    const t = OV.towerFeed(tower, insights), i = t.insight;
    let html = i ? feedHtml([i], Date.now()) : empty('No insights');
    if (i && i.action) html += '<div class="ov-act">' + esc(i.action) + '</div>';
    if (i && i.checks.length) {
      html += '<div class="ov-pills">' + i.checks.map(c => pill((c.ok ? '✓ ' : '✗ ') + c.label, c.ok ? 'ok' : 'off', c.title)).join('') + '</div>';
    }
    if (t.kv.length) html += '<dl class="ov-kv">' + kvRows(t.kv) + '</dl>';
    return { html, cols: ['Field', 'Value'], rows: t.kv.concat(i ? [['Insight (' + i.kind + ')', i.title]] : []), note: t.note };
  }
  function paintTower() {
    if (slowLoading(S.tower) && slowLoading(S.insights)) { loadingPanel('ov-b-tower', ['Field', 'Value']); return; }
    const b = ovTowerBody(S.tower, S.insights);
    fill('ov-b-tower', b.html);
    setRows('ov-b-tower', { cols: b.cols, rows: b.rows });
    note('ov-b-tower', b.note);
  }
  // Active alerts newest first, top ALERTS_TOP and "N more"; the table keeps them all.
  function paintAlerts() {
    const list = window._activeAlerts;
    if (list === undefined) {
      fill('ov-b-alerts', empty('Alerts unavailable') + goLink('events', 'Open Events and metrics'));
      setRows('ov-b-alerts', { cols: ['Severity', 'Alert', 'Detail'], rows: [] });
      note('ov-b-alerts', '');
      return;
    }
    const f = OV.alertsFeed(list, ids()), c = f.summary.counts;
    fill('ov-b-alerts', (f.rows.length ? feedHtml(f.rows.slice(0, ALERTS_TOP), Date.now()) + more(f.rows.length - ALERTS_TOP)
      : empty('No active alerts')) + goLink('events', 'Open Events and metrics'));
    setRows('ov-b-alerts', { cols: ['Severity', 'Alert', 'Detail'], rows: f.rows.map(r => [r.kind, r.title, r.detail]) });
    note('ov-b-alerts', ['critical', 'warning', 'info'].filter(k => c[k]).map(k => c[k] + ' ' + k).join(' · '));
  }
  function paintHosts() {
    const list = OV.hostRows(S.aggs, window._agentsByProvider || {}, ids());
    const provs = r => r.provs.map(p => (OV.PROVIDER_LABEL[p.prov] || p.prov) + ': ' + p.detail).join(' · ');
    fill('ov-b-hosts', list.length ? '<ul class="ov-hosts">' + list.map(r => '<li' + (r.online ? '' : ' class="off"') + '>'
      + '<span class="ov-dot' + (r.online ? ' ok' : '') + '"></span><span class="ov-h">' + esc(r.hostname)
      + '<small>' + esc(provs(r)) + '</small></span><span class="ov-age">' + esc(r.online ? ageText(r.ageS) : 'offline')
      + '</span></li>').join('') + '</ul>' : empty('No hosts yet'));
    setRows('ov-b-hosts', { cols: ['Host', 'State', 'Providers', 'Last seen'],
      rows: list.map(r => [r.hostname, r.online ? 'online' : 'offline', provs(r), r.online ? ageText(r.ageS) : '']) });
    note('ov-b-hosts', list.length ? list.filter(r => r.online).length + ' of ' + list.length + ' online' : '');
  }

  function paintTick() {
    tpNote(); paintKpis(); paintServers(); paintPowerNow(); paintForecast(); paintAlerts(); paintHosts();
  }
  function paintSlowPanels() {
    paintEnergy7(); paintEnergyHosts(); paintEnergySplit(); paintHostsTokens(); paintTower(); tpLegendSync();
  }
  // Repaints what the slow payloads feed once they land, when Overall is still showing.
  function paintSlow() { if (visible()) { paintKpis(); paintSlowPanels(); } }

  // Repaints every panel from the cached payloads; Throughput is redrawn from history when asked ({tp:true}),
  // on the first paint or when the filter or range differs from the drawn one (live bucket and zoom start over).
  function ovRender(opts) {
    if (!visible() || typeof OV === 'undefined') return;
    const root = document.getElementById('overallTab');
    if (!S.dressed) {
      S.dressed = true;
      kitDress(root);
      if (typeof mcSoloTitles === 'function') mcSoloTitles(root);
      root.querySelectorAll('.panel > .p-hd > .p-note').forEach(n => { n.title = n.textContent; });
      root.addEventListener('click', onGo);
      if (typeof keepPlace === 'function') keepPlace(root, '.panel, #overallGrid > .card');
    }
    const o = st();
    if ((opts && opts.tp) || !S.tpPainted || S.tpKey !== o.prov + '|' + o.range) paintTp();
    paintTick();
    paintSlowPanels();
    if (!S.laidOut) { S.laidOut = true; kitLayout('overall', root, window.mcShellStore); }
  }

  // Hourly energy {hourly168, hourly48} for prov: the fleet payloads for All, else each host's 168 h payload
  // summed per hour, with hourly48 as its last 48 h. Both are null when a host's fetch failed.
  async function ovEnergyHourly(prov) {
    if (prov === 'all') {
      const got = await Promise.all([getJson('/api/energy/hourly?hours=168'), getJson('/api/energy/hourly?hours=48')]);
      return { hourly168: got[0], hourly48: got[1] };
    }
    const agents = ((window._agentsByProvider || {})[prov]) || [];
    const ids = [...new Set(agents.map(a => a && a.agent_id).filter(Boolean))];
    const got = await Promise.all(ids.map(id => getJson('/api/energy/hourly?hours=168&agent=' + encodeURIComponent(id))));
    if (got.some(g => g == null)) return { hourly168: null, hourly48: null };
    const by = {};
    let end = null;
    got.forEach(g => {
      if (typeof g.end_ts === 'number' && isFinite(g.end_ts) && (end == null || g.end_ts > end)) end = g.end_ts;
      (Array.isArray(g.rows) ? g.rows : []).forEach(r => {
        if (!r || typeof r.hour_ts !== 'number' || !isFinite(r.hour_ts)) return;
        const b = by[r.hour_ts] = by[r.hour_ts] || { hour_ts: r.hour_ts };
        HOURLY_SUM.forEach(k => { b[k] = (b[k] || 0) + (+r[k] || 0); });
      });
    });
    const rows = Object.values(by).sort((a, b) => a.hour_ts - b.hour_ts);
    const last = end != null ? end : (rows.length ? rows[rows.length - 1].hour_ts + 3600 : 0);
    return { hourly168: { end_ts: end, rows }, hourly48: { end_ts: end, rows: rows.filter(r => r.hour_ts >= last - 172800) } };
  }
  // Keeps prov's hourly payloads for this page visit when both arrived.
  function cacheHourly(prov, h) {
    if (h.hourly168 != null && h.hourly48 != null) S.hourlyBy[prov] = h;
  }
  // Puts prov's hourly payloads on the page when prov is still the filter; true when it is. A failed one (null)
  // keeps the last good payload of the same filter and clears another filter's; arrived ones end the Loading… state.
  function useHourly(prov, h) {
    if (prov !== st().prov) return false;
    const same = S.hourlyProv === prov;
    ['hourly168', 'hourly48'].forEach(k => { if (h[k] != null || !same) S[k] = h[k]; });
    S.hourlyProv = prov;
    if (h.hourly168 != null && S.hourlyWait === prov) S.hourlyWait = null;
    return true;
  }
  // Stores each payload that arrived; a failed one (null) keeps the last good payload. True when any arrived.
  function keep(pairs) {
    pairs.forEach(([k, v]) => { if (v != null) S[k] = v; });
    return pairs.some(p => p[1] != null);
  }
  // Energy and Tower payloads, refetched at most once a minute; null when not due. Each refresh drops
  // the per-filter hourly cache. The minute starts only when something arrived, so a refresh that got nothing retries.
  function refreshSlow() {
    if (S.slowRun) return S.slowRun;
    if (S.slowAt && Date.now() - S.slowAt < SLOW_MS) return null;
    const at = Date.now(), prov = st().prov;
    S.slowRun = Promise.all([getJson('/api/energy/summary?days=1'), getJson('/api/energy/summary?days=7'), ovEnergyHourly(prov),
      getJson('/api/tower/state'), getJson('/api/tower/insights')])
      .then(got => {
        const h = got[2];
        S.slowEnded = true;
        S.hourlyBy = {};
        cacheHourly(prov, h);
        useHourly(prov, h);
        if (S.hourlyWait === prov) S.hourlyWait = null;
        // A run started for another filter leaves the minute unstarted, so the current filter's energy comes next.
        if ((keep([['summary1', got[0]], ['summary7', got[1]], ['tower', got[3]], ['insights', got[4]]])
          || h.hourly168 != null || h.hourly48 != null) && prov === st().prov) S.slowAt = at;
      })
      .finally(() => { S.slowRun = null; });
    return S.slowRun;
  }

  // Per-tick entry from fetchMetrics: aggregates, llama-state and LM Studio metrics, then the live bucket and a repaint.
  async function fetchOverallMetrics() {
    if (!visible()) return;
    const slow = refreshSlow();
    const got = await Promise.all(['/api/fleet/llama/aggregate', '/api/fleet/lms/aggregate', '/api/fleet/vllm/aggregate',
      '/api/llama-state', '/api/lmstudio/metrics'].map(getJson));
    const fresh = got.slice(0, 3).some(v => v != null);
    S.srvTried = true;
    S.aggs = { llama: got[0] || S.aggs.llama, lms: got[1] || S.aggs.lms, vllm: got[2] || S.aggs.vllm };
    if (keep([['llamaState', got[3]], ['lms', got[4]]]) || fresh) S.updated = Date.now();
    if (!visible()) return;
    if (fresh) liveTick();
    if (!S.laidOut) ovRender(); else paintTick();
    if (slow) slow.then(paintSlow);
  }

  // Tab entry: paints from the caches, starts the slow payloads, seeds history and redraws Throughput as soon
  // as it lands. The live bucket starts over, so time spent hidden is not appended as empty buckets.
  async function ovLoad() {
    if (!visible()) return;
    S.live = null;
    ovRender();
    const slow = refreshSlow();
    if (slow) slow.then(paintSlow);
    const gen = ++S.histGen, n = st().range * 60;
    const got = await getJson('/api/history?fleet=all&since_minutes=' + n + '&max_rows=' + n);
    if (gen !== S.histGen || !visible()) return;
    if (Array.isArray(got)) { S.rows = got; S.histTried = true; S.histFailed = false; } else S.histFailed = true;
    ovRender({ tp: true });
  }

  // ---- Toolbar filter and range ----
  // Writes a toolbar value through the shell store when it differs from the stored one (the store saves the layout).
  function saveBar(k, v) {
    const s = window.mcShellStore;
    if (s && typeof s.setItem === 'function' && s.getItem(k) !== String(v)) s.setItem(k, String(v));
  }
  function paintBar() { if (typeof window.mcShellPaintOv === 'function') window.mcShellPaintOv(); }
  // Provider filter: saves it, narrows every panel and redraws Throughput (live bucket and zoom start over).
  // Hourly energy comes from the per-filter cache, else is fetched for the filter's hosts.
  function ovSetFilter(prov) {
    if (!Object.prototype.hasOwnProperty.call(FILTER_LABEL, prov)) return Promise.resolve();
    window.ovState = window.ovState || {};
    const changed = st().prov !== prov;
    window.ovState.prov = prov;
    saveBar(PROV_KEY, prov);
    paintBar();
    if (!changed) return Promise.resolve();
    const cached = S.hourlyBy[prov];
    useHourly(prov, cached || { hourly168: null, hourly48: null });
    S.hourlyWait = cached ? null : prov;
    ovRender({ tp: true });
    if (cached) return Promise.resolve();
    // Hidden: the next tab entry refetches the slow payloads for this filter.
    if (!visible()) { S.slowAt = 0; return Promise.resolve(); }
    return ovEnergyHourly(prov).then(h => {
      cacheHourly(prov, h);
      if (S.hourlyWait === prov) S.hourlyWait = null;
      if (useHourly(prov, h) && visible()) { paintKpis(); paintEnergy7(); }
    });
  }
  // Time range (6, 12 or 24 h): saves it, redraws Throughput for it and reseeds its history.
  function ovSetRange(h) {
    h = +h;
    if (![6, 12, 24].includes(h)) return Promise.resolve();
    window.ovState = window.ovState || {};
    const changed = st().range !== h;
    window.ovState.range = h;
    saveBar(RANGE_KEY, h);
    paintBar();
    // Host page panels pinned here redraw for the new range.
    if (changed && typeof window.hpRender === 'function') window.hpRender();
    return changed ? ovLoad() : Promise.resolve();
  }

  window.ovRender = ovRender;
  window.ovLoad = ovLoad;
  window.ovSetFilter = ovSetFilter;
  window.ovSetRange = ovSetRange;
  window.fetchOverallMetrics = fetchOverallMetrics;
  window.ovSplitBody = ovSplitBody;
  window.ovPowerBody = ovPowerBody;
  window.ovTowerBody = ovTowerBody;
})();

// Enable/disable llama.cpp server control buttons.
// When down: only Start is enabled; Stop/Restart/Status are dimmed.
function _setLlamaBtns(serverUp) {
  const ids = ['llamaBtnStop', 'llamaBtnRestart', 'llamaBtnStatus'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !serverUp;
  });
  const start = document.getElementById('llamaBtnStart');
  if (start) start.disabled = serverUp; // Start only available when down
}

// Enable/disable LMS server control buttons.
// When agent offline or server down: only Start enabled.
function _setLmsBtns(serverUp) {
  const ids = ['lmsBtnStop', 'lmsBtnRestart', 'lmsBtnStatus'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = !serverUp;
  });
  const start = document.getElementById('lmsBtnStart');
  if (start) start.disabled = serverUp;
}
