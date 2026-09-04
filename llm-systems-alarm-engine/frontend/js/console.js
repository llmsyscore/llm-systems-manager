// Console view: summary line, 24-hour band, active alerts, anomalies,
// silenced alerts and the recent-alerts ledger.

const ConsoleView = {
    init() {
        UI.seg(document.getElementById('activeSev'), v => { AppState.filters.console.severity = v; this.renderActive(); });
        document.getElementById('closeAllBtn')?.addEventListener('click', () => AlertManager.closeAll());
        document.getElementById('recentRows')?.addEventListener('click', (e) => {
            const th = e.target.closest('th.sort');
            if (!th) return;
            e.stopImmediatePropagation();
            const f = AppState.filters.console;
            if (f.sort === th.dataset.sort) f.dir = f.dir === 'asc' ? 'desc' : 'asc';
            else { f.sort = th.dataset.sort; f.dir = 'desc'; }
            this.renderRecent();
        });
        const onAct = async (e) => {
            const btn = e.target.closest('[data-act]');
            const row = e.target.closest('[data-id]');
            if (btn && row) {
                e.stopPropagation();
                const id = row.dataset.id;
                if (btn.dataset.act === 'ack') await AlertManager.acknowledge(id);
                else if (btn.dataset.act === 'ignore') await AlertManager.ignore(id);
                else if (btn.dataset.act === 'close') await AlertManager.close(id);
                else if (btn.dataset.act === 'resume') await AlertManager.resume(id);
                else if (btn.dataset.act === 'open') AlertsView.openFor(id);
                else if (btn.dataset.act === 'rule') RuleManager.edit(row.dataset.rule);
                return;
            }
            if (btn && btn.dataset.act === 'ignored') {
                AppState.filters.alerts.status = 'ignored';
                const sel = document.getElementById('alertStatus'); if (sel) sel.value = 'ignored';
                TabManager.switchTab('alerts');
                return;
            }
            const inc = e.target.closest('.inc');
            if (inc) {
                document.querySelectorAll(`#activeRows [data-child="${CSS.escape(inc.dataset.inc)}"]`).forEach(r => { r.hidden = !r.hidden; });
                return;
            }
            const nm = e.target.closest('.nm');
            if (nm && row) AlertsView.openFor(row.dataset.id);
        };
        ['activeRows', 'anomalyRows', 'silencedRows', 'recentRows'].forEach(id => document.getElementById(id)?.addEventListener('click', onAct));
        document.getElementById('band')?.addEventListener('click', (e) => {
            const m = e.target.closest('[data-id]');
            if (m) AlertsView.openFor(m.dataset.id);
        });
    },

    render() {
        this.renderSummary();
        this.renderBand();
        this.renderActive();
        this.renderAnomalies();
        this.renderSilenced();
        this.renderRecent();
    },

    _hosts() {
        return new Set(MetricsManager.visible().map(m => m.hostname).filter(Boolean));
    },

    renderSummary() {
        const active = AlertManager.active();
        const crit = active.filter(a => a.severity === 'critical').length;
        const anomalies = active.filter(a => AlertManager.isAnomaly(a)).length;
        const silenced = AppState.alerts.filter(a => a.status === 'acknowledged' || a.status === 'ignored').length;
        const rulesOn = AppState.rules.filter(r => r.enabled).length;
        const metrics = MetricsManager.visible().length;
        const el = document.getElementById('consoleSum');
        if (el) el.innerHTML = [
            `<span><b class="${active.length ? 'crit' : ''}">${active.length}</b> active</span>`,
            `<span><b class="${crit ? 'crit' : ''}">${crit}</b> critical</span>`,
            `<span><b class="${anomalies ? 'warn' : ''}">${anomalies}</b> anomal${anomalies === 1 ? 'y' : 'ies'}</span>`,
            `<span><b>${silenced}</b> suppressed</span>`,
            `<span><b>${rulesOn}</b> rules on</span>`,
            `<span><b>${metrics}</b> metrics from <b>${this._hosts().size}</b> host${this._hosts().size === 1 ? '' : 's'}</span>`,
        ].join('<span class="sep">·</span>');
        const btn = document.getElementById('closeAllBtn');
        if (btn) btn.disabled = active.length === 0;
    },

    // Today's quiet-hours window for a rule as [startMs, endMs] pairs inside [from, to].
    _quietWindows(rule, from, to) {
        const parse = (s) => { const m = /^(\d{1,2}):(\d{2})$/.exec(s || ''); return m ? [Number(m[1]), Number(m[2])] : null; };
        const a = parse(rule.quiet_hours_start), b = parse(rule.quiet_hours_end);
        if (!a || !b) return [];
        const out = [];
        for (let dayOff = -1; dayOff <= 1; dayOff++) {
            const d = new Date(to); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + dayOff);
            const s = new Date(d); s.setHours(a[0], a[1], 0, 0);
            const e = new Date(d); e.setHours(b[0], b[1], 0, 0);
            if (e <= s) e.setDate(e.getDate() + 1);
            const s0 = Math.max(s.getTime(), from), e0 = Math.min(e.getTime(), to);
            if (e0 > s0) out.push([s0, e0]);
        }
        return out;
    },

    renderBand() {
        const svg = document.getElementById('band');
        const legend = document.getElementById('bandLegend');
        if (!svg) return;
        const W = 1000, base = 40, top = 8;
        const now = Date.now(), from = now - 24 * 3600 * 1000;
        const x = (t) => (W * (t - from) / (now - from));
        let out = '';
        let silencedWindows = 0;
        const sil = [];
        AppState.alerts.forEach(a => {
            if (a.ignored_until) {
                const s = parseTs(a.created_at)?.getTime(), e = parseTs(a.ignored_until)?.getTime();
                if (s && e && e > from && s < now) sil.push([Math.max(s, from), Math.min(e, now)]);
            }
        });
        AppState.rules.filter(r => r.enabled).forEach(r => this._quietWindows(r, from, now).forEach(w => sil.push(w)));
        sil.forEach(([s, e]) => {
            silencedWindows++;
            out += `<rect class="sil" x="${x(s).toFixed(1)}" y="${top}" width="${Math.max(1, x(e) - x(s)).toFixed(1)}" height="${base - top}"/>`;
        });
        const first = new Date(from); first.setMinutes(0, 0, 0); first.setHours(first.getHours() + 1);
        for (let t = first.getTime(); t <= now; t += 3600 * 1000) {
            const h = new Date(t).getHours();
            const major = h % 6 === 0;
            const px = x(t).toFixed(1);
            out += `<line class="tick" x1="${px}" y1="${base}" x2="${px}" y2="${base + (major ? 6 : 3)}"/>`;
            if (major) out += `<text class="hl" x="${(Number(px) + 3).toFixed(1)}" y="${base + 13}">${escapeHtml(fmtTime(new Date(t)).replace(':00', ''))}</text>`;
        }
        out += `<line class="axis" x1="0" y1="${base}" x2="${W}" y2="${base}"/>`;
        const counts = { critical: 0, warning: 0, info: 0 };
        const inWindow = AppState.alerts.filter(a => (parseTs(a.created_at)?.getTime() || 0) >= from);
        inWindow.slice().reverse().forEach(a => {
            const t = parseTs(a.created_at)?.getTime();
            if (!t) return;
            const sev = ['critical', 'warning', 'info'].includes(a.severity) ? a.severity : 'info';
            counts[sev]++;
            const len = { critical: 24, warning: 16, info: 10 }[sev];
            const open = (a.status === 'active' || a.status === 'acknowledged') ? ' open' : '';
            const px = x(t).toFixed(1);
            out += `<line class="m ${sev}${open}" data-id="${escapeHtml(String(a.alert_id))}" x1="${px}" y1="${base - 2}" x2="${px}" y2="${base - 2 - len}"><title>${escapeHtml(sev)} · ${escapeHtml(a.rule_name || 'Alert')}${a.source_host ? ' · ' + escapeHtml(a.source_host) : ''} · ${escapeHtml(fmtWhen(a.created_at))}</title></line>`;
        });
        if (!inWindow.length) out += `<text class="none" x="${W / 2}" y="${base - 12}" text-anchor="middle">no alerts in the last 24 hours</text>`;
        out += `<line class="now" x1="${W}" y1="${top - 2}" x2="${W}" y2="${base}"/><text class="nowl" x="${W - 4}" y="${top + 4}" text-anchor="end">now</text>`;
        svg.innerHTML = out;
        if (legend) legend.innerHTML = `<span class="critical"><b>${counts.critical}</b> critical</span><span class="warning"><b>${counts.warning}</b> warning</span><span class="info"><b>${counts.info}</b> info</span><span><b>${silencedWindows}</b> suppressed window${silencedWindows === 1 ? '' : 's'}</span>`;
    },

    _arow(a, { child = null, children = 0 } = {}) {
        const id = escapeHtml(String(a.alert_id));
        const d = AlertManager.describe(a);
        const cycles = a.trigger_count ?? 1;
        const cyc = cycles > 1 ? ` for ${cycles} cycles` : '';
        const inc = children ? `<button type="button" class="inc" data-inc="${id}">+${children} related</button>` : '';
        const acts = ibtn('check', 'Acknowledge', 'ok', 'data-act="ack"') + ibtn('snooze', 'Ignore for…', 'warnh', 'data-act="ignore"') + ibtn('x', 'Close', 'crith', 'data-act="close"');
        return `<div class="arow ${escapeHtml(a.severity || 'info')}${child ? ' child' : ''}" data-id="${id}"${child ? ` data-child="${escapeHtml(child)}" hidden` : ''}>
            <span class="dot"></span>
            <div class="who">
                <div class="t"><span class="nm" title="Open in the alerts ledger">${escapeHtml(a.rule_name || 'Alert')}</span>${hostHtml(a.source_host)}${inc}</div>
                <div class="m">${d.sentence}${escapeHtml(cyc)}</div>
                <div class="s"><span class="metric">${escapeHtml(a.metric_source)}/<b>${escapeHtml(a.metric_name)}</b></span>${d.detector ? `<span class="metric">${escapeHtml(d.detector)}</span>` : ''}</div>
            </div>
            <div class="when"><b>${escapeHtml(fmtWhen(a.created_at))}</b><br>last seen ${escapeHtml(fmtTime(a.last_evaluated_at || a.created_at))}</div>
            <div class="cnt${cycles >= 5 ? ' hot' : ''}">×${escapeHtml(String(cycles))}</div>
            <div class="act">${acts}</div>
        </div>`;
    },

    renderActive() {
        const el = document.getElementById('activeRows');
        const meta = document.getElementById('activeMeta');
        if (!el) return;
        const sev = AppState.filters.console.severity;
        const active = AlertManager.active().filter(a => sev === 'all' || a.severity === sev);
        const groups = AlertManager.byIncident(active);
        const newest = active[0];
        if (meta) meta.innerHTML = active.length ? `<b>${groups.size}</b> incident${groups.size === 1 ? '' : 's'} · newest <b>${escapeHtml(fmtAgo(newest.created_at))}</b>` : '';
        if (!active.length) {
            el.innerHTML = `<div class="empty">${sev === 'all' ? 'No active alerts. Every rule is quiet.' : `No active ${escapeHtml(sev)} alerts.`}</div>`;
            return;
        }
        const rows = [];
        for (const [key, members] of groups) {
            rows.push(this._arow(members[0], { children: members.length - 1 }));
            members.slice(1).forEach(m => rows.push(this._arow(m, { child: key })));
        }
        el.innerHTML = rows.join('');
    },

    renderAnomalies() {
        const el = document.getElementById('anomalyRows');
        if (!el) return;
        const from = Date.now() - 24 * 3600 * 1000;
        const list = AppState.alerts.filter(a => AlertManager.isAnomaly(a) && (parseTs(a.created_at)?.getTime() || 0) >= from).slice(0, 8);
        if (!list.length) { el.innerHTML = '<div class="empty">No anomalies in the last 24 hours.</div>'; return; }
        el.innerHTML = list.map(a => {
            const d = AlertManager.describe(a);
            const right = a.status === 'closed'
                ? `cleared ${escapeHtml(fmtTime(a.closed_at || a.last_evaluated_at))}${a.resolution_reason === 'manual' ? ' by ' + escapeHtml(a.acknowledged_by || 'operator') : ''}`
                : escapeHtml(a.status);
            return `<div class="krow" data-id="${escapeHtml(String(a.alert_id))}">${sevHtml(a.severity, false)}<div><div class="t"><span class="nm">${escapeHtml(a.rule_name || 'Anomaly')}</span>${hostHtml(a.source_host)}</div><div class="s">${d.detector ? escapeHtml(d.detector) + ' · ' : ''}<b>${escapeHtml(d.value)}</b>${a.threshold_value != null ? ' vs ' + escapeHtml(d.limit) : ''}</div></div><div class="r"><b>${escapeHtml(fmtWhen(a.created_at))}</b><span>${right}</span></div></div>`;
        }).join('');
    },

    renderSilenced() {
        const el = document.getElementById('silencedRows');
        if (!el) return;
        const now = Date.now();
        const rows = [];
        AppState.alerts.filter(a => a.status === 'acknowledged').forEach(a => {
            const still = (parseTs(a.last_evaluated_at)?.getTime() || 0) > (parseTs(a.acknowledged_at)?.getTime() || 0);
            rows.push(`<div class="krow" data-id="${escapeHtml(String(a.alert_id))}">${sevHtml(a.severity, false)}<div><div class="t"><span class="nm">${escapeHtml(a.rule_name || 'Alert')}</span>${hostHtml(a.source_host)}</div><div class="s">acknowledged${a.acknowledged_by ? ' by <b>' + escapeHtml(a.acknowledged_by) + '</b>' : ''} · ${escapeHtml(fmtTime(a.acknowledged_at || a.created_at))}${still ? ' · still over the limit' : ''}</div></div><div class="r"><span class="ra"><b>×${escapeHtml(String(a.trigger_count ?? 1))}</b> since${ibtn('x', 'Close', 'crith', 'data-act="close"')}</span></div></div>`);
        });
        const ignored = AppState.alerts.filter(a => a.status === 'ignored');
        const MAX_IGNORED = 8;
        ignored.slice(0, MAX_IGNORED).forEach(a => {
            const until = parseTs(a.ignored_until);
            const left = until ? (until.getTime() - now) / 1000 : null;
            const win = until ? (until.getTime() - (parseTs(a.created_at)?.getTime() || until.getTime())) / 3600000 : null;
            const what = !until ? `ignored · ${escapeHtml(fmtWhen(a.created_at))}`
                : left > 0 ? `ignored until <b>${escapeHtml(fmtWhen(until))}</b>${win ? ` · ${escapeHtml(fmtNum(Math.round(win)))} h window` : ''}`
                : `window ended <b>${escapeHtml(fmtWhen(until))}</b> · the rule can trigger again`;
            const right = left != null && left > 0 ? `resumes in <b>${escapeHtml(fmtDur(left))}</b>` : '';
            rows.push(`<div class="krow" data-id="${escapeHtml(String(a.alert_id))}">${sevHtml(a.severity, false)}<div><div class="t"><span class="nm">${escapeHtml(a.rule_name || 'Alert')}</span>${hostHtml(a.source_host)}</div><div class="s">${what}</div></div><div class="r"><span class="ra">${right}${ibtn('x', 'Close this ignored alert', 'crith', 'data-act="close"')}</span></div></div>`);
        });
        if (ignored.length > MAX_IGNORED) {
            rows.push(`<div class="krow more"><span></span><div><div class="s">+${ignored.length - MAX_IGNORED} more ignored alert${ignored.length - MAX_IGNORED === 1 ? '' : 's'}</div></div><div class="r"><button type="button" class="lnk" data-act="ignored">Open in Alerts</button></div></div>`);
        }
        AppState.rules.filter(r => r.enabled && r.quiet_hours_start && r.quiet_hours_end).forEach(r => {
            const wins = this._quietWindows(r, now - 86400000, now + 86400000);
            const cur = wins.find(([s, e]) => s <= now && now < e);
            const next = wins.find(([s]) => s > now);
            const right = cur ? `ends in <b>${escapeHtml(fmtDur((cur[1] - now) / 1000))}</b>` : next ? `starts in <b>${escapeHtml(fmtDur((next[0] - now) / 1000))}</b>` : '';
            rows.push(`<div class="krow" data-rule="${escapeHtml(String(r.rule_id))}"><span class="sev quiet"><span class="dot"></span></span><div><div class="t">Quiet hours <span class="host">${escapeHtml(r.name)}</span></div><div class="s">rule silent <b>${escapeHtml(r.quiet_hours_start)} – ${escapeHtml(r.quiet_hours_end)}</b> nightly</div></div><div class="r"><span class="ra">${right}${ibtn('edit', 'Edit rule', '', 'data-act="rule"')}</span></div></div>`);
        });
        el.innerHTML = rows.length ? rows.join('') : '<div class="empty">Nothing suppressed.</div>';
    },

    renderRecent() {
        const el = document.getElementById('recentRows');
        if (!el) return;
        const list = AppState.alerts.slice(0, 25);
        if (!list.length) { el.innerHTML = '<div class="empty">No alerts yet. Rules that trigger will show up here.</div>'; return; }
        const f = AppState.filters.console;
        const key = a => f.sort === 'cleared' ? (parseTs(a.status === 'closed' ? (a.closed_at || a.last_evaluated_at) : a.status === 'ignored' ? a.ignored_until : null)?.getTime() || 0) : (parseTs(a.created_at)?.getTime() || 0);
        const sign = f.dir === 'desc' ? -1 : 1;
        list.sort((x, y) => (key(x) - key(y)) * sign);
        const rows = list.map(a => {
            const d = AlertManager.describe(a);
            let cleared = '—';
            if (a.status === 'closed') {
                const why = a.resolution_reason === 'auto' ? `auto${a.resolved_value != null ? ' @ ' + escapeHtml(fmtVal(a.resolved_value, d.unit)) : ''}` : a.resolution_reason === 'manual' ? `closed by ${escapeHtml(a.acknowledged_by || 'operator')}` : 'cleared';
                cleared = `${escapeHtml(fmtTime(a.closed_at || a.last_evaluated_at))} · <b>${why}</b>`;
            } else if (a.status === 'ignored' && a.ignored_until) {
                cleared = `until ${escapeHtml(fmtTime(a.ignored_until))}`;
            }
            return `<tr class="pick" data-id="${escapeHtml(String(a.alert_id))}"><td>${sevHtml(a.severity)}</td><td class="n"><span class="nm">${escapeHtml(a.rule_name || 'Alert')}</span><span class="sub">${escapeHtml(a.source_host || 'any host')} · ${escapeHtml(a.metric_source)}/${escapeHtml(a.metric_name)}</span></td><td class="msg">${d.sentence}</td><td>${statusPill(a.status)}</td><td>${escapeHtml(fmtWhen(a.created_at))}</td><td class="t">${cleared}</td></tr>`;
        }).join('');
        el.innerHTML = `<table class="tbl"><colgroup><col style="width:104px"><col><col><col style="width:150px"><col style="width:132px"><col style="width:200px"></colgroup><thead><tr><th>Severity</th><th>Rule</th><th>Message</th><th>Status</th><th class="sort${f.sort === 'fired' ? ' on' : ''}${f.sort === 'fired' && f.dir === 'asc' ? ' asc' : ''}" data-sort="fired">Triggered</th><th class="sort${f.sort === 'cleared' ? ' on' : ''}${f.sort === 'cleared' && f.dir === 'asc' ? ' asc' : ''}" data-sort="cleared">Cleared</th></tr></thead><tbody>${rows}</tbody></table>`;
    },
};
