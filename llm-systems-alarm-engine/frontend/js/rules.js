// Rules view: ledger, filters, triggered state and the rule editor.

const RuleManager = {
    _unitOptions: ['%', '°C', 'W', 'ms', 'count', 'tps', 'Mbps', 'bytes', 'sessions'],

    get rules() { return AppState.rules; },

    byId(id) { return AppState.rules.find(r => String(r.rule_id) === String(id)) || null; },

    init() {
        const f = AppState.filters.rules;
        const search = document.getElementById('ruleSearch');
        search?.addEventListener('input', () => { f.search = search.value; f.page = 1; this.render(); });
        document.getElementById('ruleType')?.addEventListener('change', (e) => { f.type = e.target.value; f.page = 1; this.render(); });
        document.getElementById('ruleHost')?.addEventListener('change', (e) => { f.host = e.target.value; f.page = 1; this.render(); });
        UI.seg(document.getElementById('ruleState'), v => { f.state = v; f.page = 1; this.render(); });
        document.querySelectorAll('#rulesTable th.sort').forEach(th => th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (f.sort === key) f.dir = f.dir === 'asc' ? 'desc' : 'asc';
            else { f.sort = key; f.dir = key === 'last' ? 'desc' : 'asc'; }
            this.render();
        }));
        const menu = document.getElementById('newRuleMenu');
        if (menu) {
            menu.innerHTML = [
                { act: 'threshold_above', glyph: '↑', label: 'Threshold above' },
                { act: 'threshold_below', glyph: '↓', label: 'Threshold below' },
                { act: 'threshold_range', glyph: '↕', label: 'Threshold range' },
                'hr',
                { act: 'z_score', glyph: 'σ', label: 'Z-score anomaly' },
                { act: 'moving_average', glyph: '~', label: 'Moving average' },
                { act: 'percentile', glyph: 'P', label: 'Percentile baseline' },
                { act: 'rate_of_change', glyph: 'Δ', label: 'Rate of change' },
            ].map(i => i === 'hr' ? '<hr>' : `<button type="button" data-act="${i.act}"><span class="mi">${i.glyph}</span>${i.label}</button>`).join('');
            menu.addEventListener('click', (e) => {
                const b = e.target.closest('[data-act]');
                if (b) { UI.closeMenus(); this.create(b.dataset.act); }
            });
        }
        document.getElementById('rulesBody')?.addEventListener('click', (e) => this._onClick(e));
    },

    async _onClick(e) {
        const tr = e.target.closest('tr[data-id]');
        if (!tr) return;
        const id = tr.dataset.id;
        const rule = this.byId(id);
        if (e.target.closest('.mc-toggle')) { e.stopPropagation(); this.toggle(id, !rule?.enabled); return; }
        if (e.target.closest('.rname')) { this.edit(id); return; }
        const act = e.target.closest('[data-act]');
        if (!act) return;
        UI.closeMenus();
        switch (act.dataset.act) {
            case 'edit': this.edit(id); break;
            case 'copy': this.copy(id); break;
            case 'quiet': this.edit(id, { focus: 'quiet' }); break;
            case 'metric': if (rule) MetricsView.showFor(rule.source_host || '', rule.metric_source, rule.metric_name); break;
            case 'alerts': if (rule) { AppState.filters.alerts.search = rule.name; AppState.filters.alerts.status = 'all'; const box = document.getElementById('alertSearch'); if (box) box.value = rule.name; const sel = document.getElementById('alertStatus'); if (sel) sel.value = 'all'; TabManager.switchTab('alerts'); } break;
            case 'delete': this.delete(id); break;
        }
    },

    async load() {
        UIStates.setLoading('rules', true);
        try {
            const rules = await ApiClient.rules.list();
            AppState.rules = Array.isArray(rules) ? rules : [];
            if (AppState.currentTab === 'metrics') ChartManager.refreshAnnotations();
        } finally {
            UIStates.setLoading('rules', false);
        }
    },

    async loadHealth() {
        try { AppState.health = await ApiClient.health(); } catch (_) { AppState.health = null; }
    },

    firing(ruleId) {
        return AppState.alerts.filter(a => a.status === 'active' && String(a.rule_id) === String(ruleId));
    },

    // last_alert_at from the rule, else the newest loaded alert for it.
    lastFired(rule) {
        if (rule.last_alert_at) return parseTs(rule.last_alert_at);
        const a = AppState.alerts.find(x => String(x.rule_id) === String(rule.rule_id));
        return a ? parseTs(a.created_at) : null;
    },

    stateOf(rule) {
        if (!rule.enabled) return { key: 'off', count: 0 };
        const n = this.firing(rule.rule_id).length;
        return n ? { key: 'firing', count: n } : { key: 'quiet', count: 0 };
    },

    conditionHtml(rule) {
        const cfg = rule.config || {};
        const t = cfg.threshold || {};
        const u = t.unit ? ` <span class="u">${escapeHtml(t.unit)}</span>` : '';
        const win = (m) => `<span class="u">· ${escapeHtml(fmtDur((m || 0) * 60))}</span>`;
        switch (rule.rule_type) {
            case 'threshold_above': { const v = t.upper ?? t.value; return v != null ? `&gt; ${escapeHtml(fmtNum(v))}${u}` : '—'; }
            case 'threshold_below': { const v = t.lower ?? t.value; return v != null ? `&lt; ${escapeHtml(fmtNum(v))}${u}` : '—'; }
            case 'threshold_range': return (t.lower != null && t.upper != null) ? `${escapeHtml(fmtNum(t.lower))}–${escapeHtml(fmtNum(t.upper))}${u}` : '—';
            case 'z_score': { const c = cfg.z_score || {}; return c.threshold != null ? `z &gt; ${escapeHtml(fmtNum(c.threshold))} ${win(c.window_minutes)}` : '—'; }
            case 'moving_average': { const c = cfg.moving_average || {}; return c.deviation_factor != null ? `±${escapeHtml(fmtNum(c.deviation_factor))}σ ${win(c.window_minutes)}` : '—'; }
            case 'percentile': { const c = cfg.percentile || {}; return c.percentile != null ? `p${escapeHtml(fmtNum(c.percentile))} ${win(c.window_minutes)}` : '—'; }
            case 'rate_of_change': { const c = cfg.rate_of_change || {}; return c.max_change_per_minute != null ? `±${escapeHtml(fmtNum(c.max_change_per_minute))} <span class="u">/min</span> ${win(c.window_minutes)}` : '—'; }
            default: return '—';
        }
    },

    _filtered() {
        const f = AppState.filters.rules;
        const q = f.search.trim().toLowerCase();
        return AppState.rules.filter(r => {
            if (f.type !== 'all' && r.rule_type !== f.type) return false;
            if (f.host && (r.source_host || '') !== f.host) return false;
            const st = this.stateOf(r).key;
            if (f.state === 'firing' && st !== 'firing') return false;
            if (f.state === 'on' && !r.enabled) return false;
            if (f.state === 'off' && r.enabled) return false;
            if (q) {
                const hay = [r.name, r.description, r.metric_source, r.metric_name, r.source_host, r.rule_type, r.severity].filter(Boolean).join(' ').toLowerCase();
                if (!hay.includes(q)) return false;
            }
            return true;
        });
    },

    _sortKey(r, key) {
        switch (key) {
            case 'severity': return SEVERITY_RANK[r.severity] ?? 9;
            case 'state': { const s = this.stateOf(r); return s.key === 'firing' ? -s.count : s.key === 'quiet' ? 1 : 2; }
            case 'last': return this.lastFired(r)?.getTime() || 0;
            default: return (r.name || '').toLowerCase();
        }
    },

    render() {
        const f = AppState.filters.rules;
        const total = AppState.rules.length;
        const firing = AppState.rules.filter(r => this.stateOf(r).key === 'firing').length;
        const on = AppState.rules.filter(r => r.enabled).length;
        const anomaly = AppState.rules.filter(r => ANOMALY_TYPES.has(r.rule_type)).length;
        const h = AppState.health;
        const cadence = h && h.evaluation_interval_s ? `<span class="sep">·</span><span>evaluated every <b>${escapeHtml(fmtNum(h.evaluation_interval_s, 0))} s</b>${h.components?.rule_eval_last_cycle_ms ? ` · last cycle <b>${escapeHtml(fmtNum(h.components.rule_eval_last_cycle_ms, 0))} ms</b>` : ''}</span>` : '';
        const sum = document.getElementById('rulesSum');
        if (sum) sum.innerHTML = `<span><b>${total}</b> rule${total === 1 ? '' : 's'}</span><span class="sep">·</span><span><b>${on}</b> on</span><span class="sep">·</span><span><b class="${firing ? 'crit' : ''}">${firing}</b> triggered</span><span class="sep">·</span><span><b>${anomaly}</b> anomaly detector${anomaly === 1 ? '' : 's'}</span>${cadence}`;

        const hostSel = document.getElementById('ruleHost');
        if (hostSel) {
            const hosts = [...new Set([...MetricsManager.hosts(), ...AppState.rules.map(r => r.source_host).filter(Boolean)])].sort();
            hostSel.innerHTML = '<option value="">Any host</option>' + hosts.map(x => `<option value="${escapeHtml(x)}"${x === f.host ? ' selected' : ''}>${escapeHtml(x)}</option>`).join('');
        }

        const list = this._filtered();
        const sign = f.dir === 'desc' ? -1 : 1;
        list.sort((x, y) => { const a = this._sortKey(x, f.sort), b = this._sortKey(y, f.sort); return a < b ? -sign : a > b ? sign : 0; });
        const filtersOn = f.search.trim() || f.type !== 'all' || f.state !== 'all' || f.host;
        const cnt = document.getElementById('rulesCount');
        if (cnt) cnt.textContent = filtersOn ? `${list.length} of ${total} match` : `${total} rule${total === 1 ? '' : 's'}`;
        const [start, end] = Pager.render(document.getElementById('rulesPager'), {
            total: list.length, page: f.page, pageSize: f.pageSize, noun: 'rules',
            onPage: d => { f.page += d; this.render(); },
            onSize: s => { f.pageSize = s; f.page = 1; this.render(); },
        });
        const body = document.getElementById('rulesBody');
        if (body) {
            body.innerHTML = list.length
                ? list.slice(start, end).map(r => this._rowHtml(r)).join('')
                : `<tr><td colspan="7" class="empty">${filtersOn ? 'No rules match these filters.' : 'No rules yet. Use New rule to watch a metric.'}</td></tr>`;
        }
        document.querySelectorAll('#rulesTable th.sort').forEach(th => {
            th.classList.toggle('on', th.dataset.sort === f.sort);
            th.classList.toggle('asc', th.dataset.sort === f.sort && f.dir === 'asc');
        });
    },

    _rowHtml(r) {
        const id = escapeHtml(String(r.rule_id));
        const st = this.stateOf(r);
        const quiet = r.quiet_hours_start && r.quiet_hours_end ? ` <span class="q">· quiet ${escapeHtml(r.quiet_hours_start)} – ${escapeHtml(r.quiet_hours_end)}</span>` : '';
        const sub = `${escapeHtml(r.source_host || 'any host')} · ${escapeHtml(r.metric_source)}/${escapeHtml(r.metric_name)}${quiet}`;
        const state = st.key === 'firing'
            ? `<span class="st firing ${escapeHtml(r.severity)}"><span class="dot ${r.severity === 'critical' ? 'crit' : r.severity === 'warning' ? 'warn' : 'info'}"></span>triggered · ${st.count} alert${st.count === 1 ? '' : 's'}</span>`
            : st.key === 'quiet' ? '<span class="st quiet"><span class="dot"></span>quiet</span>' : '<span class="st off"><span class="dot"></span>off</span>';
        const pill = { critical: 'crit', warning: 'warn', info: 'info' }[r.severity] || 'dim';
        const menu = menuHtml([
            { act: 'quiet', glyph: '◔', label: 'Quiet hours…' },
            { act: 'metric', glyph: '↗', label: 'View metric' },
            { act: 'alerts', glyph: '≡', label: 'Open alerts for this rule' },
            'hr',
            { act: 'delete', glyph: '×', label: 'Delete rule', danger: true },
        ]);
        return `<tr class="${r.enabled ? '' : 'off'}" data-id="${id}">
            <td class="c-sel">${toggleHtml(!!r.enabled, '', 'data-tip="' + (r.enabled ? 'Turn off' : 'Turn on') + '"')}</td>
            <td class="n"><span class="rname" title="Edit rule">${escapeHtml(r.name)}</span><span class="sub">${sub}${r.description ? ` · ${escapeHtml(r.description)}` : ''}</span></td>
            <td class="cond">${this.conditionHtml(r)}</td>
            <td><span class="pill ${pill}">${escapeHtml(r.severity)}</span></td>
            <td>${state}</td>
            <td class="t">${escapeHtml(this.lastFired(r) ? fmtWhen(this.lastFired(r)) : 'never')}</td>
            <td><div class="act">${ibtn('edit', 'Edit', '', 'data-act="edit"')}${ibtn('copy', 'Duplicate', '', 'data-act="copy"')}${kebabBtn()}${menu}</div></td>
        </tr>`;
    },

    // ── actions ───────────────────────────────────────────────────────
    async toggle(id, on) {
        const rule = this.byId(id);
        if (!rule) return;
        rule.enabled = on;
        this.render();
        try {
            await ApiClient.rules.toggle(id);
            ToastManager.show(`Rule ${on ? 'turned on' : 'turned off'}`, 'success');
            await this.load();
        } catch (e) {
            rule.enabled = !on;
            ToastManager.show(`Could not change the rule: ${e?.message || 'request failed'}`, 'error');
        }
        this.render();
    },

    async edit(id, opts = {}) {
        let rule;
        try { rule = await ApiClient.rules.get(id); }
        catch (e) { ToastManager.show('Could not load the rule (it may have been deleted)', 'error'); return; }
        this.openEditor({ rule, mode: 'edit', focus: opts.focus });
    },

    async copy(id) {
        let rule;
        try { rule = await ApiClient.rules.get(id); }
        catch (e) { ToastManager.show('Could not load the rule', 'error'); return; }
        const draft = { ...rule, name: `${rule.name || 'Rule'} (copy)` };
        delete draft.rule_id; delete draft.created_at; delete draft.updated_at; delete draft.last_alert_at; delete draft.last_evaluated_at;
        this.openEditor({ rule: draft, mode: 'copy' });
    },

    create(type) {
        this.openEditor({ rule: { rule_type: type || 'threshold_above', severity: 'warning', enabled: true, auto_resolve_cycles: 2, config: {} }, mode: 'new' });
    },

    async delete(id) {
        const rule = this.byId(id);
        const ok = await ModalManager.confirm({
            title: 'Delete rule',
            message: `Delete "${rule?.name || 'this rule'}"? Its open alerts stay in the ledger; nothing new will trigger.`,
            confirmLabel: 'Delete', danger: true,
        });
        if (!ok) return;
        try {
            await ApiClient.rules.delete(id);
            ToastManager.show('Rule deleted', 'success');
            await this.load();
            this.render();
        } catch (e) {
            ToastManager.show(`Could not delete: ${e?.message || 'request failed'}`, 'error');
        }
    },

    handleRuleUpdate(payload) {
        const id = payload && (payload.rule_id || payload.id);
        const idx = AppState.rules.findIndex(r => String(r.rule_id) === String(id));
        if (idx !== -1) AppState.rules[idx] = { ...AppState.rules[idx], ...payload };
        this.load().then(() => { if (AppState.currentTab === 'rules') this.render(); if (AppState.currentTab === 'console') ConsoleView.render(); });
    },

    handleRuleDelete(payload) {
        const id = payload && (payload.rule_id || payload.id);
        AppState.rules = AppState.rules.filter(r => String(r.rule_id) !== String(id));
        if (AppState.currentTab === 'rules') this.render();
    },

    // ── editor ────────────────────────────────────────────────────────
    openEditor({ rule, mode, focus }) {
        const isEdit = mode === 'edit';
        const fired = isEdit ? AppState.alerts.filter(a => String(a.rule_id) === String(rule.rule_id)).length : 0;
        const meta = isEdit ? `${rule.name} · created ${fmtWhen(rule.created_at)}${fired ? ` · triggered ${fired} time${fired === 1 ? '' : 's'} recently` : ''}` : (mode === 'copy' ? 'copy of an existing rule' : RULE_TYPE_LABELS[rule.rule_type] || '');
        ModalManager.open({
            title: isEdit ? 'Edit rule' : mode === 'copy' ? 'Duplicate rule' : 'New rule',
            meta, bodyHtml: this._formHtml(rule), submitLabel: isEdit ? 'Save rule' : 'Create rule',
            footNote: 'Changes apply on the next evaluation cycle.',
            onOpen: (body) => {
                this._wireForm(body, rule);
                if (focus === 'quiet') body.querySelector('#rf-qh-start')?.focus();
            },
            onSubmit: async () => {
                const body = document.getElementById('modalBody');
                const payload = this._collect(body);
                if (isEdit) await ApiClient.rules.update(rule.rule_id, payload);
                else await ApiClient.rules.create(payload);
                ModalManager.close();
                ToastManager.show(isEdit ? 'Rule saved' : 'Rule created', 'success');
                await this.load();
                if (AppState.currentTab === 'rules') this.render();
                else if (AppState.currentTab === 'console') ConsoleView.render();
                else if (AppState.currentTab === 'metrics') ChartManager.refreshAnnotations();
            },
        });
    },

    _formHtml(rule) {
        const cfg = rule.config || {};
        const t = cfg.threshold || {};
        const v = (x, d) => escapeHtml(x != null && x !== '' ? String(x) : (d != null ? String(d) : ''));
        const typeOpts = Object.entries(RULE_TYPE_LABELS).map(([k, l]) => `<option value="${k}"${k === rule.rule_type ? ' selected' : ''}>${l}</option>`).join('');
        const unitOpts = ['<option value="">none</option>'].concat(this._unitOptions.map(u => `<option value="${escapeHtml(u)}"${u === (t.unit || '') ? ' selected' : ''}>${escapeHtml(u)}</option>`));
        if (t.unit && !this._unitOptions.includes(t.unit)) unitOpts.push(`<option value="${escapeHtml(t.unit)}" selected>${escapeHtml(t.unit)}</option>`);
        const sev = rule.severity || 'warning';
        const num = (id, val, extra = '') => `<input class="st-in n" id="${id}" type="number" step="any" value="${val}" ${extra}>`;
        return `<div class="rule-form">
            <div class="st-grid">
                <div class="st-field"><label for="rf-name">Name</label><div class="row"><input class="st-in full" id="rf-name" value="${v(rule.name)}" placeholder="CPU usage warning"></div></div>
                <div class="st-field"><label for="rf-desc">Description</label><div class="row"><input class="st-in full" id="rf-desc" value="${v(rule.description)}" placeholder="Optional, shown under the rule name"></div></div>
            </div>
            <div class="grp"><span class="microlbl">Watch<em>which metric, on which host</em></span>
                <div class="cascade"><select class="sel" id="rf-host" aria-label="Host"></select><select class="sel" id="rf-source" aria-label="Metric source"></select><select class="sel" id="rf-metric" aria-label="Metric"></select></div>
                <div class="st-field" style="margin-top:10px"><div class="help">Any host applies the rule to every agent that reports this metric. Pick a host to scope it.</div></div>
            </div>
            <div class="grp"><span class="microlbl">Trigger when<em id="rf-type-label">${escapeHtml(RULE_TYPE_LABELS[rule.rule_type] || '')}</em></span>
                <div class="st-grid">
                    <div class="st-field"><label for="rf-type">Type</label><div class="row"><select class="sel" id="rf-type">${typeOpts}</select></div></div>
                    <div class="st-field"><label>Severity</label><div class="row"><span class="mc-seg" id="rf-sev"><button type="button" data-v="info"${sev === 'info' ? ' class="on"' : ''}>info</button><button type="button" class="warn${sev === 'warning' ? ' on' : ''}" data-v="warning">warning</button><button type="button" class="crit${sev === 'critical' ? ' on' : ''}" data-v="critical">critical</button></span></div><div class="help">The default severity for alerts this rule creates.</div></div>
                    <div class="cfg" data-cfg="threshold_above"><div class="st-field"><label for="rf-upper">Value goes above</label><div class="row">${num('rf-upper', v(t.upper ?? t.value))}<select class="sel" id="rf-unit">${unitOpts.join('')}</select></div><div class="help">Checked every evaluation cycle against the newest sample.</div></div></div>
                    <div class="cfg" data-cfg="threshold_below"><div class="st-field"><label for="rf-lower">Value drops below</label><div class="row">${num('rf-lower', v(t.lower))}<select class="sel" id="rf-unit-b">${unitOpts.join('')}</select></div></div></div>
                    <div class="cfg" data-cfg="threshold_range"><div class="st-field"><label>Outside the range</label><div class="row">${num('rf-range-lo', v(t.lower))}<span class="unit">to</span>${num('rf-range-hi', v(t.upper))}<select class="sel" id="rf-unit-r">${unitOpts.join('')}</select></div></div></div>
                    <div class="cfg" data-cfg="z_score"><div class="st-field"><label for="rf-z">Z-score above</label><div class="row">${num('rf-z', v(cfg.z_score?.threshold, 3))}</div></div><div class="st-field"><label>Baseline window</label><div class="row">${num('rf-z-win', v(cfg.z_score?.window_minutes, 60), 'min="1"')}<span class="unit">minutes · at least</span>${num('rf-z-min', v(cfg.z_score?.min_data_points, 10), 'min="1"')}<span class="unit">samples</span></div></div></div>
                    <div class="cfg" data-cfg="moving_average"><div class="st-field"><label for="rf-ma">Deviation from the mean</label><div class="row">${num('rf-ma', v(cfg.moving_average?.deviation_factor, 2))}<span class="unit">σ</span></div></div><div class="st-field"><label>Window</label><div class="row">${num('rf-ma-win', v(cfg.moving_average?.window_minutes, 15), 'min="1"')}<span class="unit">minutes · at least</span>${num('rf-ma-min', v(cfg.moving_average?.min_data_points, 5), 'min="1"')}<span class="unit">samples</span></div></div></div>
                    <div class="cfg" data-cfg="percentile"><div class="st-field"><label for="rf-pct">Above the percentile</label><div class="row"><span class="unit">p</span>${num('rf-pct', v(cfg.percentile?.percentile, 95), 'min="50" max="99.9"')}</div></div><div class="st-field"><label>Baseline window</label><div class="row">${num('rf-pct-win', v(cfg.percentile?.window_minutes, 60), 'min="1"')}<span class="unit">minutes · at least</span>${num('rf-pct-min', v(cfg.percentile?.min_data_points, 10), 'min="1"')}<span class="unit">samples</span></div></div></div>
                    <div class="cfg" data-cfg="rate_of_change"><div class="st-field"><label for="rf-roc">Change per minute above</label><div class="row">${num('rf-roc', v(cfg.rate_of_change?.max_change_per_minute, 10))}</div></div><div class="st-field"><label>Window</label><div class="row">${num('rf-roc-win', v(cfg.rate_of_change?.window_minutes, 5), 'min="1"')}<span class="unit">minutes · at least</span>${num('rf-roc-min', v(cfg.rate_of_change?.min_data_points, 2), 'min="2"')}<span class="unit">samples</span></div></div></div>
                    <div class="st-field"><label for="rf-arc">Auto-resolve</label><div class="row"><span>after</span>${num('rf-arc', v(rule.auto_resolve_cycles, 2), 'min="0" step="1" style="width:64px"')}<span class="unit">clean cycles</span></div><div class="help">0 keeps alerts open until someone closes them.</div></div>
                </div>
            </div>
            <div class="grp"><span class="microlbl">Quiet hours and grouping<em>optional</em></span>
                <div class="st-grid">
                    <div class="st-field"><label>Quiet hours</label><div class="row"><input class="st-in qh" id="rf-qh-start" placeholder="HH:MM" value="${v(rule.quiet_hours_start)}"><span class="unit">to</span><input class="st-in qh" id="rf-qh-end" placeholder="HH:MM" value="${v(rule.quiet_hours_end)}"><span class="unit">alarm-engine host time</span></div><div class="help">Empty means no quiet hours. Example: 23:00 to 07:00 keeps the rule silent overnight.</div></div>
                    <div class="st-field"><label for="rf-group">Correlation group</label><div class="row"><input class="st-in w" id="rf-group" value="${v(rule.correlation_group)}" placeholder="e.g. gpu-thermal"></div><div class="help">Alerts from rules sharing a key on the same host join one incident.</div></div>
                </div>
            </div>
            <div class="grp tight">${toggleHtml(rule.enabled !== false, rule.enabled !== false ? 'Enabled' : 'Disabled', 'id="rf-enabled"')}</div>
        </div>`;
    },

    _wireForm(body, rule) {
        const typeSel = body.querySelector('#rf-type');
        const syncType = () => {
            const t = typeSel.value;
            body.querySelectorAll('.cfg').forEach(g => g.classList.toggle('on', g.dataset.cfg === t));
            const lbl = body.querySelector('#rf-type-label');
            if (lbl) lbl.textContent = RULE_TYPE_LABELS[t] || '';
        };
        typeSel.addEventListener('change', syncType);
        syncType();
        UI.seg(body.querySelector('#rf-sev'), () => {});
        UI.bindToggle(body.querySelector('#rf-enabled'), 'Enabled', 'Disabled');
        this._wireCascade(body, rule.source_host, rule.metric_source, rule.metric_name);
    },

    // Host → source → metric selects from the live catalog; saved values that
    // no longer report are kept as "(not currently reporting)" options.
    _wireCascade(body, selHost, selSource, selMetric) {
        const hostSel = body.querySelector('#rf-host');
        const srcSel = body.querySelector('#rf-source');
        const metSel = body.querySelector('#rf-metric');
        const all = () => MetricsManager.visible();
        const byHost = (m, host) => !host || (m.hostname || '') === host;
        const force = (sel, val) => {
            if (!sel || !val) return;
            sel.value = val;
            if (sel.value === val) return;
            const opt = document.createElement('option');
            opt.value = val; opt.textContent = `${val} (not currently reporting)`;
            sel.appendChild(opt); sel.value = val;
        };
        const hosts = MetricsManager.hosts();
        hostSel.innerHTML = '<option value="">Any host</option>' + hosts.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join('');
        const fillSources = (host, pre) => {
            const list = [...new Set(all().filter(m => byHost(m, host)).map(m => m.source))].sort();
            srcSel.innerHTML = '<option value="">Select source…</option>' + list.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
            force(srcSel, pre);
        };
        const fillMetrics = (host, source, pre) => {
            const seen = new Map();
            all().filter(m => m.source === source && byHost(m, host)).forEach(m => { if (!seen.has(m.metric_name)) seen.set(m.metric_name, m); });
            const items = [...seen.values()].sort((a, b) => a.metric_name.localeCompare(b.metric_name));
            metSel.innerHTML = items.length
                ? '<option value="">Select metric…</option>' + items.map(m => `<option value="${escapeHtml(m.metric_name)}">${escapeHtml(m.metric_name)}${m.unit ? ` (${escapeHtml(m.unit)})` : ''}</option>`).join('')
                : '<option value="">No metrics for this source</option>';
            force(metSel, pre);
        };
        hostSel.addEventListener('change', () => { fillSources(hostSel.value); metSel.innerHTML = '<option value="">Select source first…</option>'; });
        srcSel.addEventListener('change', () => fillMetrics(hostSel.value, srcSel.value));
        force(hostSel, selHost);
        fillSources(hostSel.value, selSource);
        if (selSource) fillMetrics(hostSel.value, selSource, selMetric);
        else metSel.innerHTML = '<option value="">Select source first…</option>';
    },

    _collect(body) {
        const val = (id) => (body.querySelector(`#${id}`)?.value ?? '').trim();
        const num = (id) => { const s = val(id); if (s === '') return null; const n = Number(s); return Number.isFinite(n) ? n : null; };
        const mark = (id, bad) => body.querySelector(`#${id}`)?.closest('.st-field')?.classList.toggle('invalid', !!bad);
        const name = val('rf-name');
        const description = val('rf-desc') || null;
        const source_host = val('rf-host') || null;
        const metric_source = val('rf-source');
        const metric_name = val('rf-metric');
        const rule_type = val('rf-type');
        const severity = body.querySelector('#rf-sev button.on')?.dataset.v || 'warning';
        const enabled = body.querySelector('#rf-enabled')?.classList.contains('on') !== false;
        mark('rf-name', !name); mark('rf-source', !metric_source); mark('rf-metric', !metric_name);
        if (!name) throw new Error('Name is required');
        if (!metric_source || !metric_name) throw new Error('Pick a metric source and a metric');
        const config = {};
        if (rule_type === 'threshold_above') {
            const upper = num('rf-upper'); mark('rf-upper', upper == null);
            if (upper == null) throw new Error('Enter the value the metric has to go above');
            config.threshold = { upper, value: upper, unit: val('rf-unit') || null };
        } else if (rule_type === 'threshold_below') {
            const lower = num('rf-lower'); mark('rf-lower', lower == null);
            if (lower == null) throw new Error('Enter the value the metric has to drop below');
            config.threshold = { lower, unit: val('rf-unit-b') || null };
        } else if (rule_type === 'threshold_range') {
            const lo = num('rf-range-lo'), hi = num('rf-range-hi'); mark('rf-range-lo', lo == null || hi == null);
            if (lo == null || hi == null) throw new Error('Enter both ends of the range');
            config.threshold = { lower: lo, upper: hi, value: hi, unit: val('rf-unit-r') || null };
        } else if (rule_type === 'z_score') {
            config.z_score = { threshold: num('rf-z') ?? 3, window_minutes: num('rf-z-win') ?? 60, min_data_points: num('rf-z-min') ?? 10 };
        } else if (rule_type === 'moving_average') {
            config.moving_average = { deviation_factor: num('rf-ma') ?? 2, window_minutes: num('rf-ma-win') ?? 15, min_data_points: num('rf-ma-min') ?? 5 };
        } else if (rule_type === 'percentile') {
            config.percentile = { percentile: num('rf-pct') ?? 95, window_minutes: num('rf-pct-win') ?? 60, min_data_points: num('rf-pct-min') ?? 10 };
        } else if (rule_type === 'rate_of_change') {
            config.rate_of_change = { max_change_per_minute: num('rf-roc') ?? 10, window_minutes: num('rf-roc-win') ?? 5, min_data_points: num('rf-roc-min') ?? 2 };
        }
        if (config.threshold && !config.threshold.unit) delete config.threshold.unit;
        const arc = num('rf-arc');
        const auto_resolve_cycles = arc != null && arc >= 0 ? Math.floor(arc) : 2;
        const qs = val('rf-qh-start'), qe = val('rf-qh-end');
        const hhmm = /^\d{1,2}:\d{2}$/;
        mark('rf-qh-start', (qs && !hhmm.test(qs)) || (qe && !hhmm.test(qe)) || (!!qs !== !!qe));
        if ((qs && !hhmm.test(qs)) || (qe && !hhmm.test(qe))) throw new Error('Quiet hours use HH:MM, for example 23:00');
        if (!!qs !== !!qe) throw new Error('Quiet hours need both a start and an end');
        return {
            name, description, source_host, metric_source, metric_name, rule_type, severity, enabled, config,
            auto_resolve_cycles, correlation_group: val('rf-group') || null,
            quiet_hours_start: qs || null, quiet_hours_end: qe || null,
        };
    },
};
