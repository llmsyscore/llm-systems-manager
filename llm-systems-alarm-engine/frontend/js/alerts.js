// Alerts: the data layer shared by every view (AlertManager) and the Alerts
// ledger with its detail drawer (AlertsView).

const AlertManager = {
    _inflight: new Set(),
    _deliveries: { at: 0, rows: [], promise: null },

    get all() { return AppState.alerts; },

    async load() {
        UIStates.setLoading('alerts', true);
        try {
            const rows = await ApiClient.alerts.list({ include_closed: 'true', limit: 500 });
            const list = Array.isArray(rows) ? rows : [];
            list.sort((a, b) => (parseTs(b.created_at)?.getTime() || 0) - (parseTs(a.created_at)?.getTime() || 0));
            AppState.alerts = list;
            UI.setNavCount(this.active().length);
        } finally {
            UIStates.setLoading('alerts', false);
        }
    },

    active() { return AppState.alerts.filter(a => a.status === 'active'); },
    open() { return AppState.alerts.filter(a => a.status === 'active' || a.status === 'acknowledged'); },
    byId(id) { return AppState.alerts.find(a => String(a.alert_id) === String(id)) || null; },
    ruleOf(alert) { return alert && alert.rule_id ? RuleManager.byId(alert.rule_id) : null; },
    isAnomaly(alert) {
        const r = this.ruleOf(alert);
        return !!(r && ANOMALY_TYPES.has(r.rule_type));
    },

    // incident key → members, newest first; insertion order follows `list`.
    byIncident(list) {
        const groups = new Map();
        for (const a of list) {
            const key = String(a.incident_id || a.alert_id);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(a);
        }
        // Root alert (alert_id == incident_id) leads; joiners follow newest first.
        for (const [key, members] of groups) {
            members.sort((a, b) => {
                const ra = String(a.alert_id) === key ? 0 : 1, rb = String(b.alert_id) === key ? 0 : 1;
                if (ra !== rb) return ra - rb;
                return (parseTs(b.created_at)?.getTime() || 0) - (parseTs(a.created_at)?.getTime() || 0);
            });
        }
        return groups;
    },

    unitOf(alert) {
        const r = this.ruleOf(alert);
        const u = r?.config?.threshold?.unit;
        if (u) return u;
        return MetricsManager.unitFor(alert.metric_source, alert.metric_name, alert.source_host) || '';
    },

    // Drops a legacy "[rule] " prefix from stored messages.
    cleanMessage(alert) {
        const msg = alert.message || '';
        const name = alert.rule_name || '';
        if (name && msg.startsWith(`[${name}]`)) return msg.slice(name.length + 2).trimStart();
        return msg;
    },

    // Escaped message with the first numeric token (and its unit) bolded.
    emphasize(text) {
        const safe = escapeHtml(text || '');
        return safe.replace(/([+-]?\d+(?:[.,]\d+)?\s*(?:%|°C|°F|ms|MB|GB|TB|KB|W|tps|Mbps|count|sessions|B|σ)?)/, '<b>$1</b>');
    },

    // {value, limit, over, sentence, detector} for a row or the drawer.
    describe(alert) {
        const rule = this.ruleOf(alert);
        const unit = this.unitOf(alert);
        const v = alert.current_value;
        const t = alert.threshold_value;
        const value = fmtVal(v, unit);
        const limit = fmtVal(t, unit);
        const type = rule?.rule_type || '';
        const cfg = rule?.config || {};
        let sentence = '';
        let detector = '';
        let over = v != null && t != null && Number(v) > Number(t);
        if (type === 'threshold_above') {
            sentence = `<b>${escapeHtml(value)}</b> over the ${escapeHtml(limit)} limit`;
        } else if (type === 'threshold_below') {
            over = v != null && t != null && Number(v) < Number(t);
            sentence = `<b>${escapeHtml(value)}</b> under the ${escapeHtml(limit)} floor`;
        } else if (type === 'threshold_range') {
            const lo = cfg.threshold?.lower, hi = cfg.threshold?.upper;
            sentence = `<b>${escapeHtml(value)}</b> outside ${escapeHtml(fmtVal(lo, unit))} – ${escapeHtml(fmtVal(hi, unit))}`;
            over = true;
        } else if (type === 'percentile') {
            const c = cfg.percentile || {};
            detector = `p${c.percentile ?? '?'} · ${fmtDur((c.window_minutes || 0) * 60)} window`;
            sentence = `<b>${escapeHtml(value)}</b> above the p${escapeHtml(String(c.percentile ?? '?'))} baseline of ${escapeHtml(limit)}`;
        } else if (type === 'z_score') {
            const c = cfg.z_score || {};
            detector = `z-score · ${fmtDur((c.window_minutes || 0) * 60)} window`;
            sentence = `${this.emphasize(this.cleanMessage(alert))}`;
        } else if (type === 'moving_average') {
            const c = cfg.moving_average || {};
            detector = `moving average · ${fmtDur((c.window_minutes || 0) * 60)} · ±${c.deviation_factor ?? '?'}σ`;
            sentence = `${this.emphasize(this.cleanMessage(alert))}`;
        } else if (type === 'rate_of_change') {
            const c = cfg.rate_of_change || {};
            detector = `rate of change · ${fmtDur((c.window_minutes || 0) * 60)} window`;
            sentence = `<b>${escapeHtml(fmtNum(v))}${unit ? ' ' + escapeHtml(unit) : ''}/min</b> over the ${escapeHtml(fmtNum(c.max_change_per_minute))}/min limit`;
        } else {
            sentence = this.emphasize(this.cleanMessage(alert)) || `<b>${escapeHtml(value)}</b> vs ${escapeHtml(limit)}`;
        }
        if (!sentence) sentence = this.emphasize(this.cleanMessage(alert));
        return { value, limit, over, sentence, detector, unit };
    },

    // ── actions ───────────────────────────────────────────────────────
    async _run(id, fn, okMsg, errMsg) {
        if (this._inflight.has(id)) return false;
        this._inflight.add(id);
        try {
            await fn();
            if (okMsg) ToastManager.show(okMsg, 'success');
            await this._afterChange();
            return true;
        } catch (e) {
            ToastManager.show(`${errMsg}: ${e?.message || 'request failed'}`, 'error');
            return false;
        } finally {
            this._inflight.delete(id);
        }
    },

    acknowledge(id) {
        return this._run(id, () => ApiClient.alerts.acknowledge(id), 'Alert acknowledged', 'Could not acknowledge');
    },

    close(id) {
        return this._run(id, () => ApiClient.alerts.close(id), 'Alert closed', 'Could not close');
    },

    async ignore(id) {
        const hours = await ModalManager.chooseDuration({ title: 'Ignore alert', confirmLabel: 'Ignore' });
        if (hours == null) return false;
        return this._run(id, () => ApiClient.alerts.ignore(id, hours), `Ignored for ${hours} h`, 'Could not ignore');
    },

    // Ends an ignore window by closing the alert; the rule fires again if still over the limit.
    resume(id) {
        return this._run(id, () => ApiClient.alerts.close(id), 'Ignore window ended', 'Could not resume');
    },

    async closeAll() {
        const active = this.active();
        if (!active.length) { ToastManager.show('No active alerts to close', 'info'); return; }
        const ok = await ModalManager.confirm({
            title: 'Close all active alerts',
            message: `Close ${active.length} active alert${active.length === 1 ? '' : 's'}? Rules that are still over their limit will trigger again.`,
            confirmLabel: 'Close all', danger: true,
        });
        if (!ok) return;
        const results = await Promise.allSettled(active.map(a => ApiClient.alerts.close(a.alert_id)));
        const failed = results.filter(r => r.status === 'rejected').length;
        ToastManager.show(failed ? `Closed ${active.length - failed}, ${failed} failed` : `Closed ${active.length} alert${active.length === 1 ? '' : 's'}`, failed ? 'warning' : 'success');
        await this._afterChange();
    },

    async bulk(ids, action) {
        if (!ids.length) return false;
        let data = {};
        if (action === 'ignore') {
            const hours = await ModalManager.chooseDuration({ title: `Ignore ${ids.length} alert${ids.length === 1 ? '' : 's'}`, confirmLabel: 'Ignore' });
            if (hours == null) return false;
            data = { duration_hours: hours };
        } else {
            const ok = await ModalManager.confirm({
                title: action === 'close' ? 'Close alerts' : 'Acknowledge alerts',
                message: `${action === 'close' ? 'Close' : 'Acknowledge'} ${ids.length} alert${ids.length === 1 ? '' : 's'}?`,
                confirmLabel: action === 'close' ? 'Close' : 'Acknowledge', danger: action === 'close',
            });
            if (!ok) return false;
        }
        try {
            const r = await ApiClient.alerts.bulkUpdate(ids, action, data);
            ToastManager.show(`${r?.updated ?? ids.length} alert${(r?.updated ?? ids.length) === 1 ? '' : 's'} ${action === 'acknowledge' ? 'acknowledged' : action + 'd'}`, 'success');
            await this._afterChange();
            return true;
        } catch (e) {
            ToastManager.show(`Bulk ${action} failed: ${e?.message || 'request failed'}`, 'error');
            return false;
        }
    },

    async _afterChange() {
        try { await this.load(); } catch (_) { /* stale list is still shown */ }
        this._deliveries.at = 0;
        if (AppState.currentTab === 'console') ConsoleView.render();
        else if (AppState.currentTab === 'alerts') AlertsView.render();
        else if (AppState.currentTab === 'rules') RuleManager.render();
    },

    // ── websocket ─────────────────────────────────────────────────────
    handleNewAlert(payload) {
        if (!payload || !payload.alert_id) return;
        if (!AppState.alerts.some(a => String(a.alert_id) === String(payload.alert_id))) {
            AppState.alerts.unshift(payload);
        }
        UI.setNavCount(this.active().length);
        this._rerender();
        this._afterChange();
    },

    handleAlertUpdate(payload) {
        const id = payload && (payload.alert_id || payload.id);
        if (!id) return;
        const idx = AppState.alerts.findIndex(a => String(a.alert_id) === String(id));
        if (idx !== -1) AppState.alerts[idx] = { ...AppState.alerts[idx], ...payload };
        UI.setNavCount(this.active().length);
        this._rerender();
        this._afterChange();
    },

    _rerender() {
        if (AppState.currentTab === 'console') ConsoleView.render();
        else if (AppState.currentTab === 'alerts') AlertsView.render();
    },

    // Delivery rows tagged with this alert id (history cached for 30 s).
    async deliveriesFor(alertId) {
        const now = Date.now();
        if (now - this._deliveries.at > 30000 && !this._deliveries.promise) {
            this._deliveries.promise = ApiClient.notifications.getHistory({ limit: 200 })
                .then(rows => { this._deliveries.rows = Array.isArray(rows) ? rows : []; this._deliveries.at = Date.now(); })
                .catch(() => {})
                .finally(() => { this._deliveries.promise = null; });
        }
        if (this._deliveries.promise) await this._deliveries.promise;
        return this._deliveries.rows.filter(d => d.metadata && String(d.metadata.alert_id) === String(alertId));
    },
};

