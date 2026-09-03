// Notifications view: channels, policies and delivery history ledgers plus
// the channel and policy editors.

const NotificationsManager = {
    channels: [],
    configs: [],
    deliveries: [],
    _inflight: new Set(),

    init() {
        const menu = document.getElementById('addChannelMenu');
        if (menu) {
            menu.innerHTML = Object.entries(CHANNEL_META).map(([k, m]) => `<button type="button" data-type="${k}"><span class="mi t">${escapeHtml(m.code)}</span>${escapeHtml(m.label)}</button>`).join('');
            menu.addEventListener('click', (e) => {
                const b = e.target.closest('[data-type]');
                if (b) { UI.closeMenus(); this.addChannel(b.dataset.type); }
            });
        }
        document.getElementById('newPolicyBtn')?.addEventListener('click', () => this.newConfig());
        document.getElementById('notifNotices')?.addEventListener('click', (e) => {
            const b = e.target.closest('[data-act="toast-on"]');
            if (b) { const ch = this.channels.find(c => c.channel_type === 'toast'); if (ch) this.toggleChannel(ch.channel_id, true); }
        });
        document.getElementById('channelsBody')?.addEventListener('click', (e) => this._onChannelClick(e));
        document.getElementById('policiesBody')?.addEventListener('click', (e) => this._onPolicyClick(e));
        const f = AppState.filters.deliveries;
        document.getElementById('deliveryType')?.addEventListener('change', async (e) => { f.type = e.target.value; f.page = 1; await this.loadDeliveries(); this.renderDeliveries(); });
        document.getElementById('deliveryResult')?.addEventListener('change', (e) => { f.result = e.target.value; f.page = 1; this.renderDeliveries(); });
    },

    async load() {
        const [channels, configs] = await Promise.all([
            ApiClient.notifications.listChannels(),
            ApiClient.notifications.listConfigs(),
        ]);
        this.channels = Array.isArray(channels) ? channels : [];
        this.configs = Array.isArray(configs) ? configs : [];
        await this.loadDeliveries();
    },

    async loadDeliveries() {
        const f = AppState.filters.deliveries;
        const params = { limit: 100 };
        if (f.type) params.channel_type = f.type;
        try {
            const rows = await ApiClient.notifications.getHistory(params);
            this.deliveries = Array.isArray(rows) ? rows : [];
        } catch (_) {
            this.deliveries = [];
        }
    },

    render() {
        this.renderSummary();
        this.renderNotices();
        this.renderChannels();
        this.renderPolicies();
        this.renderDeliveries();
    },

    chById(id) { return this.channels.find(c => String(c.channel_id) === String(id)) || null; },
    cfgById(id) { return this.configs.find(c => String(c.config_id) === String(id)) || null; },
    typeOf(ch) { return String(ch?.channel_type || '').toLowerCase(); },

    policiesUsing(channelId) {
        return this.configs.filter(c => (c.channels || []).some(id => String(id) === String(channelId)));
    },

    renderSummary() {
        const el = document.getElementById('notifSum');
        if (!el) return;
        const today = new Date(); today.setHours(0, 0, 0, 0);
        const todays = this.deliveries.filter(d => (parseTs(d.delivered_at)?.getTime() || 0) >= today.getTime());
        const failed = todays.filter(d => d.success === false).length;
        const chOn = this.channels.filter(c => c.enabled !== false).length;
        const poOn = this.configs.filter(c => c.enabled !== false).length;
        el.innerHTML = `<span><b>${this.channels.length}</b> channel${this.channels.length === 1 ? '' : 's'} · <b>${chOn}</b> on</span><span class="sep">·</span><span><b>${this.configs.length}</b> polic${this.configs.length === 1 ? 'y' : 'ies'} · <b>${poOn}</b> on</span><span class="sep">·</span><span><b>${todays.length - failed}</b> sent today · <b class="${failed ? 'crit' : 'ok'}">${failed}</b> failed</span>`;
    },

    renderNotices() {
        const el = document.getElementById('notifNotices');
        if (!el) return;
        const toast = this.channels.find(c => this.typeOf(c) === 'toast');
        const routed = toast ? this.policiesUsing(toast.channel_id).some(p => p.enabled !== false) : false;
        let html = '';
        if (toast && toast.enabled === false) {
            html = `<div class="notice info"><b>Browser popups are off.</b><span class="d">Turn on the Toast channel and route a policy to it.</span><div class="gap"></div><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="toast-on">Turn on</button></div>`;
        } else if (toast && !routed) {
            html = `<div class="notice"><b>Popups are on but no policy routes to them.</b><span class="d">Add the Toast channel to an enabled policy.</span></div>`;
        } else if (!toast) {
            html = `<div class="notice info"><b>No browser popups yet.</b><span class="d">Add a Browser popups channel and route a policy to it.</span></div>`;
        }
        el.innerHTML = html;
    },

    _target(ch) {
        const t = this.typeOf(ch);
        const cfg = (ch.config && (ch.config[t] || ch.config)) || {};
        switch (t) {
            case 'toast': return 'every open browser · popups follow this switch';
            case 'email': return `${cfg.to_email || '—'}${cfg.subject_prefix ? ` · subject ${cfg.subject_prefix}` : ''}`;
            case 'sms': return cfg.to_number || '—';
            case 'discord': { try { const u = new URL(cfg.webhook_url); return `${u.host}${u.pathname.split('/').slice(0, 4).join('/')}…`; } catch (_) { return cfg.webhook_url || '—'; } }
            case 'webhook': return `${cfg.method || 'POST'} ${cfg.url || '—'}`;
            case 'webpush': return `companion app${cfg.url ? ` · ${cfg.url}` : ''}`;
            default: return '';
        }
    },

    renderChannels() {
        const body = document.getElementById('channelsBody');
        if (!body) return;
        if (!this.channels.length) {
            body.innerHTML = '<tr><td colspan="7" class="empty">No channels yet. Use Add channel to choose where alerts go.</td></tr>';
            return;
        }
        body.innerHTML = this.channels.map(ch => {
            const t = this.typeOf(ch);
            const meta = CHANNEL_META[t] || { code: t, cls: '' };
            const on = ch.enabled !== false;
            const used = this.policiesUsing(ch.channel_id);
            const chips = used.length
                ? used.map(p => `<span class="ch${p.enabled === false ? ' off' : ''}" title="${p.enabled === false ? 'policy is off' : 'policy is on'}">${escapeHtml(p.name)}</span>`).join('')
                : '<span class="ch off">no policy</span>';
            const sent = Number(ch.send_count || 0), failed = Number(ch.fail_count || 0);
            const menu = menuHtml([
                { act: 'history', glyph: '≡', label: 'Delivery history' },
                'hr',
                { act: 'delete', glyph: '×', label: 'Delete channel', danger: true },
            ]);
            return `<tr class="${on ? '' : 'off'}" data-id="${escapeHtml(String(ch.channel_id))}">
                <td class="c-sel">${toggleHtml(on, '', `data-tip="${on ? 'Turn off' : 'Turn on'}"`)}</td>
                <td><span class="ntype ${escapeHtml(meta.cls)}">${escapeHtml(meta.code)}</span></td>
                <td class="n"><span class="cname">${escapeHtml(ch.name || 'Unnamed')}</span><span class="sub" title="${escapeHtml(this._target(ch))}">${escapeHtml(this._target(ch))}</span></td>
                <td><div class="chips">${chips}</div></td>
                <td class="r"><span class="cnts"><span class="${sent ? '' : 'z'}">${sent}</span> · <span class="${failed ? 'f' : 'z'}">${failed}</span></span></td>
                <td class="t">${escapeHtml(ch.last_sent_at ? fmtWhen(ch.last_sent_at) : 'never')}</td>
                <td><div class="act">${ibtn('play', 'Send a test', 'pri', 'data-act="test"')}${ibtn('edit', 'Edit', '', 'data-act="edit"')}${kebabBtn()}${menu}</div></td>
            </tr>`;
        }).join('');
    },

    _when(p) {
        const parts = [];
        parts.push(p.min_severity ? `severity ≥ <b>${escapeHtml(p.min_severity)}</b>` : 'any severity');
        const n = (l, one, many) => (l && l.length) ? `${l.length} ${l.length === 1 ? one : many}` : null;
        parts.push(n(p.source_hosts, 'host', 'hosts') || 'any host');
        const src = n(p.metric_sources, 'source', 'sources'), met = n(p.metric_names, 'metric', 'metrics');
        if (src) parts.push(src);
        if (met) parts.push(met);
        return parts.join(' · ');
    },

    _cadence(p) {
        const parts = [];
        parts.push(p.min_alarm_count > 1 ? `after ${p.min_alarm_count} breaches` : 'first breach');
        parts.push(p.repeat_interval_minutes > 0 ? `every ${p.repeat_interval_minutes} min` : 'every cycle');
        if (p.notify_on_clear) parts.push('on clear');
        return parts.join(' · ');
    },

    renderPolicies() {
        const body = document.getElementById('policiesBody');
        if (!body) return;
        if (!this.configs.length) {
            body.innerHTML = '<tr><td colspan="7" class="empty">No policies yet. A policy picks which alerts reach which channels.</td></tr>';
            return;
        }
        body.innerHTML = this.configs.map(p => {
            const on = p.enabled !== false;
            const chips = (p.channels || []).map(id => {
                const ch = this.chById(id);
                const t = this.typeOf(ch);
                const meta = CHANNEL_META[t];
                return `<span class="ch${!ch || ch.enabled === false ? ' off' : ''}" title="${escapeHtml(ch ? ch.name : 'missing channel')}">${escapeHtml(meta ? meta.code : (ch ? t : 'missing'))}</span>`;
            }).join('') || '<span class="ch off">no channel</span>';
            const menu = menuHtml([{ act: 'delete', glyph: '×', label: 'Delete policy', danger: true }]);
            const fired = p.trigger_count ? `${p.trigger_count} · ${fmtWhen(p.last_triggered_at)}` : 'never';
            return `<tr class="${on ? '' : 'off'}" data-id="${escapeHtml(String(p.config_id))}">
                <td class="c-sel">${toggleHtml(on, '', `data-tip="${on ? 'Turn off' : 'Turn on'}"`)}</td>
                <td class="n"><span class="pname" title="Edit policy">${escapeHtml(p.name)}</span>${p.description ? `<span class="sub">${escapeHtml(p.description)}</span>` : ''}</td>
                <td><div class="chips">${chips}</div></td>
                <td class="cond">${this._when(p)}</td>
                <td class="t">${escapeHtml(this._cadence(p))}</td>
                <td class="t">${escapeHtml(fired)}</td>
                <td><div class="act">${ibtn('edit', 'Edit', '', 'data-act="edit"')}${ibtn('copy', 'Duplicate', '', 'data-act="copy"')}${kebabBtn()}${menu}</div></td>
            </tr>`;
        }).join('');
    },

    renderDeliveries() {
        const body = document.getElementById('deliveriesBody');
        const f = AppState.filters.deliveries;
        if (!body) return;
        let rows = this.deliveries;
        if (f.result === 'ok') rows = rows.filter(d => d.success !== false);
        if (f.result === 'failed') rows = rows.filter(d => d.success === false);
        const meta = document.getElementById('deliveriesMeta');
        if (meta) meta.innerHTML = `last <b>${this.deliveries.length}</b> deliver${this.deliveries.length === 1 ? 'y' : 'ies'}`;
        const [start, end] = Pager.render(document.getElementById('deliveriesPager'), {
            total: rows.length, page: f.page, pageSize: f.pageSize, sizes: [20, 50, 100], noun: 'deliveries',
            onPage: d => { f.page += d; this.renderDeliveries(); },
            onSize: s => { f.pageSize = s; f.page = 1; this.renderDeliveries(); },
        });
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="5" class="empty">${this.deliveries.length ? 'No deliveries match these filters.' : 'No deliveries yet. Send a test from a channel row to see one here.'}</td></tr>`;
            return;
        }
        body.innerHTML = rows.slice(start, end).map(d => {
            const t = String(d.channel_type || '').toLowerCase();
            const m = CHANNEL_META[t] || { code: t || '—', cls: '' };
            const ok = d.success !== false;
            const detail = d.error_message || d.title || '';
            return `<tr><td>${escapeHtml(fmtWhen(d.delivered_at, true))}</td><td><span class="ntype ${escapeHtml(m.cls)}">${escapeHtml(m.code)}</span></td><td class="t" title="${escapeHtml(d.recipient || '')}">${escapeHtml(d.recipient || '—')}</td><td><span class="pill ${ok ? 'ok' : 'crit'}">${ok ? 'sent' : 'failed'}</span></td><td class="msg" title="${escapeHtml(detail)}">${escapeHtml(detail)}</td></tr>`;
        }).join('');
    },

    // ── row actions ───────────────────────────────────────────────────
    async _onChannelClick(e) {
        const tr = e.target.closest('tr[data-id]');
        if (!tr) return;
        const id = tr.dataset.id;
        const ch = this.chById(id);
        if (e.target.closest('.mc-toggle')) { e.stopPropagation(); this.toggleChannel(id, !(ch?.enabled !== false)); return; }
        if (e.target.closest('.cname')) { this.editChannel(id); return; }
        const act = e.target.closest('[data-act]');
        if (!act) return;
        UI.closeMenus();
        switch (act.dataset.act) {
            case 'test': this.testChannel(id); break;
            case 'edit': this.editChannel(id); break;
            case 'history': {
                const f = AppState.filters.deliveries;
                f.type = this.typeOf(ch); f.page = 1;
                const sel = document.getElementById('deliveryType'); if (sel) sel.value = f.type;
                await this.loadDeliveries(); this.renderDeliveries();
                document.getElementById('deliveriesTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                break;
            }
            case 'delete': this.deleteChannel(id); break;
        }
    },

    _onPolicyClick(e) {
        const tr = e.target.closest('tr[data-id]');
        if (!tr) return;
        const id = tr.dataset.id;
        const p = this.cfgById(id);
        if (e.target.closest('.mc-toggle')) { e.stopPropagation(); this.toggleConfig(id, !(p?.enabled !== false)); return; }
        if (e.target.closest('.pname')) { this.editConfig(id); return; }
        const act = e.target.closest('[data-act]');
        if (!act) return;
        UI.closeMenus();
        switch (act.dataset.act) {
            case 'edit': this.editConfig(id); break;
            case 'copy': this.copyConfig(id); break;
            case 'delete': this.deleteConfig(id); break;
        }
    },

    async _reload() {
        try { await this.load(); } catch (_) {}
        this.render();
    },

    async toggleChannel(id, on) {
        const ch = this.chById(id);
        if (!ch) return;
        ch.enabled = on;
        this.render();
        try {
            await ApiClient.notifications.updateChannel(id, { enabled: on });
            ToastManager.show(`${ch.name} ${on ? 'turned on' : 'turned off'}`, 'success');
        } catch (e) {
            ch.enabled = !on;
            ToastManager.show(`Could not change the channel: ${e?.message || 'request failed'}`, 'error');
        }
        await this._reload();
    },

    async toggleConfig(id, on) {
        const p = this.cfgById(id);
        if (!p) return;
        p.enabled = on;
        this.render();
        try {
            await ApiClient.notifications.updateConfig(id, { enabled: on });
            ToastManager.show(`${p.name} ${on ? 'turned on' : 'turned off'}`, 'success');
        } catch (e) {
            p.enabled = !on;
            ToastManager.show(`Could not change the policy: ${e?.message || 'request failed'}`, 'error');
        }
        await this._reload();
    },

    async testChannel(id) {
        if (this._inflight.has(id)) return;
        this._inflight.add(id);
        const ch = this.chById(id);
        try {
            await ApiClient.notifications.testChannel({ channel_id: id });
            ToastManager.show(`Test sent to ${ch?.name || 'the channel'}`, 'success');
            await this._reload();
        } catch (e) {
            ToastManager.show(`Test failed: ${e?.message || 'request failed'}`, 'error');
        } finally {
            this._inflight.delete(id);
        }
    },

    async deleteChannel(id) {
        const ch = this.chById(id);
        const used = this.policiesUsing(id);
        const ok = await ModalManager.confirm({
            title: 'Delete channel',
            message: `Delete "${ch?.name || 'this channel'}"?${used.length ? ` ${used.length} polic${used.length === 1 ? 'y stops' : 'ies stop'} sending here.` : ''}`,
            confirmLabel: 'Delete', danger: true,
        });
        if (!ok) return;
        try {
            await ApiClient.notifications.deleteChannel(id);
            ToastManager.show('Channel deleted', 'success');
        } catch (e) {
            ToastManager.show(`Could not delete: ${e?.message || 'request failed'}`, 'error');
        }
        await this._reload();
    },

    async deleteConfig(id) {
        const p = this.cfgById(id);
        const ok = await ModalManager.confirm({ title: 'Delete policy', message: `Delete "${p?.name || 'this policy'}"? Alerts it routed stop going out.`, confirmLabel: 'Delete', danger: true });
        if (!ok) return;
        try {
            await ApiClient.notifications.deleteConfig(id);
            ToastManager.show('Policy deleted', 'success');
        } catch (e) {
            ToastManager.show(`Could not delete: ${e?.message || 'request failed'}`, 'error');
        }
        await this._reload();
    },

    // ── channel editor ────────────────────────────────────────────────
    addChannel(type) { this._openChannelEditor({ channel_type: type || 'toast', enabled: true, config: {} }, false); },
    editChannel(id) { const ch = this.chById(id); if (ch) this._openChannelEditor(ch, true); },

    _openChannelEditor(ch, isEdit) {
        ModalManager.open({
            title: isEdit ? 'Edit channel' : 'Add channel',
            meta: isEdit ? `${ch.name} · ${CHANNEL_META[this.typeOf(ch)]?.label || this.typeOf(ch)}` : (CHANNEL_META[ch.channel_type]?.label || ''),
            width: 'narrow', bodyHtml: this._channelFormHtml(ch),
            submitLabel: isEdit ? 'Save channel' : 'Add channel',
            footNote: isEdit ? '' : 'Send a test from the channel row after saving.',
            footActions: isEdit ? [{ label: 'Send a test', onClick: () => this.testChannel(ch.channel_id) }] : [],
            onOpen: (body) => {
                const typeSel = body.querySelector('#cf-type');
                const sync = () => {
                    body.querySelectorAll('[data-ct]').forEach(g => { g.hidden = g.dataset.ct !== typeSel.value; });
                    const meta = document.getElementById('modalMeta');
                    if (meta && !isEdit) meta.textContent = CHANNEL_META[typeSel.value]?.label || '';
                };
                typeSel.addEventListener('change', sync);
                sync();
                UI.bindToggle(body.querySelector('#cf-enabled'), 'Channel enabled', 'Channel disabled');
            },
            onSubmit: async () => {
                const body = document.getElementById('modalBody');
                const payload = this._collectChannel(body);
                if (isEdit) await ApiClient.notifications.updateChannel(ch.channel_id, payload);
                else await ApiClient.notifications.createChannel(payload);
                ModalManager.close();
                ToastManager.show(isEdit ? 'Channel saved' : 'Channel added', 'success');
                await this._reload();
            },
        });
    },

    _channelFormHtml(ch) {
        const t = this.typeOf(ch) || 'toast';
        const cfgAll = ch.config || {};
        const c = (k) => (cfgAll[k] || {});
        const v = (x) => escapeHtml(x != null ? String(x) : '');
        const typeOpts = Object.entries(CHANNEL_META).map(([k, m]) => `<option value="${k}"${k === t ? ' selected' : ''}>${escapeHtml(m.label)}</option>`).join('');
        return `<div class="channel-form">
            <div class="st-field"><label for="cf-type">Type</label><div class="row"><select class="sel" id="cf-type">${typeOpts}</select></div></div>
            <div class="st-field"><label for="cf-name">Name</label><div class="row"><input class="st-in full" id="cf-name" value="${v(ch.name)}" placeholder="Ops channel"></div></div>
            <div data-ct="toast"><div class="st-field"><div class="help">Shows a popup in every open dashboard and console. Which alerts pop is decided by the policies that route here.</div></div></div>
            <div data-ct="email">
                <div class="st-field"><label for="cf-email-to">To</label><div class="row"><input class="st-in full" id="cf-email-to" type="email" value="${v(c('email').to_email)}" placeholder="you@example.com"></div></div>
                <div class="st-field"><label for="cf-email-prefix">Subject prefix</label><div class="row"><input class="st-in w" id="cf-email-prefix" value="${v(c('email').subject_prefix ?? '[ALARM]')}"></div></div>
            </div>
            <div data-ct="sms"><div class="st-field"><label for="cf-sms-to">Phone number</label><div class="row"><input class="st-in w" id="cf-sms-to" value="${v(c('sms').to_number)}" placeholder="+15551234567"></div><div class="help">Sending text messages needs a provider integration; deliveries record as failed until one is configured.</div></div></div>
            <div data-ct="webhook">
                <div class="st-field"><label for="cf-wh-url">Webhook URL</label><div class="row"><input class="st-in full" id="cf-wh-url" type="url" value="${v(c('webhook').url)}" placeholder="https://example.com/hook"></div></div>
                <div class="st-field"><label for="cf-wh-method">Method</label><div class="row"><select class="sel" id="cf-wh-method"><option value="POST"${(c('webhook').method || 'POST') === 'POST' ? ' selected' : ''}>POST</option><option value="PUT"${c('webhook').method === 'PUT' ? ' selected' : ''}>PUT</option></select><input class="st-in w" id="cf-wh-secret" value="${v(c('webhook').secret)}" placeholder="signing secret (optional)"></div></div>
            </div>
            <div data-ct="discord">
                <div class="st-field"><label for="cf-dc-url">Webhook URL</label><div class="row"><input class="st-in full" id="cf-dc-url" type="url" value="${v(c('discord').webhook_url)}" placeholder="https://discord.com/api/webhooks/…"></div><div class="help">Server settings → Integrations → Webhooks. The URL is the secret; it is stored on the alarm engine host only.</div></div>
                <div class="st-field"><label for="cf-dc-user">Post as</label><div class="row"><input class="st-in w" id="cf-dc-user" value="${v(c('discord').username)}" placeholder="LLM Systems"></div><div class="help">Optional bot name shown on each message.</div></div>
            </div>
            <div data-ct="webpush">
                <div class="st-field"><label for="cf-wp-url">Manager notify URL</label><div class="row"><input class="st-in full" id="cf-wp-url" type="url" value="${v(c('webpush').url)}" placeholder="blank = the local manager"></div></div>
                <div class="st-field"><label for="cf-wp-token">Bearer token</label><div class="row"><input class="st-in full" id="cf-wp-token" type="password" value="${v(c('webpush').token)}" placeholder="blank = the shared alarm-engine token"></div><div class="help">Delivers to every device subscribed in the companion app. The manager holds the push keys and does the send.</div></div>
            </div>
            <div class="grp tight">${toggleHtml(ch.enabled !== false, ch.enabled !== false ? 'Channel enabled' : 'Channel disabled', 'id="cf-enabled"')}</div>
        </div>`;
    },

    _collectChannel(body) {
        const val = (id) => (body.querySelector(`#${id}`)?.value ?? '').trim();
        const mark = (id, bad) => body.querySelector(`#${id}`)?.closest('.st-field')?.classList.toggle('invalid', !!bad);
        const name = val('cf-name'); mark('cf-name', !name);
        if (!name) throw new Error('Give the channel a name');
        const channel_type = val('cf-type') || 'toast';
        const enabled = body.querySelector('#cf-enabled')?.classList.contains('on') !== false;
        const config = {};
        if (channel_type === 'toast') config.toast = { enabled: true };
        else if (channel_type === 'email') {
            const to_email = val('cf-email-to'); mark('cf-email-to', !to_email);
            if (!to_email) throw new Error('Enter the email address');
            config.email = { to_email, subject_prefix: val('cf-email-prefix') || '[ALARM]' };
        } else if (channel_type === 'sms') {
            const to_number = val('cf-sms-to'); mark('cf-sms-to', !to_number);
            if (!to_number) throw new Error('Enter the phone number');
            config.sms = { to_number };
        } else if (channel_type === 'webhook') {
            const url = val('cf-wh-url'); mark('cf-wh-url', !url);
            if (!url) throw new Error('Enter the webhook URL');
            config.webhook = { url, method: val('cf-wh-method') || 'POST', headers: {}, secret: val('cf-wh-secret') || null };
        } else if (channel_type === 'discord') {
            const webhook_url = val('cf-dc-url'); mark('cf-dc-url', !webhook_url);
            if (!webhook_url) throw new Error('Enter the Discord webhook URL');
            config.discord = { webhook_url, username: val('cf-dc-user') || null };
        } else if (channel_type === 'webpush') {
            config.webpush = { url: val('cf-wp-url'), token: val('cf-wp-token') || null, verify_tls: true };
        }
        return { name, channel_type, config, enabled };
    },

    // ── policy editor ─────────────────────────────────────────────────
    newConfig() { this._openPolicyEditor({ enabled: true, auto_dismiss: true, channels: [], min_severity: null, source_hosts: [], metric_sources: [], metric_names: [], repeat_interval_minutes: 30, min_alarm_count: 1, notify_on_clear: false }, 'new'); },
    editConfig(id) { const p = this.cfgById(id); if (p) this._openPolicyEditor(p, 'edit'); },
    copyConfig(id) {
        const p = this.cfgById(id);
        if (!p) return;
        const draft = { ...p, name: `${p.name} (copy)` };
        delete draft.config_id; delete draft.created_at; delete draft.last_triggered_at; delete draft.trigger_count;
        this._openPolicyEditor(draft, 'copy');
    },

    _catalog() {
        const list = MetricsManager.visible();
        return {
            hosts: [...new Set(list.map(m => m.hostname).filter(Boolean))].sort(),
            sources: [...new Set(list.map(m => m.source).filter(Boolean))].sort(),
            names: [...new Set(list.map(m => m.metric_name).filter(Boolean))].sort(),
        };
    },

    async _openPolicyEditor(p, mode) {
        if (!MetricsManager.visible().length) await MetricsManager.load().catch(() => {});
        const isEdit = mode === 'edit';
        const meta = isEdit ? `${p.name} · created ${fmtWhen(p.created_at)} · ${p.trigger_count ? `fired ${p.trigger_count} time${p.trigger_count === 1 ? '' : 's'}` : 'never fired'}` : (mode === 'copy' ? 'copy of an existing policy' : '');
        const state = {
            channels: new Set((p.channels || []).map(String)),
            hosts: [...(p.source_hosts || [])], sources: [...(p.metric_sources || [])], names: [...(p.metric_names || [])],
        };
        const catalog = this._catalog();
        ModalManager.open({
            title: isEdit ? 'Edit policy' : mode === 'copy' ? 'Duplicate policy' : 'New policy',
            meta, bodyHtml: this._policyFormHtml(p, state, catalog),
            submitLabel: isEdit ? 'Save policy' : 'Create policy',
            footNote: 'Applies to the next alert.',
            onOpen: (body) => this._wirePolicyForm(body, state, catalog),
            onSubmit: async () => {
                const body = document.getElementById('modalBody');
                const payload = this._collectPolicy(body, state);
                if (isEdit) await ApiClient.notifications.updateConfig(p.config_id, payload);
                else await ApiClient.notifications.createConfig(payload);
                ModalManager.close();
                ToastManager.show(isEdit ? 'Policy saved' : 'Policy created', 'success');
                await this._reload();
            },
        });
    },

    _chipsHtml(kind, values, catalog, anyLabel) {
        const chips = values.map(vv => `<span class="c" data-v="${escapeHtml(vv)}">${escapeHtml(vv)}${catalog.includes(vv) ? '' : ' <span class="nr">not reporting</span>'}<button type="button" class="rm" data-rm="${escapeHtml(vv)}" aria-label="Remove">×</button></span>`).join('');
        const left = catalog.filter(x => !values.includes(x));
        const menu = `<div class="mc-menu">${left.length ? left.map(x => `<button type="button" data-add="${escapeHtml(x)}">${escapeHtml(x)}</button>`).join('') : '<button type="button" disabled>nothing else reported</button>'}</div>`;
        return `${chips}${values.length ? '' : `<span class="any">${anyLabel}</span>`}<button type="button" class="add" data-menu>+ add</button>${menu}`;
    },

    _policyFormHtml(p, state, catalog) {
        const v = (x) => escapeHtml(x != null ? String(x) : '');
        const sev = p.min_severity || '';
        const hasToast = this.channels.some(ch => this.typeOf(ch) === 'toast' && state.channels.has(String(ch.channel_id)));
        const dismissS = Math.min(600, Math.max(1, parseInt(p.toast_dismiss_seconds, 10) || 10));
        return `<div class="policy-form">
            <div class="st-grid">
                <div class="st-field"><label for="pf-name">Name</label><div class="row"><input class="st-in full" id="pf-name" value="${v(p.name)}" placeholder="Critical alerts"></div></div>
                <div class="st-field"><label for="pf-desc">Description</label><div class="row"><input class="st-in full" id="pf-desc" value="${v(p.description)}" placeholder="Optional, shown under the policy name"></div></div>
            </div>
            <div class="grp"><span class="microlbl">Sends to<em>add the channels this policy delivers to</em></span><div class="chipsin" id="pf-channels">${this._channelChipsHtml(state)}</div></div>
            <div class="grp"><span class="microlbl">Match<em>every filter must pass · empty means any</em></span>
                <div class="st-grid">
                    <div class="st-field"><label>Minimum severity</label><div class="row"><span class="mc-seg" id="pf-sev"><button type="button" data-v=""${sev === '' ? ' class="on"' : ''}>any</button><button type="button" data-v="info"${sev === 'info' ? ' class="on"' : ''}>info</button><button type="button" class="warn${sev === 'warning' ? ' on' : ''}" data-v="warning">warning</button><button type="button" class="crit${sev === 'critical' ? ' on' : ''}" data-v="critical">critical</button></span></div></div>
                    <div class="st-field"><label>Hosts</label><div class="chipsin" data-kind="hosts">${this._chipsHtml('hosts', state.hosts, catalog.hosts, 'any host')}</div></div>
                    <div class="st-field"><label>Metric sources</label><div class="chipsin" data-kind="sources">${this._chipsHtml('sources', state.sources, catalog.sources, 'any source')}</div></div>
                    <div class="st-field"><label>Metric names</label><div class="chipsin" data-kind="names">${this._chipsHtml('names', state.names, catalog.names, 'any metric')}</div></div>
                </div>
            </div>
            <div class="grp"><span class="microlbl">Cadence</span>
                <div class="st-grid">
                    <div class="st-field"><label for="pf-min">First notify</label><div class="row"><span>after</span><input class="st-in n" id="pf-min" type="number" min="1" step="1" value="${v(p.min_alarm_count ?? 1)}" style="width:64px"><span class="unit">consecutive breaches</span></div><div class="help">1 notifies on the first breach.</div></div>
                    <div class="st-field"><label for="pf-repeat">Repeat</label><div class="row"><span>every</span><input class="st-in n" id="pf-repeat" type="number" min="0" step="1" value="${v(p.repeat_interval_minutes ?? 30)}" style="width:64px"><span class="unit">minutes while it keeps firing</span></div><div class="help">0 repeats on every cycle.</div></div>
                    <div class="st-field">${toggleHtml(!!p.notify_on_clear, 'Also notify when the alert clears', 'id="pf-clear"')}<div class="help">Only for alerts this policy already notified on.</div></div>
                    <div class="st-field" id="pf-toast-wrap"${hasToast ? '' : ' hidden'}>${toggleHtml(p.auto_dismiss !== false, 'Popups auto-dismiss', 'id="pf-dismiss"')}<div class="row" id="pf-dismiss-row"${p.auto_dismiss !== false ? '' : ' hidden'}><span>after</span><input class="st-in n" id="pf-dismiss-s" type="number" min="1" max="600" step="1" value="${dismissS}" style="width:64px"><span class="unit">seconds</span></div><div class="help">Off keeps popups on screen until dismissed. Applies to the Toast channel.</div></div>
                </div>
            </div>
            <div class="grp tight">${toggleHtml(p.enabled !== false, p.enabled !== false ? 'Policy enabled' : 'Policy disabled', 'id="pf-enabled"')}</div>
        </div>`;
    },

    // Selected channels as chips; "+ add" lists the rest.
    _channelChipsHtml(state) {
        const chosen = this.channels.filter(ch => state.channels.has(String(ch.channel_id)));
        const rest = this.channels.filter(ch => !state.channels.has(String(ch.channel_id)));
        const chip = (ch) => {
            const m = CHANNEL_META[this.typeOf(ch)] || { code: this.typeOf(ch), cls: '' };
            return `<span class="c${ch.enabled === false ? ' off' : ''}" title="${ch.enabled === false ? 'channel is off' : ''}"><span class="ntype ${escapeHtml(m.cls)}">${escapeHtml(m.code)}</span>${escapeHtml(ch.name)}${ch.enabled === false ? ' <span class="nr">off</span>' : ''}<button type="button" class="rm" data-rmch="${escapeHtml(String(ch.channel_id))}" aria-label="Remove">×</button></span>`;
        };
        const menu = `<div class="mc-menu">${rest.length ? rest.map(ch => { const m = CHANNEL_META[this.typeOf(ch)] || { code: this.typeOf(ch) }; return `<button type="button" data-addch="${escapeHtml(String(ch.channel_id))}"><span class="mi t">${escapeHtml(m.code)}</span>${escapeHtml(ch.name)}</button>`; }).join('') : `<button type="button" disabled>${this.channels.length ? 'every channel is already added' : 'no channels yet — add one first'}</button>`}</div>`;
        return `${chosen.map(chip).join('')}${chosen.length ? '' : '<span class="any">no channels — this policy sends nothing</span>'}<button type="button" class="add" data-menu>+ add</button>${menu}`;
    },

    _wirePolicyForm(body, state, catalog) {
        UI.seg(body.querySelector('#pf-sev'), () => {});
        UI.bindToggle(body.querySelector('#pf-clear'), 'Also notify when the alert clears', 'Also notify when the alert clears');
        UI.bindToggle(body.querySelector('#pf-dismiss'), 'Popups auto-dismiss', 'Popups auto-dismiss', (on) => { const r = body.querySelector('#pf-dismiss-row'); if (r) r.hidden = !on; });
        UI.bindToggle(body.querySelector('#pf-enabled'), 'Policy enabled', 'Policy disabled');
        const chBox = body.querySelector('#pf-channels');
        chBox?.addEventListener('click', (e) => {
            const rm = e.target.closest('[data-rmch]');
            const add = e.target.closest('[data-addch]');
            if (rm) state.channels.delete(rm.dataset.rmch);
            else if (add) { state.channels.add(add.dataset.addch); UI.closeMenus(); }
            else return;
            chBox.innerHTML = this._channelChipsHtml(state);
            const hasToast = this.channels.some(ch => this.typeOf(ch) === 'toast' && state.channels.has(String(ch.channel_id)));
            const wrap = body.querySelector('#pf-toast-wrap');
            if (wrap) wrap.hidden = !hasToast;
        });
        body.querySelectorAll('.chipsin').forEach(box => {
            box.addEventListener('click', (e) => {
                const kind = box.dataset.kind;
                const rm = e.target.closest('[data-rm]');
                const add = e.target.closest('[data-add]');
                if (rm) { state[kind] = state[kind].filter(x => x !== rm.dataset.rm); }
                else if (add) { if (!state[kind].includes(add.dataset.add)) state[kind].push(add.dataset.add); UI.closeMenus(); }
                else return;
                const anyLabel = { hosts: 'any host', sources: 'any source', names: 'any metric' }[kind];
                box.innerHTML = this._chipsHtml(kind, state[kind], catalog[kind], anyLabel);
            });
        });
    },

    _collectPolicy(body, state) {
        const val = (id) => (body.querySelector(`#${id}`)?.value ?? '').trim();
        const mark = (id, bad) => body.querySelector(`#${id}`)?.closest('.st-field')?.classList.toggle('invalid', !!bad);
        const name = val('pf-name'); mark('pf-name', !name);
        if (!name) throw new Error('Give the policy a name');
        const on = (id) => body.querySelector(`#${id}`)?.classList.contains('on') === true;
        const minSev = body.querySelector('#pf-sev button.on')?.dataset.v || '';
        return {
            name, description: val('pf-desc') || null,
            channels: Array.from(state.channels),
            enabled: on('pf-enabled'), auto_dismiss: body.querySelector('#pf-dismiss') ? on('pf-dismiss') : true,
            toast_dismiss_seconds: Math.min(600, Math.max(1, parseInt(val('pf-dismiss-s'), 10) || 10)),
            min_severity: minSev || null,
            metric_sources: [...state.sources], metric_names: [...state.names], source_hosts: [...state.hosts],
            repeat_interval_minutes: Math.max(0, parseInt(val('pf-repeat'), 10) || 0),
            min_alarm_count: Math.max(1, parseInt(val('pf-min'), 10) || 1),
            notify_on_clear: on('pf-clear'),
        };
    },
};

// Older callers (and the manager's docs) know this name.
const NotificationsTester = {
    test: (id) => NotificationsManager.testChannel(id),
    delete: (id) => NotificationsManager.deleteChannel(id),
    editChannel: (id) => NotificationsManager.editChannel(id),
    deleteConfig: (id) => NotificationsManager.deleteConfig(id),
    editConfig: (id) => NotificationsManager.editConfig(id),
};
