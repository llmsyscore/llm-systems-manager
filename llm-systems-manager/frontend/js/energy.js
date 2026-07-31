// ===========================================================================
// Energy & cost sub-tab (#470) — savings hero, tiles, host table, chart.
// Pure helpers live in js/lib/energy.js (window.EN).
// ===========================================================================
let _enChart = null;
let _enLoaded = false;

function _enEl(id) { return document.getElementById(id); }

function _enNote(msg, warn) {
  const n = _enEl('enNote');
  if (!n) return;
  n.textContent = msg || '';
  n.classList.toggle('en-warn', !!warn);
}

function _enParams() {
  const params = new URLSearchParams(EN.windowQuery(_enEl('enWindow')?.value));
  for (const [id, key] of [['enPrice', 'price_kwh'], ['enCloudIn', 'cloud_in'],
                           ['enCloudOut', 'cloud_out']]) {
    const raw = _enEl(id)?.value;
    if (raw !== undefined && raw !== '' && Number.isFinite(parseFloat(raw))) {
      params.set(key, raw);
    }
  }
  return params;
}

function enRefresh() {
  const params = _enParams();
  fetch('/api/energy/summary?' + params.toString())
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { _enNote(d.error || 'Failed to load energy summary.', true); return; }
      _enNote('');
      _enRenderSummary(d);
    })
    .catch(e => _enNote('Failed to load energy summary: ' + e, true));
  // Chart follows the same selected window as the summary above it.
  const winParams = new URLSearchParams(EN.windowQuery(_enEl('enWindow')?.value));
  fetch('/api/energy/hourly?' + winParams.toString())
    .then(r => r.json())
    .then(d => {
      if (!d.ok) return;
      const label = _enEl('enChartLabel');
      if (label) label.textContent = 'Hourly energy · ' + (d.label || '');
      _enDrawChart(EN.hourlySeries(d.rows));
    })
    .catch(() => {});
}

function _enRenderSummary(d) {
  // Prefill price inputs from config once so operator overrides stick.
  const cfg = d.config || {};
  const price = _enEl('enPrice');
  if (price && !price.value && cfg.price_kwh != null) price.value = cfg.price_kwh;
  const cin = _enEl('enCloudIn');
  if (cin && !cin.value && cfg.cloud_price_in_per_mtok != null) {
    cin.value = cfg.cloud_price_in_per_mtok;
  }
  const cout = _enEl('enCloudOut');
  if (cout && !cout.value && cfg.cloud_price_out_per_mtok != null) {
    cout.value = cfg.cloud_price_out_per_mtok;
  }

  const view = EN.savingsView(d);
  const hero = _enEl('enHero');
  if (hero) {
    hero.textContent = view.headline;
    hero.className = 'en-hero en-hero--' + view.cls;
  }
  const sub = _enEl('enHeroSub');
  if (sub) sub.textContent = view.sub;
  const windowLabel = _enEl('enWindowLabel');
  if (windowLabel) windowLabel.textContent = (d.window || {}).label || '';

  const tiles = _enEl('enTiles');
  if (tiles) {
    tiles.replaceChildren();
    EN.totalTiles(d.totals).forEach(t => {
      const cell = document.createElement('div');
      cell.className = 'en-tile';
      const num = document.createElement('div');
      num.className = 'en-tile-num';
      num.textContent = t.value;
      const label = document.createElement('div');
      label.className = 'en-tile-label';
      label.textContent = t.label;
      cell.appendChild(num);
      cell.appendChild(label);
      if (t.sub) {
        const s = document.createElement('div');
        s.className = 'en-tile-sub';
        s.textContent = t.sub;
        cell.appendChild(s);
      }
      tiles.appendChild(cell);
    });
  }

  _enRenderHosts(EN.hostRows(d.hosts));
  const foot = _enEl('enCoverage');
  if (foot) foot.textContent = EN.coverageNote(d);
}

function _enRenderHosts(rows) {
  const body = _enEl('enHostRows');
  if (!body) return;
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.className = 'en-empty';
    td.textContent = 'No per-host data in this window yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const host = document.createElement('td');
    host.textContent = r.hostname;
    if (r.source) {
      const badge = document.createElement('span');
      badge.className = 'en-badge';
      badge.textContent = r.source;
      host.appendChild(badge);
    }
    tr.appendChild(host);
    const kwh = document.createElement('td');
    kwh.textContent = r.kwh;
    if (r.split != null) {
      const bar = document.createElement('span');
      bar.className = 'en-split';
      bar.title = r.split + '% active';
      const fill = document.createElement('span');
      fill.className = 'en-split-fill';
      fill.style.width = r.split + '%';
      bar.appendChild(fill);
      kwh.appendChild(bar);
    }
    tr.appendChild(kwh);
    [r.activePct, r.tokens, r.cost, r.mtok, r.coverage].forEach(v => {
      const td = document.createElement('td');
      td.textContent = v;
      tr.appendChild(td);
    });
    const notes = document.createElement('td');
    notes.className = 'en-notes';
    notes.textContent = r.notes;
    tr.appendChild(notes);
    body.appendChild(tr);
  });
}

function _enDrawChart(series) {
  const canvas = _enEl('enChart');
  if (!canvas || typeof Chart === 'undefined') return;
  if (_enChart) _enChart.destroy();
  _enChart = new Chart(canvas.getContext('2d'), {
    data: {
      labels: series.labels,
      datasets: [
        { type: 'bar', label: 'active Wh', data: series.activeWh,
          backgroundColor: cssVar('--accent'), stack: 'wh', yAxisID: 'y' },
        { type: 'bar', label: 'idle Wh', data: series.idleWh,
          backgroundColor: cssVar('--fg-faint'), stack: 'wh', yAxisID: 'y' },
        { type: 'line', label: 'tokens generated', data: series.tokens,
          borderColor: cssVar('--accent-2'), backgroundColor: cssVar('--accent-2'),
          borderWidth: 2, pointRadius: 1.5, tension: 0.25, yAxisID: 'y1' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { type: 'time', stacked: true,
             time: { unit: 'hour', tooltipFormat: 'MMM d HH:mm' },
             ticks: { color: cssVar('--fg-muted'), maxTicksLimit: 14 },
             grid: { color: cssVar('--border-soft') } },
        y: { stacked: true, position: 'left',
             title: { display: true, text: 'Wh', color: cssVar('--fg-dim') },
             ticks: { color: cssVar('--fg-muted') },
             grid: { color: cssVar('--border-soft') } },
        y1: { position: 'right', ticks: { color: cssVar('--fg-dim') },
              grid: { display: false } },
      },
      plugins: { legend: { labels: { color: cssVar('--fg-muted') } } },
    },
  });
}

function enOnControlChange() {
  enRefresh();
}

function initEnergyTab() {
  const sel = _enEl('enWindow');
  if (!sel) return;
  if (!_enLoaded) {
    _enLoaded = true;
    EN.windowOptions().forEach(o => {
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      sel.appendChild(opt);
    });
  }
  enRefresh();
}
