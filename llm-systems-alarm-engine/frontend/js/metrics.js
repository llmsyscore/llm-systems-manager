// Metrics view: catalog + pickers (MetricsManager / MetricsView) and the
// single range-driven chart with stats and trend (ChartManager).

const MetricsManager = {
    // Agent hostnames from the manager registry; null = unknown, show every host.
    _knownAgentHosts: null,

    get metrics() { return AppState.metrics; },

    // Synthetic samples that never belong in operator-facing pickers.
    isProbeMetric(m) {
        const synthetic = (s) => typeof s === 'string' && (s.startsWith('__probe') || s === 'benchtest');
        return synthetic(m.hostname) || synthetic(m.source) || synthetic(m.metric_name);
    },

    async _loadAgentHosts() {
        try {
            const res = await fetch('/api/agents', { credentials: 'same-origin' });
            if (!res.ok) return;
            const body = await res.json();
            const hosts = new Set();
            for (const a of body.agents || []) if (a.hostname) hosts.add(a.hostname);
            this._knownAgentHosts = hosts.size ? hosts : null;
        } catch (_) { /* registry unreachable → permissive */ }
    },

    async load() {
        UIStates.setLoading('metrics', true);
        try {
            const [metrics] = await Promise.all([ApiClient.metrics.list(), this._loadAgentHosts()]);
            AppState.metrics = Array.isArray(metrics) ? metrics : [];
            if (AppState.currentTab === 'metrics') MetricsView.populate();
        } finally {
            UIStates.setLoading('metrics', false);
        }
    },

    visible() {
        const known = this._knownAgentHosts;
        return AppState.metrics.filter(m => {
            if (this.isProbeMetric(m)) return false;
            if (!m.hostname) return true;
            return !(known && !known.has(m.hostname));
        });
    },

    hosts() {
        return [...new Set(this.visible().map(m => m.hostname).filter(Boolean))].sort();
    },

    sources() {
        return [...new Set(this.visible().map(m => m.source).filter(Boolean))].sort();
    },

    find(source, name, host) {
        const list = this.visible();
        return list.find(m => m.source === source && m.metric_name === name && (!host || (m.hostname || '') === host))
            || list.find(m => m.source === source && m.metric_name === name) || null;
    },

    unitFor(source, name, host) {
        return this.find(source, name, host)?.unit || '';
    },

    newestTs() {
        let best = null;
        for (const m of this.visible()) {
            const t = parseTs(m.latest_timestamp);
            if (t && (!best || t > best)) best = t;
        }
        return best;
    },

    handleMetricUpdate(payload) {
        if (!payload || !payload.source || !payload.metric_name) return;
        const idx = AppState.metrics.findIndex(m => m.source === payload.source && m.metric_name === payload.metric_name && (m.hostname || '') === (payload.hostname || ''));
        if (idx !== -1) AppState.metrics[idx] = { ...AppState.metrics[idx], ...payload };
        else AppState.metrics.push(payload);
        if (AppState.currentTab === 'metrics') MetricsView.summary();
    },
};