// ── Alerts ledger view ───────────────────────────────────────────────

const AlertsView = {
    _sel: null,
    _checked: new Set(),
    _visible: [],

    init() {
        const f = AppState.filters.alerts;
        const search = document.getElementById('alertSearch');
        search?.addEventListener('input', () => { f.search = search.value; f.page = 1; this.render(); });
        UI.seg(document.getElementById('alertSev'), v => { f.severity = v; f.page = 1; this.render(); });
        const status = document.getElementById('alertStatus');
        if (status) status.value = f.status;
        status?.addEventListener('change', () => { f.status = status.value; f.page = 1; this.render(); });
        document.getElementById('exportAlertsBtn')?.addEventListener('click', () => this.exportCsv());
        document.getElementById('alertsSelectAll')?.addEventListener('click', () => {
            const all = this._visible.map(a => String(a.alert_id));
            const every = all.length && all.every(id => this._checked.has(id));
            if (every) all.forEach(id => this._checked.delete(id)); else all.forEach(id => this._checked.add(id));
            this.render();
        });
        document.getElementById('alertsBulk')?.addEventListener('click', async (e) => {
            const b = e.target.closest('[data-bulk]');
            if (!b) return;
            const ids = Array.from(this._checked);
            if (await AlertManager.bulk(ids, b.dataset.bulk)) this._checked.clear();
            this.render();
        });
        document.querySelectorAll('#alertsTable th.sort').forEach(th => th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (f.sort === key) f.dir = f.dir === 'asc' ? 'desc' : 'asc';
            else { f.sort = key; f.dir = ['fired', 'last', 'count'].includes(key) ? 'desc' : 'asc'; }
            this.render();
        }));
        document.getElementById('alertsBody')?.addEventListener('click', (e) => this._onRowClick(e));
        document.getElementById('alertDetail')?.addEventListener('click', (e) => this._onDetailClick(e));
        document.addEventListener('keydown', (e) => {
            if (AppState.currentTab !== 'alerts' || !this._sel || ModalManager.isOpen()) return;
            const t = e.target;
            if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return;
            if (e.key === 'ArrowLeft') this.step(-1);
            else if (e.key === 'ArrowRight') this.step(1);
            else if (e.key === 'Escape') this.select(null);
        });
    },

    _onRowClick(e) {
        const act = e.target.closest('[data-act]');
        const tr = e.target.closest('tr[data-id]');
        if (!tr) return;
        const id = tr.dataset.id;
        if (act) {
            e.stopPropagation();
            this._doAction(act.dataset.act, id);
            return;
        }
        if (e.target.closest('.inc')) {
            const key = e.target.closest('.inc').dataset.inc;
            document.querySelectorAll(`#alertsBody tr[data-child="${CSS.escape(key)}"]`).forEach(r => { r.hidden = !r.hidden; });
            return;
        }
        if (e.target.closest('.cb')) {
            if (this._checked.has(id)) this._checked.delete(id); else this._checked.add(id);
            this._renderBulk();
            e.target.closest('.cb').classList.toggle('on', this._checked.has(id));
            return;
        }
        if (e.target.closest('.rname')) {
            const a = AlertManager.byId(id);
            if (a?.rule_id) RuleManager.edit(a.rule_id);
            return;
        }
        this.select(this._sel === id ? null : id);
    },

    _onDetailClick(e) {
        const b = e.target.closest('[data-act]');
        if (!b) return;
        const act = b.dataset.act;
        if (act === 'prev') return this.step(-1);
        if (act === 'next') return this.step(1);
        if (act === 'closepanel') return this.select(null);
        if (act === 'openrule') { const a = AlertManager.byId(this._sel); if (a?.rule_id) RuleManager.edit(a.rule_id); return; }
        if (act === 'member') return this.select(b.dataset.id);
        if (act === 'metric') { const a = AlertManager.byId(this._sel); if (a) MetricsView.showAround(a.source_host, a.metric_source, a.metric_name, a.created_at, a.rule_name); return; }
        this._doAction(act, this._sel);
    },

    async _doAction(act, id) {
        if (act === 'ack') await AlertManager.acknowledge(id);
        else if (act === 'ignore') await AlertManager.ignore(id);
        else if (act === 'close') await AlertManager.close(id);
        else if (act === 'resume') await AlertManager.resume(id);
    },

    // ── filtering / ordering ─────────────────────────────────────────
    filtered() {
        const f = AppState.filters.alerts;
        const q = f.search.trim().toLowerCase();
        return AppState.alerts.filter(a => {
            if (f.status === 'open' && !(a.status === 'active' || a.status === 'acknowledged')) return false;
            if (f.status !== 'open' && f.status !== 'all' && a.status !== f.status) return false;
            if (f.severity !== 'all' && a.severity !== f.severity) return false;
            if (q) {
                const hay = [a.rule_name, a.message, a.source_host, a.metric_source, a.metric_name].filter(Boolean).join(' ').toLowerCase();
                if (!hay.includes(q)) return false;
            }
            return true;
        });
    },

    _sortKey(a, key) {
        switch (key) {
            case 'severity': return SEVERITY_RANK[a.severity] ?? 9;
            case 'rule': return (a.rule_name || '').toLowerCase();
            case 'status': return ['active', 'acknowledged', 'ignored', 'exception', 'closed'].indexOf(a.status);
            case 'last': return parseTs(a.last_evaluated_at || a.created_at)?.getTime() || 0;
            case 'count': return Number(a.trigger_count ?? 1);
            default: return parseTs(a.created_at)?.getTime() || 0;
        }
    },

    // Rows in display order: incident parents sorted, children directly after.
    ordered() {
        const f = AppState.filters.alerts;
        const groups = AlertManager.byIncident(this.filtered());
        const parents = Array.from(groups.values()).map(m => m[0]);
        const sign = f.dir === 'desc' ? -1 : 1;
        parents.sort((x, y) => {
            const a = this._sortKey(x, f.sort), b = this._sortKey(y, f.sort);
            return a < b ? -sign : a > b ? sign : 0;
        });
        const rows = [];
        for (const p of parents) {
            const members = groups.get(String(p.incident_id || p.alert_id));
            rows.push({ alert: p, children: members.length - 1, child: null });
            members.slice(1).forEach(c => rows.push({ alert: c, children: 0, child: String(p.incident_id || p.alert_id) }));
        }
        return rows;
    },

    render() {
        const f = AppState.filters.alerts;
        const all = AppState.alerts;
        const today = new Date(); today.setHours(0, 0, 0, 0);
        const closedToday = all.filter(a => a.status === 'closed' && (parseTs(a.closed_at || a.created_at)?.getTime() || 0) >= today.getTime()).length;
        const cnt = s => all.filter(a => a.status === s).length;
        const sum = document.getElementById('alertsSum');
        if (sum) sum.innerHTML = `<span><b class="${cnt('active') ? 'crit' : ''}">${cnt('active')}</b> active</span><span class="sep">·</span><span><b>${cnt('acknowledged')}</b> acknowledged</span><span class="sep">·</span><span><b>${cnt('ignored')}</b> ignored</span><span class="sep">·</span><span><b>${closedToday}</b> closed today</span>`;

        const rows = this.ordered();
        const filtersOn = f.search.trim() || f.severity !== 'all' || f.status !== 'open';
        const count = document.getElementById('alertsCount');
        if (count) count.textContent = filtersOn ? `${rows.filter(r => !r.child).length} of ${all.length} match` : `${rows.filter(r => !r.child).length} open`;

        const [start, end] = Pager.render(document.getElementById('alertsPager'), {
            total: rows.length, page: f.page, pageSize: f.pageSize, noun: 'alerts',
            onPage: d => { f.page += d; this.render(); },
            onSize: s => { f.pageSize = s; f.page = 1; this.render(); },
        });
        const slice = rows.slice(start, end);
        this._visible = slice.map(r => r.alert);
        const body = document.getElementById('alertsBody');
        if (body) {
            body.innerHTML = slice.length
                ? slice.map(r => this._rowHtml(r)).join('')
                : `<tr><td colspan="9" class="empty">${filtersOn ? 'No alerts match these filters.' : 'No open alerts. Closed and ignored alerts are under Everything.'}</td></tr>`;
        }
        document.querySelectorAll('#alertsTable th.sort').forEach(th => {
            th.classList.toggle('on', th.dataset.sort === f.sort);
            th.classList.toggle('asc', th.dataset.sort === f.sort && f.dir === 'asc');
        });
        const sa = document.getElementById('alertsSelectAll');
        if (sa) {
            const ids = this._visible.map(a => String(a.alert_id));
            const n = ids.filter(id => this._checked.has(id)).length;
            sa.className = `cb${n && n === ids.length ? ' on' : n ? ' some' : ''}`;
        }
        this._renderBulk();
        if (this._sel && !AlertManager.byId(this._sel)) this._sel = null;
        this._renderDetail();
    },

    _rowHtml({ alert: a, children, child }) {
        const id = escapeHtml(String(a.alert_id));
        const d = AlertManager.describe(a);
        const sub = `${escapeHtml(a.source_host || 'any host')} · ${escapeHtml(a.metric_source)}/${escapeHtml(a.metric_name)}`;
        const inc = children ? `<button type="button" class="inc" data-inc="${id}">+${children} related</button>` : '';
        const childAttr = child ? ` data-child="${escapeHtml(child)}" hidden` : '';
        const cls = [child ? 'child' : '', this._sel === String(a.alert_id) ? 'sel' : '', (a.status === 'closed' || a.status === 'ignored') ? 'off' : '', 'pick'].filter(Boolean).join(' ');
        let acts = '';
        if (a.status === 'active') acts = ibtn('check', 'Acknowledge', 'ok', 'data-act="ack"') + ibtn('snooze', 'Ignore for…', 'warnh', 'data-act="ignore"') + ibtn('x', 'Close', 'crith', 'data-act="close"');
        else if (a.status === 'acknowledged') acts = ibtn('x', 'Close', 'crith', 'data-act="close"');
        else if (a.status === 'ignored') acts = ibtn('bell', 'End the ignore window', 'pri', 'data-act="resume"') + ibtn('x', 'Close', 'crith', 'data-act="close"');
        return `<tr class="${cls}" data-id="${id}"${childAttr}>
            <td class="c-sel"><span class="cb${this._checked.has(String(a.alert_id)) ? ' on' : ''}" role="checkbox"></span></td>
            <td>${sevHtml(a.severity)}</td>
            <td class="n">${inc}<span class="rname" title="Open the rule">${escapeHtml(a.rule_name || 'Alert')}</span><span class="sub">${sub}</span></td>
            <td class="msg" title="${escapeHtml(AlertManager.cleanMessage(a))}">${d.sentence}</td>
            <td>${statusPill(a.status)}</td>
            <td>${escapeHtml(fmtWhen(a.created_at))}</td>
            <td class="c-last">${escapeHtml(fmtWhen(a.last_evaluated_at || a.created_at))}</td>
            <td class="c-cnt r">${escapeHtml(String(a.trigger_count ?? 1))}</td>
            <td><div class="act">${acts}</div></td>
        </tr>`;
    },

    _renderBulk() {
        const bar = document.getElementById('alertsBulk');
        const n = this._checked.size;
        if (!bar) return;
        bar.hidden = n === 0;
        const c = document.getElementById('bulkCount');
        if (c) c.textContent = `${n} selected`;
    },

    // ── selection + drawer ───────────────────────────────────────────
    select(id) {
        this._sel = id ? String(id) : null;
        document.querySelectorAll('#alertsBody tr[data-id]').forEach(tr => tr.classList.toggle('sel', tr.dataset.id === this._sel));
        this._renderDetail();
    },

    step(dir) {
        const ids = this._visible.map(a => String(a.alert_id));
        const i = ids.indexOf(this._sel);
        const next = ids[i + dir];
        if (next) this.select(next);
    },

    // From the console band / toasts: switch to Alerts and open this alert.
    openFor(id) {
        const f = AppState.filters.alerts;
        const a = AlertManager.byId(id);
        if (a && !(a.status === 'active' || a.status === 'acknowledged') && f.status === 'open') {
            f.status = 'all';
            f.auto = true;
            const sel = document.getElementById('alertStatus');
            if (sel) sel.value = 'all';
        }
        f.viaOpen = true;
        f.search = '';
        const box = document.getElementById('alertSearch');
        if (box) box.value = '';
        this._sel = String(id);
        if (AppState.currentTab !== 'alerts') TabManager.switchTab('alerts');
        else this.render();
        document.querySelector(`#alertsBody tr[data-id="${CSS.escape(String(id))}"]`)?.scrollIntoView({ block: 'nearest' });
    },

    _renderDetail() {
        const split = document.getElementById('alertsSplit');
        const det = document.getElementById('alertDetail');
        const a = this._sel ? AlertManager.byId(this._sel) : null;
        if (!split || !det) return;
        split.classList.toggle('detail', !!a);
        if (!a) { det.innerHTML = ''; return; }
        const d = AlertManager.describe(a);
        const rule = AlertManager.ruleOf(a);
        const members = (AlertManager.byIncident(AppState.alerts).get(String(a.incident_id || a.alert_id)) || [a]);
        const first = members.reduce((m, x) => (parseTs(x.created_at)?.getTime() || 0) < (parseTs(m.created_at)?.getTime() || 0) ? x : m, members[0]);
        const firedAgo = fmtAgo(a.created_at);
        const cycles = (a.trigger_count ?? 1) - 1;
        const stillOn = a.status === 'active' || a.status === 'acknowledged';
        const sentence = `${d.sentence}. Triggered ${escapeHtml(firedAgo)}${cycles > 0 ? ` and has re-triggered on ${cycles} evaluation cycle${cycles === 1 ? '' : 's'} since` : ''}.`;

        const METRIC_LINK = 'type="button" data-act="metric" data-tip="Open in Metrics"';
        const maxV = Math.max(Number(a.current_value) || 0, Number(a.threshold_value) || 0) * 1.15 || 1;
        const pct = Math.min(100, Math.max(0, (Number(a.current_value) || 0) / maxV * 100));
        const tpct = Math.min(100, Math.max(0, (Number(a.threshold_value) || 0) / maxV * 100));
        const gauge = a.current_value != null && a.threshold_value != null
            ? `<div class="sec"><span class="microlbl">Reading</span><button class="gauge" ${METRIC_LINK}><span>0</span><span class="bar"><i class="${d.over ? (a.severity === 'critical' ? 'crit' : '') : 'ok'}" style="width:${pct.toFixed(1)}%"></i><s style="left:${tpct.toFixed(1)}%"></s></span><span><b>${escapeHtml(d.value)}</b> · limit ${escapeHtml(d.limit)}</span></button></div>`
            : '';

        const facts = [
            ['Metric', `<button class="mlink" ${METRIC_LINK}>${escapeHtml(a.metric_source)}/${escapeHtml(a.metric_name)}</button>`],
            ['Host', escapeHtml(a.source_host || 'any host')],
            ['Triggered', `${escapeHtml(fmtWhen(a.created_at, true))} <span class="t">· ${escapeHtml(firedAgo)}</span>`],
            ['Last seen', escapeHtml(fmtWhen(a.last_evaluated_at || a.created_at, true))],
            ['Count', `${escapeHtml(String(a.trigger_count ?? 1))} cycle${(a.trigger_count ?? 1) === 1 ? '' : 's'}`],
            d.detector ? ['Detector', escapeHtml(d.detector)] : null,
            rule ? ['Auto-resolve', rule.auto_resolve_cycles > 0 ? `after ${rule.auto_resolve_cycles} clean cycle${rule.auto_resolve_cycles === 1 ? '' : 's'}` : 'manual close only'] : null,
            ['Alert id', `${escapeHtml(String(a.alert_id).slice(0, 8))}…`],
        ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

        const memHtml = members.length > 1 ? `<div class="sec"><span class="microlbl">Incident · ${members.length} alerts</span><div class="mem">${members.map(m => {
            const dt = (parseTs(m.created_at)?.getTime() || 0) - (parseTs(first.created_at)?.getTime() || 0);
            const rel = String(m.alert_id) === String(a.alert_id) ? 'this alert' : (dt > 0 ? `+${fmtDur(dt / 1000)}` : 'first');
            return `<div class="r" data-act="member" data-id="${escapeHtml(String(m.alert_id))}">${sevHtml(m.severity, false)}<b>${escapeHtml(m.rule_name || 'Alert')}</b><span class="t">${escapeHtml(fmtTime(m.created_at))} · ${escapeHtml(rel)}</span></div>`;
        }).join('')}</div></div>` : '';

        const tl = [];
        tl.push({ cls: a.severity, html: `<b>Triggered</b> at ${escapeHtml(d.value)}`, t: fmtWhen(a.created_at, true), ts: parseTs(a.created_at) });
        members.filter(m => String(m.alert_id) !== String(a.alert_id)).forEach(m => {
            const before = (parseTs(m.created_at)?.getTime() || 0) < (parseTs(a.created_at)?.getTime() || 0);
            tl.push({ cls: m.severity, html: before ? `Joined <b>${escapeHtml(m.rule_name || 'Alert')}</b>'s incident` : `<b>${escapeHtml(m.rule_name || 'Alert')}</b> joined the incident`, t: fmtWhen(m.created_at, true), ts: parseTs(m.created_at) });
        });
        if (a.acknowledged_at) tl.push({ cls: 'info', html: `<b>Acknowledged</b>${a.acknowledged_by ? ` by ${escapeHtml(a.acknowledged_by)}` : ''}`, t: fmtWhen(a.acknowledged_at, true), ts: parseTs(a.acknowledged_at) });
        if (a.status === 'ignored' && a.ignored_until) tl.push({ cls: '', html: `<b>Ignored</b> until ${escapeHtml(fmtWhen(a.ignored_until))}`, t: '', ts: parseTs(a.ignored_until) });
        if (a.status === 'closed') {
            const why = a.resolution_reason === 'auto' ? `auto${a.resolved_value != null ? ` @ ${escapeHtml(fmtVal(a.resolved_value, d.unit))}` : ''}` : a.resolution_reason === 'manual' ? `by ${escapeHtml(a.acknowledged_by || 'operator')}` : 'cleared';
            tl.push({ cls: 'ok', html: `<b>Closed</b> · ${why}`, t: fmtWhen(a.closed_at, true), ts: parseTs(a.closed_at) });
        } else if (stillOn) {
            tl.push({ cls: '', html: `<b>Still triggered</b> · ${escapeHtml(d.value)}`, t: fmtWhen(a.last_evaluated_at || a.created_at, true), ts: parseTs(a.last_evaluated_at || a.created_at) });
        }
        const tlHtml = (extra) => `<div class="sec"><span class="microlbl">Timeline</span><ul class="tl">${[...tl, ...extra].sort((x, y) => (x.ts?.getTime() || 0) - (y.ts?.getTime() || 0)).map(i => `<li class="${escapeHtml(i.cls || '')}">${i.html}${i.t ? `<span class="t">${escapeHtml(i.t)}</span>` : ''}</li>`).join('')}</ul></div>`;

        let foot = '';
        if (a.status === 'active') foot = `<button type="button" class="mcbtn mcbtn-pri mcbtn-sm" data-act="ack">Acknowledge</button><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="ignore">Ignore for…</button><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" data-act="close">Close</button>`;
        else if (a.status === 'acknowledged') foot = `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" data-act="close">Close</button>`;
        else if (a.status === 'ignored') foot = `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="resume">End ignore window</button><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm warn" data-act="close">Close</button>`;
        else foot = `<span class="none">closed · no actions</span>`;
        if (a.rule_id) foot += `<button type="button" class="lnk" data-act="openrule">Open rule</button>`;

        det.innerHTML = `<div class="dh">${sevHtml(a.severity)}${hostHtml(a.source_host)}<div class="nav">${ibtn('left', 'Previous alert', '', 'data-act="prev"')}${ibtn('right', 'Next alert', '', 'data-act="next"')}${ibtn('x', 'Close panel', '', 'data-act="closepanel"')}</div></div>
            <div class="db">
                <div><div class="title">${escapeHtml(a.rule_name || 'Alert')}</div><div class="msg">${sentence}</div></div>
                ${gauge}
                <div class="sec"><span class="microlbl">Facts</span><dl class="kv">${facts}</dl></div>
                ${memHtml}
                <div id="alertTimeline">${tlHtml([])}</div>
            </div>
            <div class="df">${foot}</div>`;

        const selAt = this._sel;
        AlertManager.deliveriesFor(a.alert_id).then(rows => {
            if (this._sel !== selAt) return;
            const box = document.getElementById('alertTimeline');
            if (!box) return;
            const byType = new Map();
            rows.forEach(r => {
                const k = String(r.channel_type || '').toLowerCase();
                if (!byType.has(k)) byType.set(k, { ok: 0, fail: 0, ts: parseTs(r.delivered_at) });
                const e = byType.get(k);
                if (r.success) e.ok++; else e.fail++;
            });
            const extra = [];
            if (byType.size) {
                const parts = Array.from(byType.entries()).map(([k, e]) => {
                    const label = CHANNEL_META[k]?.code === 'toast' ? 'Toast' : (k.charAt(0).toUpperCase() + k.slice(1));
                    return `<b>${escapeHtml(label)}</b> ${e.fail && !e.ok ? 'failed' : e.fail ? `sent · ${e.fail} failed` : (k === 'toast' ? 'shown' : 'sent')}`;
                });
                const ts = rows.map(r => parseTs(r.delivered_at)).filter(Boolean).sort((x, y) => x - y)[0];
                extra.push({ cls: 'ok', html: parts.join(' · '), t: fmtWhen(ts, true), ts });
            }
            box.innerHTML = tlHtml(extra);
        });
    },

    async exportCsv() {
        try {
            const blob = await ApiClient.alerts.exportAlerts('csv');
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url; link.download = 'alerts.csv';
            document.body.appendChild(link); link.click(); link.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            ToastManager.show(`Export failed: ${e?.message || 'unknown'}`, 'error');
        }
    },
};