// Readable metric names and sub-groups for the picker.
const MetricNames = {
    _hide: [/^disk_snap_/, /^disk_loop/, /^disk_boot/, /^disk_System_Volumes_/, /_pid$/, /(^|_)port$/, /^chat_template_len$/, /^latency_synthetic$/, /^selfwrite_probe$/],
    _acr: new Set(['cpu', 'gpu', 'ram', 'vram', 'io', 'kv', 'sse', 'api', 'ae', 'rss', 'ane', 'soc', 'mclk', 'sclk', 'lms', 'id', 'ctx', 'efi', 'vm', 'pcore', 'ecore']),
    _units: [['_percent', '%'], ['_pct', '%'], ['_c', '°C'], ['_mhz', 'MHz'], ['_mv', 'mV'], ['_watts', 'W'], ['_w', 'W'], ['_ms', 'ms'], ['_bytes_per_sec', 'bytes/s'], ['_bytes_per_s', 'bytes/s'], ['_per_sec', '/s'], ['_per_s', '/s'], ['_pkts_s', 'pkts/s'], ['_bytes', 'bytes'], ['_mb', 'MB'], ['_rpm', 'RPM'], ['_tokens', 'tokens'], ['_ratio', 'ratio'], ['_s', 's']],
    _sys: [[/^cpu/, 'cpu'], [/^gpu/, 'gpu'], [/^(ram|swap|mem)/, 'memory'], [/^disk_io/, 'disk io'], [/^disk/, 'disk'], [/^net/, 'network'], [/^(temp|thermal|fan)/, 'thermal'], [/^(power|psu)/, 'power']],
    _mac: [[/^(cpu|ecore|pcore)/, 'cpu'], [/^gpu/, 'gpu'], [/^net/, 'network'], [/_w$/, 'power'], [/^thermal/, 'thermal']],
    _procRe: /^(.+?)_(running|available|count|pid|rss_mb|uptime_s)$/,

    hidden(m) { return this._hide.some(re => re.test(m.metric_name || '')); },

    group(source, name) {
        if (source === 'system') return `system · ${(this._sys.find(([re]) => re.test(name)) || [null, 'other'])[1]}`;
        if (source === 'mac_power') return `mac_power · ${(this._mac.find(([re]) => re.test(name)) || [null, 'other'])[1]}`;
        if (source === 'processes') { const mm = this._procRe.exec(name); return mm ? `processes · ${mm[1]}` : 'processes'; }
        return source;
    },

    _words(s) {
        return s.split(/[_\-]+/).filter(Boolean).map((w, i) => {
            const lw = w.toLowerCase();
            if (this._acr.has(lw)) return lw.toUpperCase();
            if (/^\d/.test(w)) return w;
            return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : lw;
        }).join(' ');
    },

    // "gpu_vram_usage_percent" → "GPU VRAM usage (%)"; catalog unit wins.
    label(m) {
        let name = m.metric_name || '';
        const src = m.source || '';
        if (src === 'processes') { const mm = this._procRe.exec(name); if (mm) name = mm[2]; }
        if (src === 'system' && /^disk_/.test(name) && !/^disk_io/.test(name)) name = name.replace(/^disk_/, '');
        let unit = m.unit || '';
        if (!unit) {
            for (const [suf, u] of this._units) {
                if (name.endsWith(suf) && name.length > suf.length) { unit = u; name = name.slice(0, -suf.length); break; }
            }
        } else {
            for (const [suf] of this._units) { if (name.endsWith(suf) && name.length > suf.length) { name = name.slice(0, -suf.length); break; } }
        }
        const text = this._words(name) || m.metric_name;
        return unit ? `${text} (${unit})` : text;
    },
};

const MetricsView = {
    _wired: false,

    init() {
        const f = AppState.filters.metrics;
        try {
            const saved = JSON.parse(localStorage.getItem('ae.metrics.pick') || 'null');
            if (saved && typeof saved === 'object') {
                if (typeof saved.host === 'string') f.host = saved.host;
                if (typeof saved.key === 'string') f.key = saved.key;
                if ([5, 15, 60, 360, 1440, 10080, 43200].includes(saved.minutes)) f.minutes = saved.minutes;
            }
        } catch (_) {}
        document.getElementById('metricHost')?.addEventListener('change', (e) => { f.host = e.target.value; f.key = ''; this.populate(); this._persist(); ChartManager.load(); });
        document.getElementById('metricSelect')?.addEventListener('change', (e) => { f.key = e.target.value; this._persist(); ChartManager.load(); });
        UI.seg(document.getElementById('metricRange'), v => { f.minutes = parseInt(v, 10) || 60; f.offset = 0; this._persist(); this._liveTag(); ChartManager.load(); });
        UI.segSet(document.getElementById('metricRange'), f.minutes);
        document.getElementById('exportHistoryBtn')?.addEventListener('click', () => this.exportCsv());
        document.getElementById('metricPrev')?.addEventListener('click', () => this.shift(1));
        document.getElementById('metricNext')?.addEventListener('click', () => this.shift(-1));
        document.getElementById('metricLiveBtn')?.addEventListener('click', () => { f.offset = 0; this._liveTag(); ChartManager.load(); });
        f.offset = 0;
        this._liveTag();
    },

    _persist() {
        const { host, key, minutes } = AppState.filters.metrics;
        try { localStorage.setItem('ae.metrics.pick', JSON.stringify({ host, key, minutes })); } catch (_) {}
    },

    // Whole windows back from now; 30 days is the history ceiling.
    maxOffset() { return Math.max(0, Math.floor(43200 / AppState.filters.metrics.minutes) - 1); },

    // [start, end] ms of the selected window.
    windowBounds() {
        const f = AppState.filters.metrics;
        const end = Date.now() - (f.offset || 0) * f.minutes * 60000;
        return [end - f.minutes * 60000, end];
    },

    // The alert mark, when it belongs to the charted metric and falls inside the window.
    markInView() {
        const f = AppState.filters.metrics;
        const m = f.mark;
        if (!m?.ts || m.key !== f.key) return null;
        const [start, end] = this.windowBounds();
        const t = m.ts.getTime();
        return t >= start && t <= end ? m : null;
    },

    shift(dir) {
        const f = AppState.filters.metrics;
        f.offset = Math.min(this.maxOffset(), Math.max(0, (f.offset || 0) + dir));
        this._liveTag();
        ChartManager.load();
    },

    _liveTag() {
        const f = AppState.filters.metrics;
        const tag = document.getElementById('metricLiveTag');
        const back = document.getElementById('metricLiveBtn');
        const off = f.offset || 0;
        const marked = this.markInView();
        if (tag) tag.textContent = marked ? `around ${fmtWhen(marked.ts, true)}` : off ? `${off} window${off === 1 ? '' : 's'} back` : (f.minutes <= 60 ? 'live · refreshes every 60 s' : '1-minute rollups');
        if (back) back.hidden = !off;
        const prev = document.getElementById('metricPrev'), next = document.getElementById('metricNext');
        if (prev) prev.disabled = off >= this.maxOffset();
        if (next) next.disabled = off === 0;
    },

    keyOf(m) { return `${m.hostname || '*'}|${m.source}/${m.metric_name}`; },

    parseKey(key) {
        let host = null, rest = key || '';
        const p = rest.indexOf('|');
        if (p >= 0) { const h = rest.slice(0, p); host = (h && h !== '*') ? h : null; rest = rest.slice(p + 1); }
        const s = rest.indexOf('/');
        return { host, source: s >= 0 ? rest.slice(0, s) : rest, name: s >= 0 ? rest.slice(s + 1) : '' };
    },

    // Fills the host + metric selects from the catalog, keeping the selection.
    populate() {
        const f = AppState.filters.metrics;
        const hostSel = document.getElementById('metricHost');
        const metSel = document.getElementById('metricSelect');
        if (!hostSel || !metSel) return;
        const hosts = MetricsManager.hosts();
        if (f.host && !hosts.includes(f.host)) {
            f.host = '';
            const p = this.parseKey(f.key);
            if (p.host) { f.key = `*|${p.source}/${p.name}`; if (f.mark) f.mark.key = f.key; }
        }
        if (!f.host && !f.key) f.host = hosts.find(h => /manager/.test(h)) || hosts[0] || '';
        hostSel.innerHTML = `<option value=""${f.host ? '' : ' selected'}>All hosts</option>` + hosts.map(h => `<option value="${escapeHtml(h)}"${h === f.host ? ' selected' : ''}>${escapeHtml(h)}</option>`).join('');
        const list = MetricsManager.visible().filter(m => (!f.host || (m.hostname || '') === f.host) && !MetricNames.hidden(m));
        const bySource = new Map();
        for (const m of list) {
            const k = f.host ? this.keyOf(m) : `*|${m.source}/${m.metric_name}`;
            const g = MetricNames.group(m.source, m.metric_name);
            if (!bySource.has(g)) bySource.set(g, new Map());
            if (!bySource.get(g).has(k)) bySource.get(g).set(k, m);
        }
        const groups = [...bySource.keys()].sort().map(g => {
            const items = [...bySource.get(g).entries()].map(([k, m]) => [k, m, MetricNames.label(m)]).sort((a, b) => a[2].localeCompare(b[2]));
            return `<optgroup label="${escapeHtml(g)}">${items.map(([k, m, lbl]) => `<option value="${escapeHtml(k)}"${k === f.key ? ' selected' : ''}>${escapeHtml(lbl)}</option>`).join('')}</optgroup>`;
        });
        metSel.innerHTML = groups.join('') || '<option value="">No metrics reported yet</option>';
        const keys = [...bySource.values()].flatMap(m => [...m.keys()]);
        if (!keys.includes(f.key)) {
            if (f.mark && f.mark.key === f.key) { ToastManager.show(`No history for ${this.parseKey(f.key).name}`, 'info'); f.mark = null; }
            const pref = list.find(m => m.source === 'system' && m.metric_name === 'cpu_total') || list[0];
            f.key = pref ? (f.host ? this.keyOf(pref) : `*|${pref.source}/${pref.metric_name}`) : '';
            metSel.value = f.key;
        }
        this.summary();
    },

    summary() {
        const el = document.getElementById('metricsSum');
        if (!el) return;
        const vis = MetricsManager.visible();
        const newest = MetricsManager.newestTs();
        el.innerHTML = `<span><b>${vis.length}</b> tracked</span><span class="sep">·</span><span><b>${MetricsManager.hosts().length}</b> hosts</span><span class="sep">·</span><span><b>${MetricsManager.sources().length}</b> sources</span><span class="sep">·</span><span>newest sample <b>${escapeHtml(newest ? fmtAgo(newest) : '—')}</b></span>`;
    },

    _target(host, source, name) {
        const f = AppState.filters.metrics;
        f.host = host || '';
        f.key = `${host || '*'}|${source}/${name}`;
        this._persist();
    },

    // From a rule row: show this metric.
    showFor(host, source, name) {
        this._target(host, source, name);
        TabManager.switchTab('metrics');
    },

    // From an alert: the 1-hour window holding `at`, with the alert marked on the chart.
    showAround(host, source, name, at, label) {
        const f = AppState.filters.metrics;
        const ts = parseTs(at);
        f.minutes = 60;
        f.offset = ts ? Math.min(this.maxOffset(), Math.max(0, Math.floor((Date.now() - ts.getTime()) / (f.minutes * 60000)))) : 0;
        f.mark = ts ? { ts, label: label || 'Alert', key: `${host || '*'}|${source}/${name}` } : null;
        UI.segSet(document.getElementById('metricRange'), f.minutes);
        this._target(host, source, name);
        this._liveTag();
        TabManager.switchTab('metrics');
    },

    async exportCsv() {
        const f = AppState.filters.metrics;
        if (!f.key) { ToastManager.show('Pick a metric first', 'info'); return; }
        const { host, source, name } = this.parseKey(f.key);
        try {
            const blob = await ApiClient.metrics.exportCsv({ source, metric_name: name, since_minutes: f.minutes, hostname: host || f.host || '' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = `${host ? host + '_' : ''}${source}_${name}.csv`;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            ToastManager.show(`Export failed: ${e?.message || 'unknown'}`, 'error');
        }
    },
};

// ── charts ───────────────────────────────────────────────────────────

// Alert-mark label is pushed inward by MARK_LABEL_PX when within MARK_LABEL_EDGE of a window edge.
const MARK_LABEL_EDGE = 0.2, MARK_LABEL_PX = 70;

const ChartManager = {
    _chart: null,
    _trend: null,
    _ident: null,
    _points: [],
    _unit: '',
    _seq: 0,

    _tok(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    },

    _rgba(color, alpha) {
        let h = color.replace('#', '');
        if (h.length === 3) h = h.split('').map(c => c + c).join('');
        if (h.length !== 6) return color;
        const n = parseInt(h, 16);
        return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
    },

    _zoomOpts() {
        const accent = this._tok('--accent');
        return {
            zoom: {
                wheel: { enabled: false },
                drag: { enabled: true, borderColor: accent, borderWidth: 1, backgroundColor: this._rgba(accent, 0.12) },
                mode: 'xy',
                onZoomComplete: ({ chart }) => this._syncResetZoom(chart),
            },
            pan: { enabled: false },
            limits: { x: { min: 'original', max: 'original' } },
        };
    },

    _syncResetZoom(chart) {
        if (!chart || !chart.canvas) return;
        const wrap = chart.canvas.parentElement;
        const zoomed = typeof chart.isZoomedOrPanned === 'function' && chart.isZoomedOrPanned();
        let btn = wrap.querySelector('.chart-reset-zoom');
        if (!zoomed) { if (btn) btn.remove(); return; }
        if (!btn) {
            btn = document.createElement('button');
            btn.className = 'chart-reset-zoom'; btn.type = 'button'; btn.textContent = '⟲'; btn.title = 'Reset zoom';
            btn.addEventListener('click', (e) => { e.stopPropagation(); try { chart.resetZoom(); } catch (_) {} this._syncResetZoom(chart); });
            wrap.appendChild(btn);
        }
    },

    _timeAxis(spanMs) {
        const fg = this._tok('--fg-faint');
        const dayKey = (v) => { const d = new Date(Number(v)); return `${d.getMonth()}-${d.getDate()}`; };
        return {
            type: 'time',
            time: { tooltipFormat: 'MMM d, yyyy HH:mm:ss' },
            ticks: {
                color: fg, source: 'auto', autoSkip: true, autoSkipPadding: 24, maxTicksLimit: 9, maxRotation: 0,
                font: { family: 'IBM Plex Mono, ui-monospace, monospace', size: 10 },
                callback: (value, index, ticks) => {
                    const d = new Date(Number(value));
                    const time = fmtTime(d);
                    if (spanMs <= 24 * 3600 * 1000) return time;
                    const prev = index > 0 && ticks && ticks[index - 1] ? dayKey(ticks[index - 1].value) : null;
                    if (prev === dayKey(value)) return time;
                    return [time, `${_MONTHS[d.getMonth()]} ${d.getDate()}`];
                },
            },
            grid: { color: this._tok('--border-soft') },
            border: { color: this._tok('--border-soft') },
        };
    },

    _yAxis(unit) {
        return {
            ticks: {
                color: this._tok('--fg-faint'), font: { family: 'IBM Plex Mono, ui-monospace, monospace', size: 10 },
                callback: v => fmtVal(v, unit),
            },
            grid: { color: this._tok('--border-soft') },
            border: { color: this._tok('--border-soft') },
        };
    },

    _annotations() {
        if (!this._ident || !window.Thresholds) return {};
        const { host, source, name } = this._ident;
        const out = Thresholds.thresholdAnnotations(AppState.rules, { source, metricName: name, host, hostWildcard: true });
        const mark = MetricsView.markInView();
        if (mark) {
            const color = this._tok('--crit');
            const [start, end] = MetricsView.windowBounds();
            const frac = (mark.ts.getTime() - start) / (end - start);
            const nudge = frac > 1 - MARK_LABEL_EDGE ? -MARK_LABEL_PX : frac < MARK_LABEL_EDGE ? MARK_LABEL_PX : 0;
            out.alert_mark = {
                type: 'line', xMin: mark.ts.getTime(), xMax: mark.ts.getTime(), borderColor: color, borderWidth: 1.5, borderDash: [3, 3],
                label: { display: true, content: `${mark.label} · ${fmtTime(mark.ts)}`, position: 'end', xAdjust: nudge,
                    backgroundColor: 'transparent', color, font: { size: 10, family: 'IBM Plex Mono, ui-monospace, monospace' }, padding: { top: 0, bottom: 2, left: 4, right: 4 } },
            };
        }
        return out;
    },

    refreshAnnotations() {
        if (!this._chart || !this._chart.options.plugins?.annotation) return;
        try {
            this._chart.options.plugins.annotation.annotations = this._annotations();
            this._chart.update('none');
        } catch (_) {}
        this._renderRuleChips();
    },

    retint() {
        if (AppState.currentTab === 'metrics' && this._points.length) this._draw();
    },

    async refresh() {
        MetricsView.populate();
        await this.load();
    },

    async load() {
        const f = AppState.filters.metrics;
        if (!f.key) { this._empty('Pick a metric to chart.'); return; }
        const { host, source, name } = MetricsView.parseKey(f.key);
        const m = MetricsManager.find(source, name, host);
        this._ident = { host, source, name };
        this._unit = m?.unit || '';
        const seq = ++this._seq;
        const offset = f.offset || 0;
        const now = Date.now();
        const winEnd = now - offset * f.minutes * 60000, winStart = winEnd - f.minutes * 60000;
        this._window = offset ? [winStart, winEnd] : null;
        let points = [];
        try {
            points = await ApiClient.metrics.getHistory(source, name, { since_minutes: Math.min(43200, f.minutes * (offset + 1)), hostname: host || '' });
        } catch (e) {
            ToastManager.show(`Could not load ${source}/${name}: ${e?.message || 'fetch error'}`, 'error');
        }
        if (seq !== this._seq) return;
        if (!Array.isArray(points)) points = [];
        if (offset) points = points.filter(p => { const t = parseTs(p.timestamp)?.getTime() || 0; return t >= winStart && t <= winEnd; });
        if (!points.length && !offset && m?.latest_value !== undefined) {
            points = [{ timestamp: m.latest_timestamp || new Date().toISOString(), value: m.latest_value }];
        }
        points.sort((a, b) => (parseTs(a.timestamp)?.getTime() || 0) - (parseTs(b.timestamp)?.getTime() || 0));
        this._points = points;
        this._draw();
        this._renderRuleChips();
        this._stats(source, name, f.minutes, !!offset);
        this._trendCard();
    },

    _empty(text) {
        const wrap = document.getElementById('metricChartWrap');
        if (this._chart) { this._chart.destroy(); this._chart = null; }
        const readout = document.getElementById('metricReadout');
        if (readout) readout.hidden = true;
        let msg = wrap?.querySelector('.empty');
        if (wrap && !msg) { msg = document.createElement('div'); msg.className = 'empty'; wrap.appendChild(msg); }
        if (msg) msg.textContent = text;
        const canvas = document.getElementById('metricChart');
        if (canvas) canvas.hidden = true;
        const stats = document.getElementById('metricStats');
        if (stats) stats.innerHTML = '';
    },

    _draw() {
        const canvas = document.getElementById('metricChart');
        const wrap = document.getElementById('metricChartWrap');
        if (!canvas || typeof Chart === 'undefined') return;
        wrap?.querySelector('.empty')?.remove();
        canvas.hidden = false;
        if (this._chart) { this._chart.destroy(); this._chart = null; }
        const accent = this._tok('--accent');
        const pts = this._points.map(p => ({ x: parseTs(p.timestamp), y: Number(p.value) })).filter(p => p.x && Number.isFinite(p.y));
        if (!pts.length) { this._empty(this._window ? 'No samples in this window.' : 'No samples in this range yet.'); return; }
        const span = pts[pts.length - 1].x - pts[0].x;
        const unit = this._unit;
        const self = this;
        this._chart = new Chart(canvas, {
            type: 'line',
            data: { datasets: [{
                label: `${this._ident.source}/${this._ident.name}`, data: pts,
                borderColor: accent, backgroundColor: this._rgba(accent, 0.12), borderWidth: 1.5,
                pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 10, pointBackgroundColor: accent,
                fill: true, tension: 0.25,
            }] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                scales: { x: this._timeAxis(span), y: this._yAxis(unit) },
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: false, external: (ctx) => self._readout(ctx.tooltip) },
                    zoom: this._zoomOpts(),
                    annotation: { annotations: this._annotations() },
                },
            },
        });
        this._readout(null);
    },

    // The pill above the chart: hovered point, or the newest one when idle.
    _readout(tooltip) {
        const el = document.getElementById('metricReadout');
        if (!el || !this._points.length) return;
        const pts = this._points;
        let p = pts[pts.length - 1];
        if (tooltip && tooltip.opacity !== 0 && tooltip.dataPoints?.length) {
            const dp = tooltip.dataPoints[0];
            p = { timestamp: dp.raw.x, value: dp.raw.y };
        }
        const t = parseTs(p.timestamp);
        const lastMin = pts.filter(q => (parseTs(q.timestamp)?.getTime() || 0) >= (parseTs(pts[pts.length - 1].timestamp)?.getTime() || 0) - 60000);
        const avg = lastMin.length ? lastMin.reduce((s, q) => s + Number(q.value), 0) / lastMin.length : null;
        let peak = pts[0];
        for (const q of pts) if (Number(q.value) > Number(peak.value)) peak = q;
        el.hidden = false;
        const win = this._window ? `<span class="win">${escapeHtml(fmtWhen(this._window[0]))} – ${escapeHtml(fmtTime(this._window[1]))}</span>` : '';
        el.innerHTML = `${win}<span><span class="k">${escapeHtml(fmtTime(t, true))}</span></span><span><b>${escapeHtml(fmtVal(p.value, this._unit))}</b></span><span><span class="k">1-min avg</span> <b>${escapeHtml(fmtVal(avg, this._unit))}</b></span><span><span class="k">peak</span> <b>${escapeHtml(fmtVal(peak.value, this._unit))}</b> at ${escapeHtml(fmtTime(peak.timestamp))}</span>`;
    },

    _thresholdRules() {
        if (!this._ident) return [];
        const { host, source, name } = this._ident;
        return AppState.rules.filter(r => r.enabled && r.metric_source === source && r.metric_name === name
            && (!r.source_host || !host || r.source_host === host)
            && ['threshold_above', 'threshold_below', 'threshold_range'].includes(r.rule_type));
    },

    _renderRuleChips() {
        const el = document.getElementById('metricRules');
        if (!el) return;
        const rules = this._thresholdRules();
        if (!rules.length) { el.innerHTML = '<span class="l">no threshold rules on this metric</span>'; return; }
        el.innerHTML = '<span class="l">rules on this metric</span>' + rules.map(r => {
            const t = r.config?.threshold || {};
            const v = r.rule_type === 'threshold_below' ? t.lower : (t.upper ?? t.value);
            const txt = r.rule_type === 'threshold_range' ? `${fmtNum(t.lower)}–${fmtNum(t.upper)}` : fmtNum(v);
            const cls = r.severity === 'critical' ? 'crit' : r.severity === 'info' ? 'info' : '';
            return `<span class="t ${cls}" title="${escapeHtml(r.name)}">${escapeHtml(r.name)} <i></i> ${escapeHtml(txt)}${t.unit ? ' ' + escapeHtml(t.unit) : ''}</span>`;
        }).join('');
    },

    _localStats() {
        const vals = this._points.map(p => Number(p.value)).filter(Number.isFinite).sort((a, b) => a - b);
        if (!vals.length) return null;
        const n = vals.length;
        const mean = vals.reduce((a, b) => a + b, 0) / n;
        const sd = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
        const q = (p) => vals[Math.min(n - 1, Math.floor(p * (n - 1)))];
        return { min_value: vals[0], max_value: vals[n - 1], avg_value: mean, std_dev: sd, p50: q(0.5), p95: q(0.95), p99: q(0.99), count: n };
    },

    async _stats(source, name, minutes, local = false) {
        const el = document.getElementById('metricStats');
        if (!el) return;
        let s = null;
        if (local) s = this._localStats();
        else { try { s = await ApiClient.metrics.getSummary(source, name, { window_minutes: minutes }); } catch (_) { s = this._localStats(); } }
        if (!s) { el.innerHTML = ''; return; }
        const unit = this._unit;
        const rules = this._thresholdRules();
        const warnAt = rules.filter(r => r.rule_type === 'threshold_above' && r.severity === 'warning').map(r => Number(r.config?.threshold?.upper ?? r.config?.threshold?.value)).filter(Number.isFinite);
        const critAt = rules.filter(r => r.rule_type === 'threshold_above' && r.severity === 'critical').map(r => Number(r.config?.threshold?.upper ?? r.config?.threshold?.value)).filter(Number.isFinite);
        const max = Number(s.max_value);
        const maxCls = critAt.length && max >= Math.min(...critAt) ? 'crit' : warnAt.length && max >= Math.min(...warnAt) ? 'warn' : '';
        const cell = (k, v, cls = '', small = '') => `<div><span class="k">${k}</span><span class="v ${cls}">${escapeHtml(v)}${small ? `<small>${escapeHtml(small)}</small>` : ''}</span></div>`;
        const num = (v) => fmtNum(v);
        el.innerHTML = cell('Min', num(s.min_value), '', unit) + cell('Max', num(s.max_value), maxCls, unit) + cell('Mean', num(s.avg_value), '', unit)
            + cell('Std dev', num(s.std_dev)) + cell('p50', num(s.p50), '', unit) + cell('p95', num(s.p95), '', unit) + cell('p99', num(s.p99), '', unit)
            + cell('Samples', String(s.count ?? this._points.length), '', minutes <= 60 ? 'raw' : 'rollup');
    },

    _trendCard() {
        const canvas = document.getElementById('trendChart');
        const note = document.getElementById('trendNote');
        if (!canvas || typeof Chart === 'undefined') return;
        if (this._trend) { this._trend.destroy(); this._trend = null; }
        const pts = this._points.map(p => ({ x: parseTs(p.timestamp), y: Number(p.value) })).filter(p => p.x && Number.isFinite(p.y));
        const n = pts.length;
        if (n < 3) {
            canvas.hidden = true;
            if (note) note.textContent = 'Not enough samples yet for a trend.';
            return;
        }
        canvas.hidden = false;
        const win = Math.min(30, Math.max(3, Math.floor(n * 0.1)));
        const ma = pts.map((_, i) => {
            const s = Math.max(0, i - Math.floor(win / 2)), e = Math.min(n, s + win);
            const slice = pts.slice(s, e);
            return { x: pts[i].x, y: slice.reduce((a, p) => a + p.y, 0) / slice.length };
        });
        const ys = pts.map(p => p.y);
        const sumX = (n * (n - 1)) / 2, sumX2 = (n * (n - 1) * (2 * n - 1)) / 6;
        const sumY = ys.reduce((a, b) => a + b, 0), sumXY = ys.reduce((s, y, i) => s + i * y, 0);
        const slope = (n * sumXY - sumX * sumY) / (n * sumX2 - sumX * sumX) || 0;
        const intercept = (sumY - slope * sumX) / n;
        const fit = [{ x: pts[0].x, y: intercept }, { x: pts[n - 1].x, y: slope * (n - 1) + intercept }];
        const accent = this._tok('--accent'), accent2 = this._tok('--accent-2'), dim = this._tok('--fg-dim');
        const span = pts[n - 1].x - pts[0].x;
        this._trend = new Chart(canvas, {
            type: 'line',
            data: { datasets: [
                { data: pts, borderColor: this._rgba(accent, 0.28), borderWidth: 1, pointRadius: 0, tension: 0, order: 3 },
                { data: ma, borderColor: accent2, borderWidth: 1.4, borderDash: [4, 3], pointRadius: 0, tension: 0.3, order: 2 },
                { data: fit, borderColor: dim, borderWidth: 1, borderDash: [2, 3], pointRadius: 0, tension: 0, order: 1 },
            ] },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                scales: { x: this._timeAxis(span), y: this._yAxis(this._unit) },
                plugins: { legend: { display: false }, tooltip: { enabled: false }, zoom: this._zoomOpts() },
            },
        });
        if (!note) return;
        const hours = Math.max(span / 3600000, 1 / 60);
        const perHour = (fit[1].y - fit[0].y) / hours;
        const mean = sumY / n;
        const sd = Math.sqrt(ys.reduce((a, y) => a + (y - mean) ** 2, 0) / n) || 0;
        const lastT = pts[n - 1].x.getTime() - 5 * 60000;
        const last5 = pts.filter(p => p.x.getTime() >= lastT);
        const last5Mean = last5.length ? last5.reduce((a, p) => a + p.y, 0) / last5.length : pts[n - 1].y;
        const z = sd ? (last5Mean - mean) / sd : 0;
        const rel = Math.abs(fit[0].y) > 1e-9 ? (fit[1].y - fit[0].y) / Math.abs(fit[0].y) * 100 : 0;
        const dir = Math.abs(rel) < 2 ? 'Flat' : rel > 0 ? 'Rising' : 'Falling';
        const unitTxt = this._unit ? ` ${this._unit}` : '';
        const warnAt = this._thresholdRules().filter(r => r.rule_type === 'threshold_above').map(r => Number(r.config?.threshold?.upper ?? r.config?.threshold?.value)).filter(Number.isFinite);
        let breaches = 0;
        if (warnAt.length) {
            const line = Math.min(...warnAt);
            let above = false;
            for (const p of pts) { const now = p.y >= line; if (now && !above) breaches++; above = now; }
        }
        const rangeTxt = hours >= 1 ? `${Math.round(hours)} hour${Math.round(hours) === 1 ? '' : 's'}` : `${Math.round(hours * 60)} minutes`;
        note.innerHTML = `${dir === 'Flat' ? '<b>Flat</b>' : `${dir} <b class="${dir === 'Rising' ? 'up' : 'dn'}">${rel > 0 ? '+' : ''}${escapeHtml(fmtNum(perHour))}${escapeHtml(unitTxt)} per hour</b>`} over the last ${escapeHtml(rangeTxt)}; the rolling mean is <b>${escapeHtml(fmtVal(mean, this._unit))}</b> and the last 5 minutes sit <b>${escapeHtml(fmtNum(Math.abs(z)))}σ</b> ${z >= 0 ? 'above' : 'below'} it.${warnAt.length ? ` ${breaches === 0 ? 'No breaches' : breaches === 1 ? 'One breach' : `${breaches} breaches`} of the ${escapeHtml(fmtVal(Math.min(...warnAt), this._unit))} line in this window.` : ''}`;
    },
};

// Double-click a chart to reset its zoom.
document.addEventListener('dblclick', (e) => {
    [ChartManager._chart, ChartManager._trend].forEach(c => {
        if (c && c.canvas === e.target && typeof c.resetZoom === 'function') {
            try { c.resetZoom(); } catch (_) {}
            ChartManager._syncResetZoom(c);
        }
    });
});
