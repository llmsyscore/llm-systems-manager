/**
 * Alarm Engine Dashboard - Main Application Entry Point
 * Initializes all modules, manages UI state, and coordinates event handling.
 */

// ── HTML escape helper ──
// Every field that originates from a metric payload, rule, channel, alert,
// or delivery passes through agent / operator input and may contain `<`, `"`,
// or `'`. Escape with this before interpolating into an `innerHTML` template
// (works for both element-body and attribute-value contexts since we escape
// the same five characters either way).
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
window.escapeHtml = escapeHtml;

// ── Global State ──

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };

const AppState = {
    currentTab: 'dashboard',
    alerts: [],
    rules: [],
    metrics: [],
    notifications: { methods: [], rules: [] },
    historyData: {},
    selectedAlerts: new Set(),
    filters: {
        alerts: { severity: 'all', status: 'all', search: '' },
        rules: { type: 'all', active: 'all', search: '' },
        rulesSort: { key: 'name', dir: 'asc' },
        rulesPage: { page: 1, pageSize: 10 },
        deliveriesPage: { page: 1, pageSize: 20 },
    },
};

// ── UI State Manager ──

const UIStates = {
    connected: false,
    loading: { alerts: false, rules: false, metrics: false },
    modalOpen: false,

    setConnected(value) {
        this.connected = value;
        const indicator = document.getElementById('connectionStatus');
        if (indicator) {
            const dot = indicator.querySelector('.status-dot');
            const text = indicator.querySelector('.status-text');
            if (dot) {
                dot.className = `status-dot ${value ? 'connected' : 'disconnected'}`;
            }
            if (text) {
                text.textContent = value ? 'Live' : 'Disconnected';
            }
        }
    },

    setLoading(resource, value) {
        this.loading[resource] = value;
        const spinner = document.getElementById(`${resource}-loading`);
        if (spinner) {
            spinner.style.display = value ? 'flex' : 'none';
        }
    },

    setModalOpen(value) {
        this.modalOpen = value;
        const overlay = document.getElementById('modalOverlay');
        if (overlay) {
            overlay.classList.toggle('visible', value);
        }
    },
};

// ── Toast Notification Manager ──
//
// Toasts that originate from a server-side alert carry an `alertId`. Dismissals
// of those toasts are broadcast on a same-origin BroadcastChannel so the main
// dashboard's parallel toast stack (in /opt/llm-systems-manager/frontend/index.html)
// can drop its copy too. Without this, dismissing on one view leaves the other
// view still showing the same notification — the user reads it twice.

const _dismissedAlertIds = new Set();
let _toastBus = null;
try {
    _toastBus = new BroadcastChannel('alarm-toasts');
    _toastBus.onmessage = (e) => {
        const msg = e.data || {};
        if (msg.type === 'dismiss' && msg.alertId) {
            ToastManager._receiveDismiss(msg.alertId);
        }
    };
} catch (_) { /* older browser — toasts simply won't sync */ }

const ToastManager = {
    toasts: [],
    maxToasts: 5,

    show(message, type = 'info', options = {}) {
        // Backwards-compat: legacy callers pass duration as a number 3rd arg.
        if (typeof options === 'number') options = { duration: options };
        const sticky = options.sticky === true;
        const duration = options.duration != null ? options.duration : 10000;
        const alertId = options.alertId || null;
        const subtitle = options.subtitle || '';

        // If a sibling view already dismissed this alert, suppress the toast
        // entirely rather than briefly flashing it.
        if (alertId && _dismissedAlertIds.has(alertId)) return;

        let container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.className = 'toast-container';
            document.body.appendChild(container);
        }

        // Message and subtitle are treated as plain text. Callers that need a
        // two-line layout should pass `subtitle` separately instead of
        // injecting raw HTML — the inputs come from WebSocket/agent payloads
        // and were previously a DOM XSS sink (rule_name, metric_name, etc).
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        if (alertId) toast.dataset.alertId = alertId;
        const stickyBadge = sticky ? '<span class="toast-sticky-indicator">Sticky</span>' : '';
        const subtitleHtml = subtitle
            ? `<br><small style="opacity:0.8">${escapeHtml(subtitle)}</small>`
            : '';
        const actionButtons = alertId ? `
            <button class="toast-action toast-ack" type="button" title="Acknowledge alert">Ack</button>
            <button class="toast-action toast-resolve" type="button" title="Close alert">Close</button>
        ` : '';
        toast.innerHTML = `
            <span class="toast-message">${escapeHtml(message)}${subtitleHtml}</span>
            ${actionButtons}
            <button class="toast-close" type="button" aria-label="Dismiss">×</button>
            ${stickyBadge}
        `;

        const dismiss = (broadcast = true) => {
            if (toast._dismissed) return;
            toast._dismissed = true;
            toast.classList.remove('show');
            toast.classList.add('hide');
            setTimeout(() => toast.remove(), 350);
            if (broadcast && alertId) {
                _dismissedAlertIds.add(alertId);
                if (_toastBus) {
                    try { _toastBus.postMessage({ type: 'dismiss', alertId }); } catch (_) {}
                }
            }
        };

        toast.querySelector('.toast-close').addEventListener('click', (e) => {
            e.stopPropagation();
            dismiss(true);
        });
        toast._dismiss = dismiss;

        if (alertId) {
            toast.classList.add('toast-clickable');
            toast.title = 'Click to open All Alerts';
            toast.addEventListener('click', () => {
                if (typeof TabManager !== 'undefined') TabManager.switchTab('alerts');
                dismiss(true);
            });
            const ackBtn = toast.querySelector('.toast-ack');
            const resBtn = toast.querySelector('.toast-resolve');
            if (ackBtn) ackBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                try {
                    await ApiClient.alerts.acknowledge(alertId);
                    ToastManager.show('Alert acknowledged', 'success');
                } catch {
                    ToastManager.show('Failed to acknowledge alert', 'error');
                }
                dismiss(true);
            });
            if (resBtn) resBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                try {
                    await ApiClient.alerts.close(alertId);
                    ToastManager.show('Alert closed', 'success');
                } catch {
                    ToastManager.show('Failed to close alert', 'error');
                }
                dismiss(true);
            });
        }

        container.appendChild(toast);
        // Trigger slide-in on the next frame so the transition fires.
        requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));

        if (!sticky) {
            setTimeout(() => dismiss(true), duration);
        }

        // Cap simultaneous toasts (drop the oldest non-sticky first).
        while (container.children.length > this.maxToasts) {
            const victim = Array.from(container.children).find(
                el => !el.querySelector('.toast-sticky-indicator')
            ) || container.firstChild;
            victim.remove();
        }
    },

    _receiveDismiss(alertId) {
        _dismissedAlertIds.add(alertId);
        const container = document.getElementById('toastContainer');
        if (!container) return;
        container.querySelectorAll(`.toast[data-alert-id="${alertId}"]`).forEach(el => {
            if (el._dismiss) el._dismiss(false);
        });
    },
};

// ── Timestamp utility ──
// datetime.utcnow() produces naive ISO strings (no Z). JavaScript treats those as local
// time which causes timestamps to appear offset. Append Z to force UTC interpretation.
function parseTs(ts) {
    if (!ts) return null;
    if (typeof ts === 'string' && !ts.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(ts)) {
        ts += 'Z';
    }
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d;
}

// ── Notifications Tester (test/delete buttons for channels) ──

window.NotificationsTester = {
    _findChannel(channelId) {
        const list = AppState.notifications?.methods || [];
        return list.find(c => (c.channel_id || c.id) === channelId);
    },

    async test(channelId) {
        const ch = this._findChannel(channelId);
        if (!ch) {
            ToastManager.show('Channel not found in local state', 'error');
            return;
        }
        const type = (ch.channel_type || ch.type || '').toLowerCase();
        const name = ch.name || 'channel';

        try {
            const result = await ApiClient.notifications.testChannel({
                channel_id: channelId,
                channel_type: type,
                title: `🔔 Test from "${name}"`,
                body: `Test notification from the ${type} channel "${name}"`,
                severity: 'info',
            });
            if (result && result.status === 'ok') {
                // Toast channels: the backend now sends a WS notification which
                // ToastManager receives via the 'notification' websocket handler.
                // For non-toast channels, confirm in-UI that the send succeeded.
                if (type !== 'toast') {
                    ToastManager.show(`Test sent via ${type}`, 'success');
                }
                // Refresh delivery history to show the new record
                setTimeout(() => TabManager.loadNotifications(), 800);
            } else {
                const err = result?.details?.error || 'unknown error';
                ToastManager.show(`Test failed: ${err}`, 'error');
            }
        } catch (e) {
            ToastManager.show(`Test failed: ${e.message || 'request error'}`, 'error');
        }
    },

    async delete(channelId) {
        if (!confirm('Delete this notification channel?')) return;
        try {
            await ApiClient.notifications.deleteChannel(channelId);
            ToastManager.show('Channel deleted', 'success');
            TabManager.loadNotifications();
        } catch (e) {
            ToastManager.show(`Delete failed: ${e.message || 'error'}`, 'error');
        }
    },

    async editChannel(channelId) {
        const ch = (AppState.notifications?.methods || []).find(
            c => String(c.channel_id) === String(channelId)
        );
        if (!ch) { ToastManager.show('Channel not found', 'error'); return; }
        ModalManager.open('create-channel-modal', {
            initialData: ch,
            onSubmit: async (data) => {
                try {
                    await ApiClient.notifications.updateChannel(channelId, data);
                    ToastManager.show('Channel updated', 'success');
                    ModalManager.close();
                    TabManager.loadNotifications();
                } catch (e) {
                    ToastManager.show(`Failed to update: ${e.message || 'error'}`, 'error');
                }
            },
        });
    },

    async deleteConfig(configId) {
        if (!confirm('Delete this notification config?')) return;
        try {
            await ApiClient.notifications.deleteConfig(configId);
            ToastManager.show('Config deleted', 'success');
            TabManager.loadNotifications();
        } catch (e) {
            ToastManager.show(`Delete failed: ${e.message || 'error'}`, 'error');
        }
    },

    async editConfig(configId) {
        const cfg = (AppState.notifications?.configs || []).find(
            c => String(c.config_id) === String(configId)
        );
        if (!cfg) { ToastManager.show('Config not found', 'error'); return; }
        const channels = AppState.notifications?.methods || [];
        const catalog = await ModalManager._fetchPolicyCatalog();
        ModalManager.open('create-config-modal', {
            channels,
            catalog,
            initialData: cfg,
            onSubmit: async (data) => {
                try {
                    await ApiClient.notifications.updateConfig(configId, data);
                    ToastManager.show('Config updated', 'success');
                    ModalManager.close();
                    TabManager.loadNotifications();
                } catch (e) {
                    ToastManager.show(`Failed to update: ${e.message || 'error'}`, 'error');
                }
            },
        });
    },
};

// ── Dashboard Manager ──

const DashboardManager = {
    async init() {
        this.renderStats();
        // Load all sections in parallel, then re-render stats with real counts.
        await Promise.allSettled([
            AlertManager.load(),
            RuleManager.load(),
            MetricsManager.load(),
            RecentAlertsManager.load(),
        ]);
        this.renderStats();
    },

    async refresh(data = {}) {
        if (data.alerts) {
            AlertManager.load();
            RecentAlertsManager.load();
        }
        if (data.rules) RuleManager.load();
        if (data.metrics) MetricsManager.load();
    },

    renderStats() {
        const activeAlerts = AppState.alerts.filter(a => a.status === 'active').length;
        const criticalAlerts = AppState.alerts.filter(a => a.status === 'active' && a.severity === 'critical').length;
        const activeRules = AppState.rules.filter(r => r.enabled).length;
        const metricsTracked = AppState.metrics.length;

        const anomalyTypes = ['z_score', 'moving_average', 'percentile', 'rate_of_change'];
        const anomalyRuleIds = new Set(
            AppState.rules.filter(r => anomalyTypes.includes(r.rule_type)).map(r => String(r.rule_id))
        );
        const anomalyCount = AppState.alerts.filter(a => a.rule_id && anomalyRuleIds.has(String(a.rule_id))).length;

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('activeAlertsCount', activeAlerts);
        set('criticalAlertsCount', criticalAlerts);
        set('rulesCount', activeRules);
        set('metricsCount', metricsTracked);
        set('anomaliesCount', anomalyCount);

        const badge = document.getElementById('alertsBadge');
        if (badge) badge.textContent = activeAlerts;
    },
};

// ── Alert Manager ──

const AlertManager = {
    async load() {
        UIStates.setLoading('alerts', true);
        try {
            const filters = AppState.filters.alerts;
            const params = {};
            if (filters.severity !== 'all') params.severity = filters.severity;
            if (filters.status !== 'all') params.status = filters.status;
            if (filters.search) params.search = filters.search;

            const alerts = await ApiClient.alerts.list(params);
            AppState.alerts = alerts || [];
            this.render();
            updateLastRefreshed();
        } catch (error) {
            console.error('Failed to load alerts:', error);
            ToastManager.show('Failed to load alerts', 'error');
        } finally {
            UIStates.setLoading('alerts', false);
        }
    },

    render() {
        const filtered = this.applyFilters(AppState.alerts);
        const activeAlerts = AppState.alerts.filter(a => a.status === 'active');

        // Dashboard widget: show active alerts as cards
        const widgetContainer = document.getElementById('alertsList');
        if (widgetContainer) {
            if (activeAlerts.length === 0) {
                widgetContainer.innerHTML = '<div class="empty-state"><p>No active alerts</p></div>';
            } else {
                widgetContainer.innerHTML = activeAlerts.map(a => this.renderAlertItem(a)).join('');
            }
        }

        // Dashboard anomaly feed: alerts from anomaly-type rules
        const anomalyTypes = ['z_score', 'moving_average', 'percentile', 'rate_of_change'];
        const anomalyRuleIds = new Set(
            AppState.rules.filter(r => anomalyTypes.includes(r.rule_type)).map(r => String(r.rule_id))
        );
        const anomalyAlerts = AppState.alerts.filter(a =>
            a.rule_id && anomalyRuleIds.has(String(a.rule_id))
        ).slice(0, 20);
        const anomalyList = document.getElementById('anomalyList');
        if (anomalyList) {
            if (anomalyAlerts.length === 0) {
                anomalyList.innerHTML = '<div class="empty-state"><p>No anomalies detected</p></div>';
            } else {
                anomalyList.innerHTML = anomalyAlerts.map(a => {
                    const ts = parseTs(a.created_at);
                    const tStr = ts ? ts.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }) : '—';
                    return `<div class="anomaly-item">
                        <div class="anomaly-item-content">
                            <div class="anomaly-item-message">${escapeHtml(a.rule_name || 'Anomaly')}: ${escapeHtml(a.message || '')}</div>
                            <div class="anomaly-item-meta">
                                <span>${escapeHtml(a.metric_source)}/${escapeHtml(a.metric_name)}</span>
                                <span>= ${a.current_value != null ? Number(a.current_value).toFixed(2) : '—'}</span>
                                <span>${escapeHtml(tStr)}</span>
                            </div>
                        </div>
                    </div>`;
                }).join('');
            }
        }

        // Alerts tab: table rows
        const tbody = document.getElementById('alertsTableBody');
        if (tbody) {
            if (filtered.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" class="empty-row">No alerts to display</td></tr>';
            } else {
                tbody.innerHTML = filtered.map(a => this.renderAlertRow(a)).join('');
            }
        }
    },

    // Drop a legacy "[rule_name] " prefix from the message so old alerts that
    // were stored with the prefix display the same as new ones (where the
    // backend no longer adds it).
    _stripRulePrefix(message, ruleName) {
        if (!message || !ruleName) return message || '';
        const prefix = `[${ruleName}]`;
        return message.startsWith(prefix)
            ? message.slice(prefix.length).trimStart()
            : message;
    },

    renderAlertRow(alert) {
        const sev = alert.severity || 'info';
        const time = alert.created_at ? (parseTs(alert.created_at)?.toLocaleString() ?? '—') : '—';
        const lastEval = alert.last_evaluated_at
            ? (parseTs(alert.last_evaluated_at)?.toLocaleString() ?? '—')
            : '—';
        const count = alert.trigger_count != null ? alert.trigger_count : 1;
        const sourceCol = alert.source_host
            || [alert.metric_source, alert.metric_name].filter(Boolean).join('/')
            || '—';
        // Severity drives both the severity column color and the status column
        // color so the user can scan the table by severity at a glance — no
        // separate status icon column needed.
        const aid = escapeHtml(alert.alert_id);
        const safeSev = escapeHtml(sev);
        // Rule column links to the rule editor when the alert carries a rule_id
        // (anomaly/manual alerts without one stay plain text). The id rides a
        // data attribute and a delegated click handler calls RuleManager.edit —
        // no inline onclick string built from data (avoids the JS-context XSS).
        const ruleName = escapeHtml(alert.rule_name || '—');
        const ruleCell = alert.rule_id
            ? `<a href="#" class="alert-rule-link" title="Open this rule for editing" data-rule-id="${escapeHtml(String(alert.rule_id))}">${ruleName}</a>`
            : ruleName;
        return `
            <tr data-alert-id="${aid}">
                <td><input type="checkbox" class="alert-checkbox" data-alert-id="${aid}"></td>
                <td class="alert-sev-cell sev-${safeSev}">${escapeHtml(sev.toUpperCase())}</td>
                <td>${ruleCell}</td>
                <td>${escapeHtml(sourceCol)}</td>
                <td>${escapeHtml(this._stripRulePrefix(alert.message, alert.rule_name) || '—')}</td>
                <td class="alert-status-cell sev-${safeSev}">${escapeHtml(alert.status || '—')}</td>
                <td>${escapeHtml(time)}</td>
                <td>${escapeHtml(lastEval)}</td>
                <td>${escapeHtml(count)}</td>
                <td>
                    ${alert.status === 'active' ? `<button class="btn btn-sm btn-accent" onclick="AlertManager.acknowledge('${aid}')">Ack</button>` : ''}
                    ${alert.status !== 'closed' ? `<button class="btn btn-sm btn-success" onclick="AlertManager.close('${aid}')">Close</button>` : ''}
                </td>
            </tr>`;
    },

    renderAlertItem(alert) {
        const sev = alert.severity || 'info';
        const ts = parseTs(alert.created_at);
        const timeStr = ts ? ts.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
        const metric = [alert.metric_source, alert.metric_name].filter(Boolean).join('/');
        const host = alert.source_host || '';
        // Detail row shows just metric + time — value/threshold are already in
        // the alert message (e.g. "Value 49.6% exceeds threshold 5.0%") so
        // duplicating them here only adds noise.
        const aid = escapeHtml(alert.alert_id);
        const safeSev = escapeHtml(sev);
        return `
            <div class="alert-row sev-${safeSev}" data-alert-id="${aid}">
                <span class="sev-dot sev-dot-${safeSev}"></span>
                <span class="sev-badge sev-${safeSev}">${escapeHtml(sev.toUpperCase())}</span>
                <div class="alert-row-body">
                    <div class="alert-row-rule">${escapeHtml(alert.rule_name || 'Alert')}${host ? ` <span class="alert-row-host" style="opacity:0.7;font-weight:normal;">· ${escapeHtml(host)}</span>` : ''}</div>
                    ${alert.message ? `<div class="alert-row-message">${escapeHtml(this._stripRulePrefix(alert.message, alert.rule_name))}</div>` : ''}
                    <div class="alert-row-detail">
                        ${metric ? `<span class="alert-row-metric">${escapeHtml(metric)}</span>` : ''}
                        <span class="alert-row-time">${escapeHtml(timeStr)}</span>
                    </div>
                </div>
                <div class="alert-row-acts">
                    ${alert.status === 'active' ? `<button class="btn-xs btn-ack" onclick="AlertManager.acknowledge('${aid}')">Ack</button>` : ''}
                    ${alert.status !== 'closed' ? `<button class="btn-xs btn-close-alert" onclick="AlertManager.close('${aid}')">Close</button>` : ''}
                </div>
            </div>`;
    },

    applyFilters(alerts) {
        return alerts.filter(alert => {
            if (AppState.filters.alerts.severity !== 'all' && alert.severity !== AppState.filters.alerts.severity) return false;
            if (AppState.filters.alerts.status !== 'all' && alert.status !== AppState.filters.alerts.status) return false;
            if (AppState.filters.alerts.search) {
                const search = AppState.filters.alerts.search.toLowerCase();
                return (alert.rule_name || '').toLowerCase().includes(search) ||
                       (alert.message || '').toLowerCase().includes(search);
            }
            return true;
        });
    },

    async acknowledge(alertId) {
        try {
            await ApiClient.alerts.acknowledge(alertId);
            ToastManager.show('Alert acknowledged', 'success');
            this.load();
        } catch (error) {
            ToastManager.show('Failed to acknowledge alert', 'error');
        }
    },

    async close(alertId) {
        try {
            await ApiClient.alerts.close(alertId);
            ToastManager.show('Alert closed', 'success');
            this.load();
        } catch (error) {
            ToastManager.show('Failed to close alert', 'error');
        }
    },

    async ignore(alertId) {
        try {
            await ApiClient.alerts.ignore(alertId);
            ToastManager.show('Alert ignored', 'info');
            this.load();
        } catch (error) {
            ToastManager.show('Failed to ignore alert', 'error');
        }
    },

    async createException(alertId) {
        ModalManager.open('exception-modal', {
            onSubmit: async (data) => {
                try {
                    await ApiClient.alerts.createException(alertId, data);
                    ToastManager.show('Exception created', 'success');
                    ModalManager.close();
                    this.load();
                } catch (error) {
                    ToastManager.show('Failed to create exception', 'error');
                }
            },
        });
    },

    async adjustThreshold(alertId) {
        const alert = AppState.alerts.find(a => a.id === alertId);
        if (!alert) return;

        ModalManager.open('threshold-modal', {
            initialThreshold: alert.threshold_value,
            onSubmit: async (data) => {
                try {
                    await ApiClient.rules.update(alert.rule_id, { threshold: data.threshold });
                    ToastManager.show('Threshold updated', 'success');
                    ModalManager.close();
                    this.load();
                } catch (error) {
                    ToastManager.show('Failed to update threshold', 'error');
                }
            },
        });
    },

    handleNewAlert(payload) {
        AppState.alerts.unshift(payload);
        this.render();
        DashboardManager.renderStats();
        // While on the alerts tab, ensure the table reflects server truth
        // (e.g. server-side dedup/sort). render() above keeps it snappy;
        // the load() is the belt-and-braces re-sync.
        if (AppState.currentTab === 'alerts') {
            this.load();
        }
    },

    handleAlertUpdate(payload) {
        // Server sends `alert_id` (UUID), not `id`. The previous match always
        // failed, so ack/close events never refreshed the table.
        const matchId = payload.alert_id || payload.id;
        const idx = AppState.alerts.findIndex(a =>
            (a.alert_id && a.alert_id === matchId) || (a.id && a.id === matchId)
        );
        if (idx !== -1) {
            AppState.alerts[idx] = { ...AppState.alerts[idx], ...payload };
            this.render();
            DashboardManager.renderStats();
        }
        if (AppState.currentTab === 'alerts') {
            this.load();
        }
    },
};

// ── Recent Alerts Manager (dashboard widget) ──
// Standalone fetcher for the dashboard's "Recent Alerts" history widget.
// It pulls the last 25 alerts of any status (active, acknowledged, closed)
// via /api/alarm/alerts/?include_closed=true&limit=25 — kept separate from
// AlertManager so the alerts table (active+ack only) view stays unchanged.

const RecentAlertsManager = {
    items: [],

    async load() {
        try {
            const data = await ApiClient._request('/alerts/?include_closed=true&limit=25');
            this.items = Array.isArray(data) ? data : [];
            this.render();
        } catch (e) {
            console.warn('RecentAlertsManager.load failed', e);
            const el = document.getElementById('recentAlertsList');
            if (el) el.innerHTML = '<div class="empty-state"><p>Failed to load recent alerts</p></div>';
        }
    },

    render() {
        const el = document.getElementById('recentAlertsList');
        if (!el) return;
        if (!this.items.length) {
            el.innerHTML = '<div class="empty-state"><p>No alerts in history</p></div>';
            return;
        }
        // Compact row layout: severity pill, rule name + host, message, status, age.
        // Reuses the dashboard's existing severity color tokens (sev-critical etc.)
        // so the widget feels native to the rest of the dashboard.
        const rows = this.items.map(a => {
            const sev = a.severity || 'info';
            const ts = parseTs(a.created_at);
            const age = ts ? ts.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
            const host = a.source_host || '';
            const metric = [a.metric_source, a.metric_name].filter(Boolean).join('/');
            const msg = AlertManager._stripRulePrefix(a.message, a.rule_name) || '';
            const status = a.status || '';
            const safeSev = escapeHtml(sev);

            // Cleared-alert chip: shown for closed alerts. Includes why
            // (auto vs manual) and the value at the moment of resolution
            // when available. Older closed alerts without metadata fall
            // back to a plain "Cleared" chip.
            let clearedChip = '';
            if (String(status).toLowerCase() === 'closed') {
                const reason = (a.resolution_reason || '').toLowerCase();
                const reasonLabel =
                    reason === 'auto'   ? 'auto · threshold cleared' :
                    reason === 'manual' ? 'manually closed' :
                    'cleared';
                let valuePart = '';
                if (a.resolved_value !== undefined && a.resolved_value !== null && a.resolved_value !== '') {
                    const num = Number(a.resolved_value);
                    if (!Number.isNaN(num)) valuePart = ` @ ${num.toFixed(2)}`;
                }
                clearedChip = `<span class="recent-alert-cleared-chip" title="Alert closed: ${escapeHtml(reasonLabel)}${valuePart ? ' ' + escapeHtml(valuePart) : ''}">✓ ${escapeHtml(reasonLabel)}${escapeHtml(valuePart)}</span>`;
            }

            return `
                <div class="recent-alert-row sev-${safeSev}">
                    <span class="sev-badge sev-${safeSev}">${escapeHtml(sev.toUpperCase())}</span>
                    <div class="recent-alert-body">
                        <div class="recent-alert-rule">${escapeHtml(a.rule_name || 'Alert')}${host ? ` <span class="recent-alert-host">· ${escapeHtml(host)}</span>` : ''}</div>
                        ${msg ? `<div class="recent-alert-msg">${escapeHtml(msg)}</div>` : ''}
                        <div class="recent-alert-meta">
                            ${metric ? `<span class="recent-alert-metric">${escapeHtml(metric)}</span>` : ''}
                            <span class="recent-alert-status sev-${safeSev}">${escapeHtml(status)}</span>
                            ${clearedChip}
                            <span class="recent-alert-time">${escapeHtml(age)}</span>
                        </div>
                    </div>
                </div>`;
        }).join('');
        el.innerHTML = rows;
    },
};

// ── Rule Manager ──

const RuleManager = {
    async load() {
        UIStates.setLoading('rules', true);
        try {
            const rules = await ApiClient.rules.list();
            AppState.rules = rules || [];
            this.render();
            if (typeof ChartManager !== 'undefined') ChartManager.refreshAnnotations();
        } catch (error) {
            console.error('Failed to load rules:', error);
            ToastManager.show('Failed to load rules', 'error');
        } finally {
            UIStates.setLoading('rules', false);
        }
    },

    _sortKey(rule, key) {
        switch (key) {
            case 'source':
                return (rule.source_host ? rule.source_host + ' ' : '') + (rule.metric_source || '');
            case 'threshold': {
                const t = rule.config?.threshold || {};
                const v = t.upper ?? t.value ?? t.lower;
                return v == null ? Number.NEGATIVE_INFINITY : Number(v);
            }
            case 'severity':
                return SEVERITY_RANK[(rule.severity || 'info').toLowerCase()] ?? 99;
            case 'enabled':
                return rule.enabled ? 0 : 1;
            case 'created_at':
                return rule.created_at ? new Date(rule.created_at).getTime() : 0;
            default:
                return (rule[key] ?? '').toString().toLowerCase();
        }
    },

    _sortRules(rules) {
        const { key, dir } = AppState.filters.rulesSort;
        const sign = dir === 'desc' ? -1 : 1;
        const decorated = rules.map(r => [this._sortKey(r, key), r]);
        decorated.sort((a, b) => {
            if (a[0] < b[0]) return -1 * sign;
            if (a[0] > b[0]) return  1 * sign;
            return 0;
        });
        return decorated.map(d => d[1]);
    },

    _wireHeaderSort() {
        const table = document.getElementById('rulesTable');
        if (!table || table.dataset.sortWired) return;
        table.dataset.sortWired = '1';
        table.querySelectorAll('th.sortable').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.dataset.sortKey;
                if (!key) return;
                const cur = AppState.filters.rulesSort;
                if (cur.key === key) {
                    cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
                } else {
                    cur.key = key;
                    cur.dir = 'asc';
                }
                this.render();
            });
        });
    },

    _paintSortIndicators() {
        const table = document.getElementById('rulesTable');
        if (!table) return;
        const { key, dir } = AppState.filters.rulesSort;
        table.querySelectorAll('th.sortable').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.sortKey === key) {
                th.classList.add(dir === 'desc' ? 'sort-desc' : 'sort-asc');
            }
        });
    },

    _wireFilters() {
        const search = document.getElementById('rulesSearchInput');
        const typeSel = document.getElementById('rulesTypeFilter');
        const statSel = document.getElementById('rulesStatusFilter');
        if (search && !search.dataset.wired) {
            search.dataset.wired = '1';
            search.value = AppState.filters.rules.search || '';
            search.addEventListener('input', (e) => {
                AppState.filters.rules.search = e.target.value || '';
                AppState.filters.rulesPage.page = 1;
                this.render();
            });
        }
        if (typeSel && !typeSel.dataset.wired) {
            typeSel.dataset.wired = '1';
            typeSel.value = AppState.filters.rules.type || 'all';
            typeSel.addEventListener('change', (e) => {
                AppState.filters.rules.type = e.target.value || 'all';
                AppState.filters.rulesPage.page = 1;
                this.render();
            });
        }
        if (statSel && !statSel.dataset.wired) {
            statSel.dataset.wired = '1';
            statSel.value = AppState.filters.rules.active || 'all';
            statSel.addEventListener('change', (e) => {
                AppState.filters.rules.active = e.target.value || 'all';
                AppState.filters.rulesPage.page = 1;
                this.render();
            });
        }
    },

    render() {
        this._wireFilters();
        const filtered = this.applyFilters(AppState.rules);
        const total = AppState.rules.length;
        const countEl = document.getElementById('rulesFilterCount');
        if (countEl) {
            const filtersActive = (AppState.filters.rules.search?.trim())
                || AppState.filters.rules.type !== 'all'
                || AppState.filters.rules.active !== 'all';
            countEl.textContent = filtersActive
                ? `${filtered.length} of ${total} match`
                : '';
        }

        // Dashboard widget: card-style summary
        const widgetContainer = document.getElementById('rulesList');
        if (widgetContainer) {
            widgetContainer.innerHTML = filtered.length === 0
                ? '<div class="empty-state">No alarm rules configured</div>'
                : filtered.map(rule => this.renderRuleItem(rule)).join('');
        }

        const tbody = document.getElementById('rulesTableBody');
        if (tbody) {
            const sorted = this._sortRules(filtered);
            const { pageSize } = AppState.filters.rulesPage;
            const totalPages = pageSize > 0 ? Math.max(1, Math.ceil(sorted.length / pageSize)) : 1;
            if (AppState.filters.rulesPage.page > totalPages) {
                AppState.filters.rulesPage.page = totalPages;
            }
            const page = AppState.filters.rulesPage.page;
            const slice = pageSize > 0
                ? sorted.slice((page - 1) * pageSize, page * pageSize)
                : sorted;
            if (sorted.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" class="empty-row">No rules configured</td></tr>';
            } else {
                tbody.innerHTML = slice.map(rule => this.renderRuleRow(rule)).join('');
            }
            this._wireHeaderSort();
            this._paintSortIndicators();
            this._renderPagination(sorted.length, page, pageSize, totalPages);
        }
    },

    _renderPagination(total, page, pageSize, totalPages) {
        const range = document.getElementById('rulesPaginationRange');
        const pages = document.getElementById('rulesPagePages');
        const prev = document.getElementById('rulesPagePrev');
        const next = document.getElementById('rulesPageNext');
        const sizeSel = document.getElementById('rulesPageSize');
        if (!range || !pages || !prev || !next || !sizeSel) return;

        if (total === 0) {
            range.textContent = '0 rules';
            pages.textContent = 'Page 0 / 0';
        } else if (pageSize <= 0) {
            range.textContent = `Showing all ${total}`;
            pages.textContent = 'Page 1 / 1';
        } else {
            const start = (page - 1) * pageSize + 1;
            const end = Math.min(total, page * pageSize);
            range.textContent = `${start}–${end} of ${total}`;
            pages.textContent = `Page ${page} / ${totalPages}`;
        }
        prev.disabled = page <= 1 || pageSize <= 0;
        next.disabled = page >= totalPages || pageSize <= 0;

        if (!sizeSel.dataset.wired) {
            sizeSel.dataset.wired = '1';
            sizeSel.addEventListener('change', () => {
                AppState.filters.rulesPage.pageSize = parseInt(sizeSel.value, 10) || 10;
                AppState.filters.rulesPage.page = 1;
                this.render();
            });
            prev.addEventListener('click', () => {
                if (AppState.filters.rulesPage.page > 1) {
                    AppState.filters.rulesPage.page -= 1;
                    this.render();
                }
            });
            next.addEventListener('click', () => {
                AppState.filters.rulesPage.page += 1;
                this.render();
            });
        }
    },

    // Format the rule's threshold/condition for the Threshold column based on
    // rule_type. The legacy code rendered `${rule.condition} ${rule.threshold}`
    // which doesn't match the AlarmRule schema (threshold lives inside
    // config.threshold / config.z_score / etc.) so the column was always blank.
    _formatThreshold(rule) {
        const cfg = rule.config || {};
        const t = cfg.threshold || {};
        const unit = t.unit ? ` ${escapeHtml(t.unit)}` : '';
        if (rule.rule_type === 'threshold_above') {
            const v = t.upper ?? t.value;
            return v != null ? `&gt; ${v}${unit}` : '—';
        }
        if (rule.rule_type === 'threshold_below') {
            const v = t.lower;
            return v != null ? `&lt; ${v}${unit}` : '—';
        }
        if (rule.rule_type === 'threshold_range') {
            if (t.lower != null && t.upper != null) return `${t.lower}–${t.upper}${unit}`;
            return '—';
        }
        if (rule.rule_type === 'z_score') {
            const v = cfg.z_score?.threshold;
            return v != null ? `z &gt; ${v}` : '—';
        }
        if (rule.rule_type === 'moving_average') {
            const v = cfg.moving_average?.deviation_factor;
            return v != null ? `±${v}σ` : '—';
        }
        if (rule.rule_type === 'percentile') {
            const v = cfg.percentile?.percentile;
            return v != null ? `p${v}` : '—';
        }
        if (rule.rule_type === 'rate_of_change') {
            const v = cfg.rate_of_change?.max_change_per_minute;
            return v != null ? `±${v}/min` : '—';
        }
        return '—';
    },

    renderRuleRow(rule) {
        const created = rule.created_at ? (parseTs(rule.created_at)?.toLocaleDateString() ?? '—') : '—';
        const sourceCol = rule.source_host
            ? `${rule.source_host} · ${rule.metric_source || ''}`
            : (rule.metric_source || rule.source || '—');
        const enabled = !!rule.enabled;
        const rid = escapeHtml(rule.rule_id);
        const statusPill = `<span class="rule-status-pill ${enabled ? 'rule-status-on' : 'rule-status-off'}"
            title="Click to ${enabled ? 'disable' : 'enable'}"
            onclick="RuleManager.toggle('${rid}', ${!enabled})">${enabled ? 'Enabled' : 'Disabled'}</span>`;
        const sev = (rule.severity || 'info').toLowerCase();
        const safeSev = escapeHtml(sev);
        const sevBadge = `<span class="sev-badge sev-${safeSev}">${safeSev}</span>`;
        return `
            <tr data-rule-id="${rid}">
                <td>${escapeHtml(rule.name)}</td>
                <td><span class="badge">${escapeHtml(rule.rule_type)}</span></td>
                <td>${escapeHtml(sourceCol)}</td>
                <td><code>${escapeHtml(rule.metric_name || '—')}</code></td>
                <td>${this._formatThreshold(rule)}</td>
                <td>${sevBadge}</td>
                <td>${statusPill}</td>
                <td>${escapeHtml(created)}</td>
                <td>
                    <button class="btn btn-sm btn-secondary" onclick="RuleManager.edit('${rid}')">Edit</button>
                    <button class="btn btn-sm btn-secondary" onclick="RuleManager.copy('${rid}')">Copy</button>
                    <button class="btn btn-sm btn-danger" onclick="RuleManager.delete('${rid}')">Delete</button>
                </td>
            </tr>`;
    },

    renderRuleItem(rule) {
        const thresh = rule.config?.threshold?.upper ?? rule.config?.threshold?.value ?? '';
        const unit   = rule.config?.threshold?.unit || '';
        const metricBase = `${rule.metric_source || ''}/${rule.metric_name || ''}`;
        const metric = rule.source_host ? `${rule.source_host} · ${metricBase}` : metricBase;
        const rid = escapeHtml(rule.rule_id);
        const safeSev = escapeHtml(rule.severity);
        return `
            <div class="rule-row ${rule.enabled ? '' : 'rule-row-disabled'}" data-rule-id="${rid}">
                <label class="toggle-switch">
                    <input type="checkbox" ${rule.enabled ? 'checked' : ''}
                        onchange="RuleManager.toggle('${rid}', this.checked)">
                    <span class="toggle-slider"></span>
                </label>
                <span class="rule-row-name">${escapeHtml(rule.name)}</span>
                <span class="rule-row-metric">${escapeHtml(metric)}</span>
                ${thresh !== '' ? `<span class="rule-row-thresh">&gt; ${escapeHtml(thresh)}${unit ? ' ' + escapeHtml(unit) : ''}</span>` : ''}
                <span class="sev-badge sev-${safeSev}">${safeSev}</span>
            </div>`;
    },

    applyFilters(rules) {
        const f = AppState.filters.rules;
        const search = (f.search || '').trim().toLowerCase();
        return rules.filter(rule => {
            if (f.type !== 'all' && rule.rule_type !== f.type) return false;
            if (f.active === 'enabled' && !rule.enabled) return false;
            if (f.active === 'disabled' && rule.enabled) return false;
            if (search) {
                const hay = [
                    rule.name, rule.description,
                    rule.metric_source, rule.metric_name,
                    rule.source_host, rule.rule_type, rule.severity,
                ].filter(Boolean).join(' ').toLowerCase();
                if (!hay.includes(search)) return false;
            }
            return true;
        });
    },

    async toggle(ruleId, enabled) {
        try {
            await ApiClient.rules.toggle(ruleId);
            ToastManager.show(`Rule ${enabled ? 'enabled' : 'disabled'}`, 'success');
            this.load();
        } catch (error) {
            ToastManager.show('Failed to toggle rule', 'error');
        }
    },

    _openRuleModal({ initialData, titleOverride, submit, successMsg, errorMsg }) {
        ModalManager.open('create-rule-modal', {
            initialData,
            titleOverride,
            onSubmit: async (data) => {
                try {
                    await submit(data);
                    ToastManager.show(successMsg, 'success');
                    ModalManager.close();
                    this.load();
                } catch (e) {
                    ToastManager.show(errorMsg, 'error');
                }
            },
        });
    },

    async edit(ruleId) {
        let rule;
        try {
            rule = await ApiClient.rules.get(ruleId);
        } catch (e) {
            ToastManager.show('Failed to load rule (it may have been deleted)', 'error');
            return;
        }
        this._openRuleModal({
            initialData: rule,
            submit: (data) => ApiClient.rules.update(ruleId, data),
            successMsg: 'Rule updated',
            errorMsg: 'Failed to update rule',
        });
    },

    async copy(ruleId) {
        let rule;
        try {
            rule = await ApiClient.rules.get(ruleId);
        } catch (e) {
            ToastManager.show('Failed to load rule', 'error');
            return;
        }
        const draft = { ...rule };
        delete draft.rule_id;
        delete draft.created_at;
        delete draft.updated_at;
        draft.name = `${rule.name || 'Rule'} (copy)`;
        this._openRuleModal({
            initialData: draft,
            titleOverride: 'Copy Rule',
            submit: (data) => ApiClient.rules.create(data),
            successMsg: 'Rule copied',
            errorMsg: 'Failed to copy rule',
        });
    },

    async delete(ruleId) {
        const ok = await ModalManager.confirm({
            title: 'Delete Rule',
            message: 'Are you sure you want to delete this rule?',
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!ok) return;
        try {
            await ApiClient.rules.delete(ruleId);
            ToastManager.show('Rule deleted', 'success');
            this.load();
        } catch (error) {
            ToastManager.show('Failed to delete rule', 'error');
        }
    },

    handleRuleUpdate(payload) {
        const idx = AppState.rules.findIndex(r => r.id === payload.id);
        if (idx !== -1) {
            AppState.rules[idx] = { ...AppState.rules[idx], ...payload };
            this.render();
            DashboardManager.renderStats();
        } else {
            this.load();
        }
    },

    handleRuleDelete(payload) {
        AppState.rules = AppState.rules.filter(r => r.id !== payload.id);
        this.render();
        DashboardManager.renderStats();
    },
};

// ── Metrics Manager ──

const MetricsManager = {
    // Set of hostnames that should appear in device pulldowns. Populated
    // from the manager's /api/agents registry (same-origin: the AE UI is
    // served via the manager's /alarm/ proxy). When unknown, we keep the
    // permissive behaviour — any hostname is shown — so a registry fetch
    // failure doesn't hide everything.
    _knownAgentHosts: null,

    // Synthetic samples that shouldn't appear in operator-facing dropdowns:
    //   __probe__ / __probe_meta — agent self-monitoring (_probe_ae_ingest, _probe_influxdb)
    //   benchtest                — tools/llm-systems-benchtest.py load generator
    isProbeMetric(m) {
        const synthetic = (s) =>
            typeof s === 'string' && (s.startsWith('__probe') || s === 'benchtest');
        return synthetic(m.hostname) || synthetic(m.source) || synthetic(m.metric_name);
    },

    async _loadAgentHosts() {
        try {
            const res = await fetch('/api/agents', { credentials: 'same-origin' });
            if (!res.ok) return;
            const body = await res.json();
            const hosts = new Set();
            for (const a of body.agents || []) {
                if (a.hostname) hosts.add(a.hostname);
            }
            // Treat an empty registry as "unknown" rather than "filter
            // everything out" — otherwise a pre-approval boot would hide
            // all metrics and the operator couldn't see what's flowing in.
            this._knownAgentHosts = hosts.size ? hosts : null;
        } catch {
            // leave as null → permissive
        }
    },

    async load() {
        UIStates.setLoading('metrics', true);
        try {
            const [metrics] = await Promise.all([
                ApiClient.metrics.list(),
                this._loadAgentHosts(),
            ]);
            AppState.metrics = metrics || [];
            this.render();
        } catch (error) {
            console.error('Failed to load metrics:', error);
        } finally {
            UIStates.setLoading('metrics', false);
        }
    },

    render() {
        // Build a key per (host, source, metric) so devices stay separate.
        // Format: "host|source/metric_name" — use "*|" prefix to mean any host.
        const buildKey = (m) => `${m.hostname || '*'}|${m.source}/${m.metric_name}`;
        const buildLabel = (m) =>
            `${m.source} — ${m.metric_name}${m.unit ? ` (${m.unit})` : ''}`;

        // Drop the internal __probe__ pseudo-host and anything not in the
        // current agent registry. The registry is best-effort: if we
        // couldn't fetch it (null), fall back to "anything but __probe__".
        const known = this._knownAgentHosts;
        const visibleMetrics = AppState.metrics.filter(m => {
            if (this.isProbeMetric(m)) return false;
            if (!m.hostname) return true;
            if (known && !known.has(m.hostname)) return false;
            return true;
        });

        // Cascading Device → Metric dropdown for both the dashboard chart and
        // the history tab. Selecting a device narrows the metric list.
        const wireCascade = (deviceSelId, metricSelId) => {
            const devSel = document.getElementById(deviceSelId);
            const metSel = document.getElementById(metricSelId);
            if (!metSel) return;

            const isInitialWire = devSel && !devSel.dataset.cascadeWired;

            // Populate device list (de-duped, sorted)
            const hosts = [...new Set(visibleMetrics.map(m => m.hostname).filter(Boolean))].sort();
            if (devSel) {
                let prevDev = devSel.value;
                // On initial wire only, default the device to llm-systems-manager
                // (when present) so the metric cascade narrows correctly. Don't
                // override on later re-renders or the user's selection is lost.
                if (isInitialWire && !prevDev && hosts.includes('llm-systems-manager')) {
                    prevDev = 'llm-systems-manager';
                }
                devSel.innerHTML = '<option value="">All devices</option>' +
                    hosts.map(h => {
                        const safeH = escapeHtml(h);
                        return `<option value="${safeH}" ${h === prevDev ? 'selected' : ''}>${safeH}</option>`;
                    }).join('');
                devSel.value = prevDev;
            }

            const populateMetrics = () => {
                const host = devSel ? devSel.value : '';
                const matches = host
                    ? visibleMetrics.filter(m => (m.hostname || '') === host)
                    : visibleMetrics;
                const prev = metSel.value;

                const bySource = new Map();
                for (const m of matches) {
                    const list = bySource.get(m.source) || [];
                    list.push(m);
                    bySource.set(m.source, list);
                }
                const sortedSources = [...bySource.keys()].sort();
                const groupsHtml = sortedSources.map(src => {
                    const items = bySource.get(src)
                        .slice()
                        .sort((a, b) => a.metric_name.localeCompare(b.metric_name));
                    const opts = items.map(m => {
                        const key = buildKey(m);
                        return `<option value="${escapeHtml(key)}" ${key === prev ? 'selected' : ''}>${escapeHtml(buildLabel(m))}</option>`;
                    }).join('');
                    return `<optgroup label="${escapeHtml(src)}">${opts}</optgroup>`;
                }).join('');
                metSel.innerHTML = '<option value="">Select metric...</option>' + groupsHtml;

                // Auto-select a preferred metric so chart isn't blank.
                // Prefer (llm-systems-manager, system, cpu_total); fall back to first.
                if (!prev && matches.length > 0) {
                    const preferred = matches.find(m =>
                        m.hostname === 'llm-systems-manager' &&
                        m.source === 'system' &&
                        m.metric_name === 'cpu_total'
                    );
                    const pick = preferred || matches[0];
                    metSel.value = buildKey(pick);
                    metSel.dispatchEvent(new Event('change'));
                }
            };

            // Wire change event ONCE per element (idempotent)
            if (isInitialWire) {
                devSel.addEventListener('change', populateMetrics);
                devSel.dataset.cascadeWired = '1';
            }
            populateMetrics();
        };

        wireCascade('deviceSelect', 'metricSelect');
        wireCascade('historyDeviceSelect', 'historyMetricSelect');

        // Render latest values as metric tiles in the dashboard panel
        const grid = document.getElementById('metricsGrid') || document.getElementById('metrics-overview');
        if (grid) {
            grid.innerHTML = AppState.metrics.map(m => {
                const hostBadge = m.hostname
                    ? `<span class="metric-tile-host" style="opacity:0.7;font-size:0.75em;">${escapeHtml(m.hostname)}</span> `
                    : '';
                return `
                <div class="metric-tile">
                    <div class="metric-tile-name">${hostBadge}${escapeHtml(m.source)} / ${escapeHtml(m.metric_name)}</div>
                    <div class="metric-tile-value">${m.latest_value !== undefined ? Number(m.latest_value).toFixed(2) : '—'} <span class="metric-tile-unit">${escapeHtml(m.unit || '')}</span></div>
                </div>`;
            }).join('');
        }
    },

    handleMetricUpdate(payload) {
        const idx = AppState.metrics.findIndex(m => m.name === payload.name);
        if (idx !== -1) {
            AppState.metrics[idx] = { ...AppState.metrics[idx], ...payload };
        } else {
            AppState.metrics.push(payload);
        }
        this.render();
        DashboardManager.renderStats();
    },
};

// ── Modal Manager ──

const ModalManager = {
    currentCallback: null,
    currentModalId: null,
    _wired: false,

    _wireOnce() {
        if (this._wired) return;
        const closeBtn = document.getElementById('modalCloseBtn');
        const cancelBtn = document.getElementById('modalCancelBtn');
        const confirmBtn = document.getElementById('modalConfirmBtn');
        if (closeBtn) closeBtn.addEventListener('click', () => this.close());
        if (cancelBtn) cancelBtn.addEventListener('click', () => this.close());
        if (confirmBtn) confirmBtn.addEventListener('click', () => this._submit());
        this._wired = true;
    },

    open(modalId, options = {}) {
        this._wireOnce();
        const overlay = document.getElementById('modalOverlay');
        const body = document.getElementById('modalBody');
        const title = document.getElementById('modalTitle');
        if (!overlay || !body) return;

        this.currentCallback = options.onSubmit || null;
        this.currentModalId = modalId;

        const isEdit = !!options.initialData;
        if (modalId === 'create-rule-modal') {
            if (title) title.textContent = options.titleOverride
                || (isEdit ? 'Edit Rule' : 'Create Rule');
            body.innerHTML = this._renderRuleForm();
            this._wireRuleTypeToggle();
            if (isEdit) {
                this._populateRuleForm(options.initialData);
            } else {
                this._wireMetricCascade();
            }
            if (options.initialRuleType) {
                const sel = document.getElementById('form-rule_type');
                if (sel) { sel.value = options.initialRuleType; sel.dispatchEvent(new Event('change')); }
            }
        } else if (modalId === 'create-channel-modal') {
            const isEdit = !!options.initialData;
            if (title) title.textContent = isEdit ? 'Edit Notification Channel' : 'Add Notification Channel';
            body.innerHTML = this._renderChannelForm();
            this._wireChannelTypeToggle();
            if (options.initialChannelType) {
                const sel = document.getElementById('form-ch-type');
                if (sel) { sel.value = options.initialChannelType; sel.dispatchEvent(new Event('change')); }
            }
            if (isEdit) {
                this._populateChannelForm(options.initialData);
            }
        } else if (modalId === 'create-config-modal') {
            const isEdit = !!options.initialData;
            if (title) title.textContent = isEdit ? 'Edit Notification Config' : 'Add Notification Config';
            body.innerHTML = this._renderConfigForm(
                options.channels || [],
                options.catalog || {sources: [], names: [], hosts: []},
            );
            if (isEdit) this._populateConfigForm(options.initialData);
            this._wireToastBehaviorToggle();
        } else {
            if (title) title.textContent = options.title || 'Modal';
            body.innerHTML = options.bodyHtml || '<p>No content</p>';
        }

        UIStates.setModalOpen(true);
    },

    close() {
        UIStates.setModalOpen(false);
        this.currentCallback = null;
        this.currentModalId = null;
    },

    confirm({ title = 'Confirm', message = 'Are you sure?', confirmLabel = 'Confirm', danger = false } = {}) {
        return new Promise(resolve => {
            this._wireOnce();
            const overlay = document.getElementById('modalOverlay');
            const body = document.getElementById('modalBody');
            const titleEl = document.getElementById('modalTitle');
            const confirmBtn = document.getElementById('modalConfirmBtn');
            if (!overlay || !body || !confirmBtn) { resolve(window.confirm(message)); return; }

            const prevLabel = confirmBtn.textContent;
            const prevClass = confirmBtn.className;
            if (titleEl) titleEl.textContent = title;
            body.innerHTML = `<p style="margin:0;line-height:1.5;">${escapeHtml(message)}</p>`;
            confirmBtn.textContent = confirmLabel;
            if (danger) confirmBtn.className = 'btn-danger';

            let settled = false;
            const realClose = this.close.bind(this);
            const finish = (result) => {
                if (settled) return;
                settled = true;
                confirmBtn.textContent = prevLabel;
                confirmBtn.className = prevClass;
                this.close = realClose;
                resolve(result);
            };
            this.currentModalId = 'confirm-dialog';
            this.currentCallback = () => { finish(true); realClose(); };
            this.close = () => { finish(false); realClose(); };
            UIStates.setModalOpen(true);
        });
    },

    _submit() {
        if (!this.currentCallback) {
            this.close();
            return;
        }
        let payload;
        if (this.currentModalId === 'create-rule-modal') {
            try {
                payload = this._collectRuleForm();
            } catch (e) {
                ToastManager.show(e.message || 'Invalid form', 'error');
                return;
            }
        } else if (this.currentModalId === 'create-channel-modal') {
            try {
                payload = this._collectChannelForm();
            } catch (e) {
                ToastManager.show(e.message || 'Invalid form', 'error');
                return;
            }
        } else if (this.currentModalId === 'create-config-modal') {
            try {
                payload = this._collectConfigForm();
            } catch (e) {
                ToastManager.show(e.message || 'Invalid form', 'error');
                return;
            }
        } else {
            payload = this._collectGenericForm();
        }
        this.currentCallback(payload);
    },

    _renderRuleForm() {
        // Build a Device → Source → Metric cascade from live metric data so
        // rules are scoped to a specific host. The metrics endpoint returns
        // one row per (hostname, source, metric_name); we collect the unique
        // hostnames first, then narrow on selection.
        const allMetrics = (AppState.metrics || []).filter(m => !MetricsManager.isProbeMetric(m));
        const known = MetricsManager._knownAgentHosts;
        const liveHosts = [...new Set(allMetrics
            .map(m => m.hostname)
            .filter(h => h && (!known || known.has(h)))
        )].sort();
        const ruleTypes = [
            ['threshold_above', 'Threshold above'],
            ['threshold_below', 'Threshold below'],
            ['threshold_range', 'Threshold range'],
            ['z_score', 'Z-score (anomaly)'],
            ['moving_average', 'Moving average (anomaly)'],
            ['percentile', 'Percentile (anomaly)'],
            ['rate_of_change', 'Rate of change (anomaly)'],
        ];
        const severities = ['info', 'warning', 'critical'];
        const opt = (v, label, sel) => `<option value="${v}"${sel === v ? ' selected' : ''}>${label || v}</option>`;
        return `
        <div class="form-group">
            <label>Name <span class="required">*</span></label>
            <input id="form-name" type="text" placeholder="CPU temperature warning" required>
        </div>
        <div class="form-group">
            <label>Description</label>
            <textarea id="form-description" rows="2" placeholder="Optional description"></textarea>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Device</label>
                <select id="form-source_host">
                    <option value="">Any device</option>
                    ${liveHosts.map(h => `<option value="${escapeHtml(h)}">${escapeHtml(h)}</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>Metric source <span class="required">*</span></label>
                <select id="form-metric_source">
                    <option value="">Select source…</option>
                </select>
            </div>
            <div class="form-group">
                <label>Metric name <span class="required">*</span></label>
                <select id="form-metric_name">
                    <option value="">Select source first…</option>
                </select>
            </div>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Rule type</label>
                <select id="form-rule_type">${ruleTypes.map(([v, l]) => opt(v, l)).join('')}</select>
            </div>
            <div class="form-group">
                <label>Severity</label>
                <select id="form-severity">${severities.map(s => opt(s, s, 'warning')).join('')}</select>
            </div>
        </div>

        <div data-cfg="threshold_above threshold_below threshold_range" class="form-row">
            <div class="form-group">
                <label>Upper threshold</label>
                <input id="form-cfg-upper" type="number" step="any" placeholder="e.g. 80">
            </div>
            <div class="form-group">
                <label>Lower threshold</label>
                <input id="form-cfg-lower" type="number" step="any" placeholder="e.g. 10">
            </div>
            <div class="form-group">
                <label>Unit</label>
                <select id="form-cfg-unit">
                    <option value="">— none —</option>
                    <option value="%">% (percent)</option>
                    <option value="°C">°C (temperature)</option>
                    <option value="Mbps">Mbps (network/disk throughput)</option>
                    <option value="W">W (watts)</option>
                    <option value="tps">tps (tokens/sec)</option>
                    <option value="bytes">bytes</option>
                    <option value="ms">ms (milliseconds)</option>
                    <option value="count">count</option>
                </select>
            </div>
        </div>

        <div data-cfg="z_score" class="form-row">
            <div class="form-group">
                <label>Z-score threshold</label>
                <input id="form-cfg-z-threshold" type="number" step="any" value="3.0">
            </div>
            <div class="form-group">
                <label>Window (minutes)</label>
                <input id="form-cfg-z-window" type="number" min="1" value="60">
            </div>
            <div class="form-group">
                <label>Min data points</label>
                <input id="form-cfg-z-min" type="number" min="1" value="10">
            </div>
        </div>

        <div data-cfg="moving_average" class="form-row">
            <div class="form-group">
                <label>Deviation factor (σ)</label>
                <input id="form-cfg-ma-dev" type="number" step="any" value="2.0">
            </div>
            <div class="form-group">
                <label>Window (minutes)</label>
                <input id="form-cfg-ma-window" type="number" min="1" value="15">
            </div>
            <div class="form-group">
                <label>Min data points</label>
                <input id="form-cfg-ma-min" type="number" min="1" value="5">
            </div>
        </div>

        <div data-cfg="percentile" class="form-row">
            <div class="form-group">
                <label>Percentile</label>
                <input id="form-cfg-pct-p" type="number" min="50" max="99.9" step="0.1" value="95">
            </div>
            <div class="form-group">
                <label>Window (minutes)</label>
                <input id="form-cfg-pct-window" type="number" min="1" value="60">
            </div>
            <div class="form-group">
                <label>Min data points</label>
                <input id="form-cfg-pct-min" type="number" min="1" value="10">
            </div>
        </div>

        <div data-cfg="rate_of_change" class="form-row">
            <div class="form-group">
                <label>Max change / minute</label>
                <input id="form-cfg-roc-max" type="number" step="any" value="10">
            </div>
            <div class="form-group">
                <label>Window (minutes)</label>
                <input id="form-cfg-roc-window" type="number" min="1" value="5">
            </div>
            <div class="form-group">
                <label>Min data points</label>
                <input id="form-cfg-roc-min" type="number" min="2" value="2">
            </div>
        </div>

        <div class="form-row">
            <div class="form-group">
                <label>Auto-resolve after N OK cycles</label>
                <input id="form-auto_resolve_cycles" type="number" min="0" step="1" value="2"
                       title="Close active alerts once the metric stays below threshold for this many consecutive eval cycles. 0 = never auto-resolve (manual close only).">
            </div>
        </div>

        <div class="checkbox-item">
            <input id="form-enabled" type="checkbox" checked>
            <label for="form-enabled">Enabled</label>
        </div>`;
    },

    _wireRuleTypeToggle() {
        const select = document.getElementById('form-rule_type');
        if (!select) return;
        const groups = document.querySelectorAll('#modalBody [data-cfg]');
        const toggle = () => {
            const rt = select.value;
            groups.forEach(g => {
                const types = g.dataset.cfg.split(' ');
                g.style.display = types.includes(rt) ? '' : 'none';
            });
        };
        select.addEventListener('change', toggle);
        toggle();
    },

    _wireMetricCascade(selectedHost, selectedSource, selectedMetric) {
        const hostSel = document.getElementById('form-source_host');
        const srcSel = document.getElementById('form-metric_source');
        const metSel = document.getElementById('form-metric_name');
        if (!srcSel || !metSel) return;

        const filterByHost = (m, host) => !host || (m.hostname || '') === host;
        const allMetrics = () => (AppState.metrics || []).filter(m => !MetricsManager.isProbeMetric(m));

        const populateSources = (host, preselect) => {
            const matches = allMetrics().filter(m => filterByHost(m, host));
            const uniqueSources = [...new Set(matches.map(m => m.source))].sort();
            const fallback = ['cpu', 'gpu', 'ram', 'disk', 'net', 'psu', 'llama'];
            const list = uniqueSources.length > 0 ? uniqueSources : fallback;
            srcSel.innerHTML = '<option value="">Select source…</option>' +
                list.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
            if (preselect) srcSel.value = preselect;
        };

        const populateMetrics = (host, source, preselect) => {
            const matches = allMetrics().filter(m =>
                m.source === source && filterByHost(m, host));
            // When no specific device is selected, deduplicate metric_name across hosts
            // so "cpu/usage_percent" appears once even if both agents report it.
            const seen = new Map();
            matches.forEach(m => {
                if (!seen.has(m.metric_name)) seen.set(m.metric_name, m);
            });
            const unique = [...seen.values()].sort((a, b) =>
                a.metric_name.localeCompare(b.metric_name));
            if (unique.length > 0) {
                metSel.innerHTML = '<option value="">Select metric…</option>' +
                    unique.map(m =>
                        `<option value="${escapeHtml(m.metric_name)}">${escapeHtml(m.metric_name)}${m.unit ? ' (' + escapeHtml(m.unit) + ')' : ''}</option>`
                    ).join('');
            } else {
                metSel.innerHTML = '<option value="">No metrics for this source</option>';
            }
            if (preselect) metSel.value = preselect;
        };

        // Re-populate the source list whenever device changes; clear metric.
        if (hostSel) {
            hostSel.addEventListener('change', () => {
                populateSources(hostSel.value);
                metSel.innerHTML = '<option value="">Select source first…</option>';
            });
        }
        srcSel.addEventListener('change', () =>
            populateMetrics(hostSel ? hostSel.value : '', srcSel.value));

        // Initial population — honour pre-selection from edit form
        if (hostSel && selectedHost) hostSel.value = selectedHost;
        const initialHost = hostSel ? hostSel.value : '';
        populateSources(initialHost, selectedSource);
        if (selectedSource) populateMetrics(initialHost, selectedSource, selectedMetric);
    },

    _populateRuleForm(rule) {
        const set = (id, v) => { const el = document.getElementById(id); if (el != null && v != null) el.value = v; };
        set('form-name', rule.name);
        set('form-description', rule.description);
        // source_host/metric_source/metric_name handled via _wireMetricCascade
        // so each select is populated before we pre-select it.
        this._wireMetricCascade(rule.source_host, rule.metric_source, rule.metric_name);
        set('form-rule_type', rule.rule_type);
        set('form-severity', rule.severity);
        set('form-auto_resolve_cycles', rule.auto_resolve_cycles ?? 2);
        const enabled = document.getElementById('form-enabled');
        if (enabled) enabled.checked = rule.enabled !== false;
        const cfg = rule.config || {};
        if (cfg.threshold) {
            set('form-cfg-upper', cfg.threshold.upper ?? cfg.threshold.value);
            set('form-cfg-lower', cfg.threshold.lower);
            set('form-cfg-unit', cfg.threshold.unit);
        }
        if (cfg.z_score) {
            set('form-cfg-z-threshold', cfg.z_score.threshold);
            set('form-cfg-z-window', cfg.z_score.window_minutes);
            set('form-cfg-z-min', cfg.z_score.min_data_points);
        }
        if (cfg.moving_average) {
            set('form-cfg-ma-dev', cfg.moving_average.deviation_factor);
            set('form-cfg-ma-window', cfg.moving_average.window_minutes);
            set('form-cfg-ma-min', cfg.moving_average.min_data_points);
        }
        if (cfg.percentile) {
            set('form-cfg-pct-p', cfg.percentile.percentile);
            set('form-cfg-pct-window', cfg.percentile.window_minutes);
            set('form-cfg-pct-min', cfg.percentile.min_data_points);
        }
        if (cfg.rate_of_change) {
            set('form-cfg-roc-max', cfg.rate_of_change.max_change_per_minute);
            set('form-cfg-roc-window', cfg.rate_of_change.window_minutes);
            set('form-cfg-roc-min', cfg.rate_of_change.min_data_points);
        }
        this._wireRuleTypeToggle();
    },

    _num(id) {
        const el = document.getElementById(id);
        if (!el || el.value === '') return null;
        const n = Number(el.value);
        return Number.isFinite(n) ? n : null;
    },

    _str(id) {
        const el = document.getElementById(id);
        const v = el ? el.value.trim() : '';
        return v || null;
    },

    _collectRuleForm() {
        const name = this._str('form-name');
        const source_host = this._str('form-source_host');  // null = any device
        const metric_source = this._str('form-metric_source');
        const metric_name = this._str('form-metric_name');
        const rule_type = this._str('form-rule_type');
        const severity = this._str('form-severity') || 'warning';
        const description = this._str('form-description');
        const enabledEl = document.getElementById('form-enabled');
        const enabled = enabledEl ? enabledEl.checked : true;
        if (!name) throw new Error('Name is required');
        if (!metric_source || !metric_name) throw new Error('Metric source and name are required');

        const config = {};
        if (rule_type === 'threshold_above' || rule_type === 'threshold_below' || rule_type === 'threshold_range') {
            const upper = this._num('form-cfg-upper');
            const lower = this._num('form-cfg-lower');
            const unit = this._str('form-cfg-unit');
            const t = {};
            if (upper != null) { t.upper = upper; t.value = upper; }
            if (lower != null) t.lower = lower;
            if (unit) t.unit = unit;
            if (rule_type === 'threshold_above' && upper == null) throw new Error('Upper threshold is required');
            if (rule_type === 'threshold_below' && lower == null) throw new Error('Lower threshold is required');
            if (rule_type === 'threshold_range' && (upper == null || lower == null)) throw new Error('Both upper and lower thresholds are required');
            config.threshold = t;
        } else if (rule_type === 'z_score') {
            config.z_score = {
                threshold: this._num('form-cfg-z-threshold') ?? 3.0,
                window_minutes: this._num('form-cfg-z-window') ?? 60,
                min_data_points: this._num('form-cfg-z-min') ?? 10,
            };
        } else if (rule_type === 'moving_average') {
            config.moving_average = {
                deviation_factor: this._num('form-cfg-ma-dev') ?? 2.0,
                window_minutes: this._num('form-cfg-ma-window') ?? 15,
                min_data_points: this._num('form-cfg-ma-min') ?? 5,
            };
        } else if (rule_type === 'percentile') {
            config.percentile = {
                percentile: this._num('form-cfg-pct-p') ?? 95.0,
                window_minutes: this._num('form-cfg-pct-window') ?? 60,
                min_data_points: this._num('form-cfg-pct-min') ?? 10,
            };
        } else if (rule_type === 'rate_of_change') {
            config.rate_of_change = {
                max_change_per_minute: this._num('form-cfg-roc-max') ?? 10.0,
                window_minutes: this._num('form-cfg-roc-window') ?? 5,
                min_data_points: this._num('form-cfg-roc-min') ?? 2,
            };
        }

        const arcRaw = this._num('form-auto_resolve_cycles');
        const auto_resolve_cycles = arcRaw != null && arcRaw >= 0 ? Math.floor(arcRaw) : 2;

        return {
            name, description, source_host, metric_source, metric_name,
            rule_type, severity, enabled, config, auto_resolve_cycles,
        };
    },

    _renderChannelForm() {
        return `
        <div class="form-group">
            <label>Channel name <span class="required">*</span></label>
            <input id="form-ch-name" type="text" placeholder="My email alert">
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>Channel type</label>
                <select id="form-ch-type">
                    <option value="toast">Toast (browser popup)</option>
                    <option value="email">Email</option>
                    <option value="sms">SMS</option>
                    <option value="webhook">Webhook</option>
                    <option value="discord">Discord</option>
                </select>
            </div>
        </div>
        <div data-ch-type="email" class="form-row">
            <div class="form-group">
                <label>To email <span class="required">*</span></label>
                <input id="form-ch-email-to" type="email" placeholder="you@example.com">
            </div>
            <div class="form-group">
                <label>Subject prefix</label>
                <input id="form-ch-email-prefix" type="text" value="[ALARM]">
            </div>
        </div>
        <div data-ch-type="sms" class="form-group">
            <label>Phone number <span class="required">*</span></label>
            <input id="form-ch-sms-to" type="text" placeholder="+15551234567">
        </div>
        <div data-ch-type="webhook">
            <div class="form-group">
                <label>Webhook URL <span class="required">*</span></label>
                <input id="form-ch-wh-url" type="url" placeholder="https://example.com/hook">
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>Method</label>
                    <select id="form-ch-wh-method">
                        <option value="POST">POST</option>
                        <option value="PUT">PUT</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Secret (optional)</label>
                    <input id="form-ch-wh-secret" type="text" placeholder="signing secret">
                </div>
            </div>
        </div>
        <div data-ch-type="discord">
            <div class="form-group">
                <label>Discord webhook URL <span class="required">*</span></label>
                <input id="form-ch-dc-url" type="url" placeholder="https://discord.com/api/webhooks/...">
            </div>
            <div class="form-group">
                <label>Bot username (optional)</label>
                <input id="form-ch-dc-user" type="text" placeholder="Alarm Bot">
            </div>
        </div>
        <div class="checkbox-item">
            <input id="form-ch-enabled" type="checkbox" checked>
            <label for="form-ch-enabled">Enabled</label>
        </div>`;
    },

    _wireChannelTypeToggle() {
        const select = document.getElementById('form-ch-type');
        if (!select) return;
        const groups = document.querySelectorAll('#modalBody [data-ch-type]');
        const toggle = () => {
            const ct = select.value;
            groups.forEach(g => { g.style.display = g.dataset.chType === ct ? '' : 'none'; });
        };
        select.addEventListener('change', toggle);
        toggle();
    },

    _populateChannelForm(channel) {
        // Pre-fill the channel form from an existing channel object so the
        // operator can edit it via PUT /channels/{id}. channel.config has
        // type-specific keys we map to the same form-ch-* inputs that
        // _collectChannelForm reads.
        if (!channel) return;
        const nameEl = document.getElementById('form-ch-name');
        const typeEl = document.getElementById('form-ch-type');
        const enabledEl = document.getElementById('form-ch-enabled');
        if (nameEl) nameEl.value = channel.name || '';
        if (typeEl && channel.channel_type) {
            typeEl.value = channel.channel_type;
            typeEl.dispatchEvent(new Event('change'));
        }
        if (enabledEl) enabledEl.checked = channel.enabled !== false;
        const cfg = channel.config || {};
        // config can be a JSON string (from InfluxDB round-trip) or a dict.
        let cfgData = cfg;
        if (typeof cfg === 'string') {
            try { cfgData = JSON.parse(cfg); } catch (_) { cfgData = {}; }
        }
        const setIf = (id, v) => {
            const el = document.getElementById(id);
            if (el && v !== undefined && v !== null) el.value = v;
        };
        // Email
        setIf('form-ch-email-to', cfgData.email?.to_email);
        setIf('form-ch-email-prefix', cfgData.email?.subject_prefix);
        // SMS
        setIf('form-ch-sms-to', cfgData.sms?.to_number);
        // Webhook
        setIf('form-ch-wh-url', cfgData.webhook?.url);
        setIf('form-ch-wh-method', cfgData.webhook?.method);
        setIf('form-ch-wh-secret', cfgData.webhook?.secret);
        // Discord
        setIf('form-ch-dc-url', cfgData.discord?.webhook_url);
        setIf('form-ch-dc-user', cfgData.discord?.username);
    },

    _collectChannelForm() {
        const name = (document.getElementById('form-ch-name')?.value || '').trim();
        if (!name) throw new Error('Channel name is required');
        const channel_type = document.getElementById('form-ch-type')?.value || 'toast';
        const enabled = document.getElementById('form-ch-enabled')?.checked !== false;
        const config = {};
        if (channel_type === 'toast') {
            config.toast = { enabled: true };
        } else if (channel_type === 'email') {
            const to_email = (document.getElementById('form-ch-email-to')?.value || '').trim();
            if (!to_email) throw new Error('Email address is required');
            config.email = { to_email, subject_prefix: document.getElementById('form-ch-email-prefix')?.value || '[ALARM]' };
        } else if (channel_type === 'sms') {
            const to_number = (document.getElementById('form-ch-sms-to')?.value || '').trim();
            if (!to_number) throw new Error('Phone number is required');
            config.sms = { to_number };
        } else if (channel_type === 'webhook') {
            const url = (document.getElementById('form-ch-wh-url')?.value || '').trim();
            if (!url) throw new Error('Webhook URL is required');
            const secret = (document.getElementById('form-ch-wh-secret')?.value || '').trim() || null;
            config.webhook = { url, method: document.getElementById('form-ch-wh-method')?.value || 'POST', headers: {}, secret };
        } else if (channel_type === 'discord') {
            const webhook_url = (document.getElementById('form-ch-dc-url')?.value || '').trim();
            if (!webhook_url) throw new Error('Discord webhook URL is required');
            const username = (document.getElementById('form-ch-dc-user')?.value || '').trim() || null;
            config.discord = { webhook_url, username };
        }
        return { name, channel_type, config, enabled };
    },

    async _fetchPolicyCatalog() {
        // Build the {sources, names, hosts} option lists for the filter
        // dropdowns from /api/alarm/metrics. Returns empty arrays on failure
        // so the form still renders (operator can save with no filters).
        try {
            // Refresh agent registry so the hosts filter matches what the
            // rules editor shows. Best-effort: a failure leaves it permissive.
            await MetricsManager._loadAgentHosts();
            const known = MetricsManager._knownAgentHosts;
            const metrics = await ApiClient.metrics.list();
            const sources = new Set(), names = new Set(), hosts = new Set();
            for (const m of (metrics || [])) {
                if (MetricsManager.isProbeMetric(m)) continue;
                if (m.source) sources.add(m.source);
                if (m.metric_name) names.add(m.metric_name);
                if (m.hostname && (!known || known.has(m.hostname))) {
                    hosts.add(m.hostname);
                }
            }
            return {
                sources: [...sources].sort(),
                names:   [...names].sort(),
                hosts:   [...hosts].sort(),
            };
        } catch (e) {
            console.warn('catalog fetch failed:', e);
            return { sources: [], names: [], hosts: [] };
        }
    },

    _renderMultiSelect(id, options, hint) {
        // <select multiple> + a small "selected count" indicator. Ctrl/Cmd-
        // click toggles individual options.
        const opts = options.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
        return `
            <div class="form-group">
                <label for="${id}">${hint}</label>
                <select id="${id}" multiple size="${Math.min(Math.max(options.length, 3), 8)}"
                        style="width:100%;min-height:5em;">
                    ${opts}
                </select>
                <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                    Ctrl-click (or Cmd-click on macOS) to toggle individual entries. Leave nothing selected = match all.
                </span>
            </div>`;
    },

    _renderConfigForm(channels, catalog = {sources: [], names: [], hosts: []}) {
        const channelList = channels.length
            ? channels.map(ch => `
                <div class="checkbox-item">
                    <input id="form-cfg-ch-${escapeHtml(ch.channel_id)}" type="checkbox" value="${escapeHtml(ch.channel_id)}"
                           data-channel-type="${escapeHtml(ch.channel_type)}">
                    <label for="form-cfg-ch-${escapeHtml(ch.channel_id)}">${escapeHtml(ch.name)} <span class="badge">${escapeHtml(ch.channel_type)}</span></label>
                </div>`).join('')
            : '<p class="hint">No channels configured yet. Add a channel first.</p>';
        return `
        <div class="form-group">
            <label>Policy name <span class="required">*</span></label>
            <input id="form-cfg-name" type="text" placeholder="Critical GPU alerts">
        </div>
        <div class="form-group">
            <label>Description</label>
            <textarea id="form-cfg-desc" rows="2" placeholder="Optional description"></textarea>
        </div>
        <div class="form-group">
            <label>Channels</label>
            <div id="form-cfg-channels">${channelList}</div>
        </div>
        <fieldset class="form-group" style="border:1px solid var(--border,#374151);padding:0.75rem;border-radius:6px;">
            <legend style="padding:0 0.5rem;">Filters <span class="hint" style="font-weight:normal;color:var(--text-muted,#9ca3af);">— policy fires only when ALL filters pass. Empty filter = match all.</span></legend>

            <div class="form-group">
                <label for="form-cfg-min-severity">Minimum severity</label>
                <select id="form-cfg-min-severity">
                    <option value="">(any — no severity filter)</option>
                    <option value="info">info</option>
                    <option value="warning">warning</option>
                    <option value="critical">critical</option>
                </select>
                <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                    Alerts below this level are skipped by this policy.
                </span>
            </div>

            ${this._renderMultiSelect('form-cfg-metric-sources', catalog.sources, 'Metric sources')}
            ${this._renderMultiSelect('form-cfg-metric-names',   catalog.names,   'Metric names')}
            ${this._renderMultiSelect('form-cfg-source-hosts',   catalog.hosts,   'Source hosts')}
        </fieldset>

        <fieldset class="form-group" style="border:1px solid var(--border,#374151);padding:0.75rem;border-radius:6px;">
            <legend style="padding:0 0.5rem;">Delivery</legend>

            <div class="form-group">
                <label for="form-cfg-repeat-interval">Repeat interval (minutes)</label>
                <input id="form-cfg-repeat-interval" type="number" min="0" step="1" value="30">
                <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                    Minimum time between consecutive notifications for the same alert. 0 = no rate limit (every cycle while firing).
                </span>
            </div>

            <div class="form-group">
                <label for="form-cfg-min-alarm-count">Minimum alarm count before notifying</label>
                <input id="form-cfg-min-alarm-count" type="number" min="1" step="1" value="1">
                <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                    Defer the first notification until the rule has been firing for this many consecutive evaluation cycles. Use 1 to fire on the first breach.
                </span>
            </div>

            <div class="checkbox-item">
                <input id="form-cfg-notify-on-clear" type="checkbox">
                <label for="form-cfg-notify-on-clear">
                    Send a notification when the alarm clears
                    <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                        Only fires for alerts this policy actually notified on.
                    </span>
                </label>
            </div>
        </fieldset>

        <div class="form-group" id="form-cfg-toast-behavior" style="display:none;">
            <label>Toast behavior</label>
            <div class="checkbox-item">
                <input id="form-cfg-auto-dismiss" type="checkbox" checked>
                <label for="form-cfg-auto-dismiss">
                    Auto-dismiss after 10 seconds
                    <span class="hint" style="display:block;font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:2px;">
                        Uncheck to make toast notifications sticky (require manual dismiss).
                    </span>
                </label>
            </div>
        </div>
        <div class="checkbox-item">
            <input id="form-cfg-enabled" type="checkbox" checked>
            <label for="form-cfg-enabled">Enabled</label>
        </div>`;
    },

    _setMultiSelectValues(id, values) {
        const el = document.getElementById(id);
        if (!el) return;
        const wanted = new Set((values || []).map(String));
        // First mark existing options that match.
        const existing = new Set();
        for (const opt of el.options) {
            opt.selected = wanted.has(opt.value);
            existing.add(opt.value);
        }
        // Then synthesize options for any saved value not in the current catalog
        // (e.g. an agent that hasn't pushed recently) so they remain selectable.
        for (const v of wanted) {
            if (!existing.has(v)) {
                const opt = document.createElement('option');
                opt.value = v;
                opt.text = `${v} (not currently reporting)`;
                opt.selected = true;
                el.add(opt);
            }
        }
    },

    _wireToastBehaviorToggle() {
        const wrap = document.getElementById('form-cfg-toast-behavior');
        const container = document.getElementById('form-cfg-channels');
        if (!wrap || !container) return;
        const sync = () => {
            const hasToast = !!container.querySelector(
                'input[type="checkbox"][data-channel-type="toast"]:checked'
            );
            wrap.style.display = hasToast ? '' : 'none';
        };
        container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', sync);
        });
        sync();
    },

    _populateConfigForm(cfg) {
        const nameEl = document.getElementById('form-cfg-name');
        const descEl = document.getElementById('form-cfg-desc');
        const enabledEl = document.getElementById('form-cfg-enabled');
        const autoDismissEl = document.getElementById('form-cfg-auto-dismiss');
        const minSevEl = document.getElementById('form-cfg-min-severity');
        const repeatEl = document.getElementById('form-cfg-repeat-interval');
        const minCountEl = document.getElementById('form-cfg-min-alarm-count');
        const clearEl = document.getElementById('form-cfg-notify-on-clear');
        if (nameEl) nameEl.value = cfg.name || '';
        if (descEl) descEl.value = cfg.description || '';
        if (enabledEl) enabledEl.checked = cfg.enabled !== false;
        if (autoDismissEl) autoDismissEl.checked = cfg.auto_dismiss !== false;
        if (minSevEl) minSevEl.value = cfg.min_severity || '';
        if (repeatEl) repeatEl.value = cfg.repeat_interval_minutes ?? 30;
        if (minCountEl) minCountEl.value = cfg.min_alarm_count ?? 1;
        if (clearEl) clearEl.checked = !!cfg.notify_on_clear;
        this._setMultiSelectValues('form-cfg-metric-sources', cfg.metric_sources);
        this._setMultiSelectValues('form-cfg-metric-names',   cfg.metric_names);
        this._setMultiSelectValues('form-cfg-source-hosts',   cfg.source_hosts);
        (cfg.channels || []).forEach(chId => {
            const cb = document.querySelector(`#form-cfg-channels input[value="${chId}"]`);
            if (cb) cb.checked = true;
        });
    },

    _collectConfigForm() {
        const name = (document.getElementById('form-cfg-name')?.value || '').trim();
        if (!name) throw new Error('Policy name is required');
        const description = (document.getElementById('form-cfg-desc')?.value || '').trim() || null;
        const enabled = document.getElementById('form-cfg-enabled')?.checked !== false;
        const auto_dismiss = document.getElementById('form-cfg-auto-dismiss')?.checked !== false;
        const channels = Array.from(
            document.querySelectorAll('#form-cfg-channels input[type="checkbox"]:checked')
        ).map(cb => cb.value);

        const collectMulti = (id) => {
            const el = document.getElementById(id);
            if (!el) return [];
            return [...el.selectedOptions].map(o => o.value).filter(Boolean);
        };
        const min_severity = (document.getElementById('form-cfg-min-severity')?.value || '').trim() || null;
        const metric_sources = collectMulti('form-cfg-metric-sources');
        const metric_names   = collectMulti('form-cfg-metric-names');
        const source_hosts   = collectMulti('form-cfg-source-hosts');

        const repeatRaw = document.getElementById('form-cfg-repeat-interval')?.value;
        const repeat_interval_minutes = Math.max(0, parseInt(repeatRaw, 10) || 0);
        const minCountRaw = document.getElementById('form-cfg-min-alarm-count')?.value;
        const min_alarm_count = Math.max(1, parseInt(minCountRaw, 10) || 1);
        const notify_on_clear = !!document.getElementById('form-cfg-notify-on-clear')?.checked;

        return {
            name, description, channels, enabled, auto_dismiss,
            min_severity, metric_sources, metric_names, source_hosts,
            repeat_interval_minutes, min_alarm_count, notify_on_clear,
        };
    },

    _collectGenericForm() {
        const data = {};
        document.querySelectorAll('#modalOverlay input, #modalOverlay select, #modalOverlay textarea').forEach(input => {
            if (input.id && input.id.startsWith('form-')) {
                data[input.id.replace('form-', '')] = input.type === 'checkbox' ? input.checked : input.value;
            }
        });
        return data;
    },
};

// ── Tab Manager ──

const TabManager = {
    init() {
        const tabs = document.querySelectorAll('.tab[data-tab]');
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                const tabName = tab.dataset.tab;
                this.switchTab(tabName);
            });
        });
        // Sub-tab navigation within a top-level panel. Currently only the
        // Events (dashboard) panel uses this. Sub-tab buttons live alongside
        // their sub-panels in the same parent .tab-panel.
        document.querySelectorAll('.subtab[data-subtab]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.switchSubtab(btn.closest('.tab-panel')?.id || '', btn.dataset.subtab);
            });
        });
    },

    _alertsPollTimer: null,
    _metricsPollTimer: null,
    _activeSubtabs: { dashboard: 'overview' },

    _stopMetricsPoll() {
        if (this._metricsPollTimer) {
            clearInterval(this._metricsPollTimer);
            this._metricsPollTimer = null;
        }
    },

    _startMetricsPoll() {
        this._stopMetricsPoll();
        // Refresh the metrics sub-tab every 60s while it's the active view.
        // Mirrors _alertsPollTimer — only ticks while still on the right tab.
        this._metricsPollTimer = setInterval(async () => {
            if (AppState.currentTab !== 'dashboard' ||
                this._activeSubtabs.dashboard !== 'metrics') {
                return;
            }
            try {
                await MetricsManager.load();
                await this.loadHistory();
            } finally {
                updateLastRefreshed();
            }
        }, 60000);
    },

    switchSubtab(panelId, subtabName) {
        // panelId is e.g. "dashboard-panel"; tab group key is "dashboard"
        const group = panelId.replace(/-panel$/, '');
        this._activeSubtabs[group] = subtabName;
        const root = document.getElementById(panelId);
        if (!root) return;
        root.querySelectorAll('.subtab[data-subtab]').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.subtab === subtabName);
        });
        root.querySelectorAll('.subtab-panel').forEach(panel => {
            panel.classList.toggle('active', panel.id === `${group}-sub-${subtabName}`);
        });
        // When the Metrics sub-tab becomes active, kick the history loaders
        // so the charts render with current data, and start the 60s poll.
        // Stop the poll when leaving Metrics for another sub-tab.
        if (group === 'dashboard') {
            if (subtabName === 'metrics') {
                this.loadHistory();
                this._startMetricsPoll();
            } else {
                this._stopMetricsPoll();
            }
        }
    },

    switchTab(tabName) {
        AppState.currentTab = tabName;

        // Update tab buttons
        document.querySelectorAll('.tab[data-tab]').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });

        // Update tab panels
        document.querySelectorAll('.tab-panel').forEach(panel => {
            panel.classList.toggle('active', panel.id === `${tabName}-panel`);
        });

        // Stop the alerts poll when leaving the alerts tab; start when entering.
        // WebSocket push covers most updates instantly — the poll closes any
        // gap during reconnects or missed events without spamming the server.
        if (this._alertsPollTimer) {
            clearInterval(this._alertsPollTimer);
            this._alertsPollTimer = null;
        }
        if (tabName === 'alerts') {
            this._alertsPollTimer = setInterval(() => {
                if (AppState.currentTab === 'alerts') AlertManager.load();
            }, 15000);
        }

        // Metrics sub-tab poll only runs while Dashboard › Metrics is active.
        // Stop unconditionally on any tab switch; re-arm below when relevant.
        this._stopMetricsPoll();

        // Load data for the tab
        switch (tabName) {
            case 'dashboard':
                DashboardManager.init();
                // If the user was last on the Metrics sub-tab, fire its loader
                // and re-arm the 60s refresh poll.
                if (this._activeSubtabs.dashboard === 'metrics') {
                    this.loadHistory();
                    this._startMetricsPoll();
                }
                break;
            case 'alerts':
                AlertManager.load();
                break;
            case 'rules':
                RuleManager.load();
                break;
            case 'notifications':
                this.loadNotifications();
                break;
        }
    },

    _renderDeliveriesPagination(total, page, pageSize, totalPages) {
        const range = document.getElementById('deliveriesPaginationRange');
        const pages = document.getElementById('deliveriesPagePages');
        const prev = document.getElementById('deliveriesPagePrev');
        const next = document.getElementById('deliveriesPageNext');
        const sizeSel = document.getElementById('deliveriesPageSize');
        if (!range || !pages || !prev || !next || !sizeSel) return;

        if (total === 0) {
            range.textContent = '0 records';
            pages.textContent = 'Page 0 / 0';
        } else if (pageSize <= 0) {
            range.textContent = `Showing all ${total}`;
            pages.textContent = 'Page 1 / 1';
        } else {
            const start = (page - 1) * pageSize + 1;
            const end = Math.min(total, page * pageSize);
            range.textContent = `${start}–${end} of ${total}`;
            pages.textContent = `Page ${page} / ${totalPages}`;
        }
        prev.disabled = page <= 1 || pageSize <= 0;
        next.disabled = page >= totalPages || pageSize <= 0;

        if (!sizeSel.dataset.wired) {
            sizeSel.dataset.wired = '1';
            sizeSel.addEventListener('change', () => {
                AppState.filters.deliveriesPage.pageSize = parseInt(sizeSel.value, 10) || 10;
                AppState.filters.deliveriesPage.page = 1;
                this.renderNotifications();
            });
            prev.addEventListener('click', () => {
                if (AppState.filters.deliveriesPage.page > 1) {
                    AppState.filters.deliveriesPage.page -= 1;
                    this.renderNotifications();
                }
            });
            next.addEventListener('click', () => {
                AppState.filters.deliveriesPage.page += 1;
                this.renderNotifications();
            });
        }
    },

    async loadNotifications() {
        try {
            const [methods, rules, deliveries] = await Promise.all([
                ApiClient.notifications.listMethods(),
                ApiClient.notifications.listRules(),
                ApiClient.notifications.getHistory({ limit: 100 }).catch(() => []),
            ]);
            AppState.notifications = {
                methods:    methods    || [],
                configs:    rules      || [],
                deliveries: deliveries || [],
            };
            this.renderNotifications();
        } catch (error) {
            ToastManager.show('Failed to load notifications', 'error');
        }
    },

    renderNotifications() {
        const CH_META = {
            toast:   { icon: '🔔', label: 'Toast' },
            email:   { icon: '📧', label: 'Email' },
            sms:     { icon: '📱', label: 'SMS' },
            webhook: { icon: '🔗', label: 'Webhook' },
            discord: { icon: '💬', label: 'Discord' },
        };

        const channelsList = document.getElementById('channelsList');
        if (channelsList) {
            if (AppState.notifications.methods.length === 0) {
                channelsList.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">📡</div>
                        <p>No channels configured — click a type above or <strong>+ Add Channel</strong></p>
                    </div>`;
            } else {
                channelsList.innerHTML = AppState.notifications.methods.map(m => {
                    const id = m.channel_id || m.id || '';
                    const type = (m.channel_type || m.type || 'unknown').toLowerCase();
                    const name = m.name || m.target || 'Unnamed';
                    const enabled = m.enabled !== false;
                    const meta = CH_META[type] || { icon: '📡', label: type.toUpperCase() };

                    // Extract readable target/recipient from config
                    let target = '';
                    if (m.config) {
                        const cfg = m.config[type] || m.config;
                        target = cfg.to_email || cfg.to_number || cfg.url || cfg.webhook_url || '';
                    }

                    const safeId = escapeHtml(id);
                    const safeType = escapeHtml(type);
                    return `
                    <div class="ch-card ch-${safeType}">
                        <div class="ch-card-stripe"></div>
                        <div class="ch-card-body">
                            <div class="ch-type-icon">${escapeHtml(meta.icon)}</div>
                            <div class="ch-info">
                                <div class="ch-name">${escapeHtml(name)}</div>
                                <div class="ch-meta">
                                    <span class="ch-type-label">${escapeHtml(meta.label)}</span>
                                    ${target ? `<span class="ch-target">${escapeHtml(target)}</span>` : ''}
                                    <span class="ch-status ${enabled ? 'enabled' : 'disabled'}">${enabled ? 'Enabled' : 'Disabled'}</span>
                                </div>
                            </div>
                            <div class="ch-actions">
                                <button class="btn-test" onclick="NotificationsTester.test('${safeId}')">▷ Test</button>
                                <button class="btn btn-sm btn-secondary" onclick="NotificationsTester.editChannel('${safeId}')">Edit</button>
                                <button class="btn btn-sm btn-danger" onclick="NotificationsTester.delete('${safeId}')">Delete</button>
                            </div>
                        </div>
                    </div>`;
                }).join('');
            }
        }

        // Delivery history table (paginated)
        const tbody = document.getElementById('deliveryTableBody');
        if (tbody) {
            const deliveries = AppState.notifications.deliveries || [];
            const { pageSize } = AppState.filters.deliveriesPage;
            const totalPages = pageSize > 0 ? Math.max(1, Math.ceil(deliveries.length / pageSize)) : 1;
            if (AppState.filters.deliveriesPage.page > totalPages) {
                AppState.filters.deliveriesPage.page = totalPages;
            }
            const page = AppState.filters.deliveriesPage.page;
            const slice = pageSize > 0
                ? deliveries.slice((page - 1) * pageSize, page * pageSize)
                : deliveries;
            if (deliveries.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No delivery history — send a test notification to see records here</td></tr>';
            } else {
                const CH_META = { toast: '🔔', email: '📧', sms: '📱', webhook: '🔗', discord: '💬' };
                tbody.innerHTML = slice.map(d => {
                    const ts = d.delivered_at
                        ? (parseTs(d.delivered_at)?.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' }) ?? '—')
                        : '—';
                    const type = (d.channel_type || '').toLowerCase();
                    const icon = CH_META[type] || '📡';
                    const channel = `${icon} ${type || '—'}`;
                    const recipient = d.recipient || '—';
                    const ok = d.success !== false && String(d.success).toLowerCase() !== 'false';
                    const statusClass = ok ? 'sent' : 'failed';
                    const statusText  = ok ? 'Sent' : 'Failed';
                    const detail = d.error_message || d.title || '';
                    return `<tr>
                        <td style="white-space:nowrap;color:var(--text-secondary);font-size:0.8rem">${escapeHtml(ts)}</td>
                        <td>${escapeHtml(channel)}</td>
                        <td style="font-family:var(--font-mono);font-size:0.78rem;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(recipient)}</td>
                        <td><span class="delivery-status ${statusClass}">${escapeHtml(statusText)}</span></td>
                        <td style="font-size:0.8rem;color:var(--text-secondary);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(detail)}">${escapeHtml(detail)}</td>
                    </tr>`;
                }).join('');
            }
            this._renderDeliveriesPagination(deliveries.length, page, pageSize, totalPages);
        }

        const configsList = document.getElementById('configsList');
        if (configsList) {
            if (AppState.notifications.configs.length === 0) {
                configsList.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">⚙️</div>
                        <p>No configs yet — click <strong>+ Add Config</strong> to route alerts to channels</p>
                    </div>`;
            } else {
                configsList.innerHTML = AppState.notifications.configs.map(r => {
                    const id = r.config_id || r.id || '';
                    const channelCount = (r.channels || []).length;
                    const enabled = r.enabled !== false;
                    const autoDismiss = r.auto_dismiss !== false;
                    const desc = r.description || '';
                    const safeId = escapeHtml(id);
                    return `
                    <div class="cfg-card">
                        <div class="cfg-card-body">
                            <div class="cfg-icon">⚙️</div>
                            <div class="cfg-info">
                                <div class="cfg-name">${escapeHtml(r.name)}</div>
                                ${desc ? `<div class="cfg-description">${escapeHtml(desc)}</div>` : ''}
                                <div class="cfg-badges">
                                    <span class="cfg-badge channels">📡 ${channelCount} channel${channelCount === 1 ? '' : 's'}</span>
                                    <span class="cfg-badge ${enabled ? 'enabled' : 'disabled'}">${enabled ? '● Active' : '○ Disabled'}</span>
                                    <span class="cfg-badge ${autoDismiss ? 'auto-dismiss' : 'sticky'}">${autoDismiss ? '↩ Auto-dismiss' : '📌 Sticky'}</span>
                                </div>
                            </div>
                            <div class="cfg-actions">
                                <button class="btn btn-sm btn-secondary" onclick="NotificationsTester.editConfig('${safeId}')">Edit</button>
                                <button class="btn btn-sm btn-danger" onclick="NotificationsTester.deleteConfig('${safeId}')">Delete</button>
                            </div>
                        </div>
                    </div>`;
                }).join('');
            }
        }
    },

    async loadHistory() {
        if (AppState.metrics.length === 0) {
            await MetricsManager.load();
        }
        // Cascade is populated by MetricsManager.render() via wireCascade —
        // we just trigger chart load for whatever the current selection is.
        const sel = document.getElementById('historyMetricSelect');
        const key = sel?.value;
        const range = parseInt(document.getElementById('historyRangeSelect')?.value || '1440', 10);
        if (key) ChartManager.loadHistory(key, range);
    },
};

// ── Chart Manager ──

// x-axis wheel-zoom + drag-pan bounded to data range; double-click resets
// (wired at end of file). Touch/pinch needs Hammer.js (not vendored).
const _zoomOpts = {
    zoom: { wheel: { enabled: true }, drag: { enabled: false }, mode: 'x' },
    pan: { enabled: true, mode: 'x' },
    limits: { x: { min: 'original', max: 'original' } },
};

const ChartManager = {
    _mainChart: null,
    _historyChart: null,
    _trendChart: null,

    // WeakMap cache so we compute "first-of-day" indices once per ticks
    // array (callback fires once per tick during a single render).
    _firstOfDayCache: new WeakMap(),

    _firstOfDayIndices(ticks) {
        const cached = this._firstOfDayCache.get(ticks);
        if (cached) return cached;
        const set = new Set();
        let lastDay = null;
        for (let i = 0; i < ticks.length; i++) {
            const d = new Date(Number(ticks[i].value));
            const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
            if (key !== lastDay) {
                set.add(i);
                lastDay = key;
            }
        }
        this._firstOfDayCache.set(ticks, set);
        return set;
    },

    // Tick callback: time-of-day always; date label appended only on the
    // first tick of each calendar day, and only when the visible span
    // exceeds 24 hours. Returning a string[] gives a two-line tick.
    _tickFormatter(value, index, ticks) {
        const d = new Date(Number(value));
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        const time = `${hh}:${mm}`;

        const spanMs = ticks.length > 1
            ? (Number(ticks[ticks.length - 1].value) - Number(ticks[0].value))
            : 0;
        if (spanMs <= 24 * 60 * 60 * 1000) return time;

        const firstOfDay = ChartManager._firstOfDayIndices(ticks);
        if (!firstOfDay.has(index)) return time;
        const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        return [time, date];
    },

    _timeAxis() {
        return {
            type: 'time',
            time: { tooltipFormat: 'MMM d, yyyy HH:mm:ss' },
            ticks: {
                color: '#94a3b8',
                source: 'auto',
                autoSkip: true,
                autoSkipPadding: 24,
                maxTicksLimit: 10,
                maxRotation: 0,
                callback: ChartManager._tickFormatter,
            },
            grid: { color: '#334155' },
        };
    },

    // Annotation lines for a metric chart, via the shared thresholds module.
    // hostWildcard: a null host (console "any host") matches any source_host.
    _thresholdAnnotations(host, source, metricName) {
        if (!window.Thresholds) return {};
        return Thresholds.thresholdAnnotations(AppState.rules, { source, metricName, host, hostWildcard: true });
    },

    // Re-apply threshold lines to live main/history charts (e.g. after rules
    // load post-render). Reads each chart's stored metric identity.
    refreshAnnotations() {
        [this._mainChart, this._historyChart].forEach(c => {
            if (!c || !c.$ident) return;
            try {
                c.options.plugins = c.options.plugins || {};
                c.options.plugins.annotation = { annotations: this._thresholdAnnotations(c.$ident.host, c.$ident.source, c.$ident.metricName) };
                c.update('none');
            } catch (_) {}
        });
    },

    _chartConfig(label, unit, points, ident) {
        const data = points.map(p => ({ x: new Date(p.timestamp), y: p.value }));
        return {
            type: 'line',
            data: {
                datasets: [{
                    label: `${label}${unit ? ' (' + unit + ')' : ''}`,
                    data,
                    borderColor: '#6366f1',
                    backgroundColor: 'rgba(99,102,241,0.1)',
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHitRadius: 10,
                    pointBackgroundColor: '#6366f1',
                    fill: true,
                    tension: 0.3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: this._timeAxis(),
                    y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                },
                plugins: {
                    legend: { labels: { color: '#f1f5f9' } },
                    tooltip: { mode: 'index', intersect: false },
                    zoom: _zoomOpts,
                    annotation: { annotations: ident ? this._thresholdAnnotations(ident.host, ident.source, ident.metricName) : {} },
                },
            },
        };
    },

    // Compact stat formatting — keeps the statistics grid from overflowing
    // its tile when the metric is bytes, large counts, or any value that
    // toFixed(2)'s into a 10+ character string.
    _fmtStat(v, unit) {
        if (v == null) return '—';
        const n = Number(v);
        if (!Number.isFinite(n)) return '—';
        if (unit === 'bytes' || unit === 'B') {
            const abs = Math.abs(n);
            if (abs >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + ' TB';
            if (abs >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + ' GB';
            if (abs >= 1024 ** 2) return (n / 1024 ** 2).toFixed(2) + ' MB';
            if (abs >= 1024)      return (n / 1024).toFixed(2)      + ' KB';
            return n.toFixed(0) + ' B';
        }
        const abs = Math.abs(n);
        if (abs >= 1e9) return (n / 1e9).toFixed(2) + 'G';
        if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
        if (abs >= 1e3) return (n / 1e3).toFixed(2) + 'k';
        return n.toFixed(2);
    },

    loadTrend(points, metricLabel, unit) {
        const canvas = document.getElementById('trendChart');
        if (!canvas || typeof Chart === 'undefined') return;

        if (this._trendChart) { this._trendChart.destroy(); this._trendChart = null; }

        if (points.length < 3) {
            canvas.style.display = 'none';
            const existing = canvas.parentElement.querySelector('.trend-empty');
            if (!existing) {
                const msg = document.createElement('div');
                msg.className = 'trend-empty empty-state';
                msg.style.cssText = 'padding:20px;text-align:center;color:#64748b;font-size:0.85em';
                msg.textContent = 'Not enough data points for trend analysis';
                canvas.parentElement.appendChild(msg);
            }
            return;
        }

        // Remove any empty state message and show canvas
        canvas.style.display = '';
        canvas.parentElement.querySelector('.trend-empty')?.remove();

        const n = points.length;
        const rawData = points.map(p => ({ x: new Date(p.timestamp), y: p.value }));

        // Moving average — window ≈ 10% of points, clamped 3–30
        const win = Math.min(30, Math.max(3, Math.floor(n * 0.1)));
        const maData = rawData.map((_, i) => {
            const s = Math.max(0, i - Math.floor(win / 2));
            const e = Math.min(n, s + win);
            const slice = points.slice(s, e);
            return { x: rawData[i].x, y: slice.reduce((acc, p) => acc + p.value, 0) / slice.length };
        });

        // Linear regression over point indices
        const ys = points.map(p => p.value);
        const sumX = (n * (n - 1)) / 2;
        const sumX2 = (n * (n - 1) * (2 * n - 1)) / 6;
        const sumY = ys.reduce((a, b) => a + b, 0);
        const sumXY = ys.reduce((s, y, i) => s + i * y, 0);
        const slope = (n * sumXY - sumX * sumY) / (n * sumX2 - sumX * sumX);
        const intercept = (sumY - slope * sumX) / n;
        const trendData = [
            { x: rawData[0].x,     y: intercept },
            { x: rawData[n - 1].x, y: slope * (n - 1) + intercept },
        ];

        // Trend direction label
        const startY = intercept;
        const endY = slope * (n - 1) + intercept;
        const pct = startY !== 0 ? ((endY - startY) / Math.abs(startY)) * 100 : 0;
        let direction, trendColor;
        if (Math.abs(pct) < 2) {
            direction = '→ Stable';
            trendColor = '#94a3b8';
        } else if (pct > 0) {
            direction = `↑ Rising  +${pct.toFixed(1)}% over window`;
            trendColor = '#f59e0b';
        } else {
            direction = `↓ Falling  ${pct.toFixed(1)}% over window`;
            trendColor = '#4ade80';
        }

        this._trendChart = new Chart(canvas, {
            type: 'line',
            data: {
                datasets: [
                    {
                        label: 'Raw',
                        data: rawData,
                        borderColor: 'rgba(99,102,241,0.25)',
                        backgroundColor: 'transparent',
                        borderWidth: 1,
                        pointRadius: 0,
                        tension: 0,
                        order: 3,
                    },
                    {
                        label: `Moving avg (${win}pt)`,
                        data: maData,
                        borderColor: '#6366f1',
                        backgroundColor: 'rgba(99,102,241,0.07)',
                        borderWidth: 2.5,
                        pointRadius: 0,
                        fill: true,
                        tension: 0.4,
                        order: 2,
                    },
                    {
                        label: 'Trend',
                        data: trendData,
                        borderColor: trendColor,
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        pointRadius: 4,
                        pointBackgroundColor: trendColor,
                        borderDash: [6, 3],
                        tension: 0,
                        order: 1,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: this._timeAxis(),
                    y: {
                        ticks: {
                            color: '#94a3b8',
                            callback: v => unit ? `${Number(v).toFixed(1)} ${unit}` : Number(v).toFixed(1),
                        },
                        grid: { color: '#334155' },
                    },
                },
                plugins: {
                    legend: { labels: { color: '#f1f5f9' } },
                    zoom: _zoomOpts,
                    title: {
                        display: true,
                        text: direction,
                        color: trendColor,
                        font: { size: 13, weight: '600' },
                        padding: { bottom: 8 },
                    },
                },
            },
        });
    },

    // Parse a metric key in either format: "host|source/metric_name" (new
    // per-host form) or legacy "source/metric_name" — returns {host, source, name}.
    _parseKey(metricKey) {
        let host = null;
        let rest = metricKey;
        const pipeIdx = metricKey.indexOf('|');
        if (pipeIdx >= 0) {
            const h = metricKey.slice(0, pipeIdx);
            host = (h && h !== '*') ? h : null;
            rest = metricKey.slice(pipeIdx + 1);
        }
        const slashIdx = rest.indexOf('/');
        const source = slashIdx >= 0 ? rest.slice(0, slashIdx) : rest;
        const name = slashIdx >= 0 ? rest.slice(slashIdx + 1) : '';
        return { host, source, name };
    },

    _chartLabel(host, source, name) {
        return host ? `[${host}] ${source}/${name}` : `${source}/${name}`;
    },

    async loadMain(metricKey, rangeMinutes) {
        if (!metricKey) return;
        const { host, source, name: metricName } = this._parseKey(metricKey);
        const m = AppState.metrics.find(x =>
            x.source === source && x.metric_name === metricName && (x.hostname || null) === host);
        const unit = m?.unit || '';

        let points = [];
        try {
            points = await ApiClient.metrics.getHistory(source, metricName,
                { since_minutes: rangeMinutes, hostname: host || '' });
        } catch (e) {
            ToastManager.show(
                `Failed to load ${source}/${metricName}: ${e?.message || 'fetch error'}`,
                'error'
            );
        }

        // Fallback: use latest_value as a single point when history is empty
        if (points.length === 0 && m?.latest_value !== undefined) {
            points = [{ timestamp: m.latest_timestamp || new Date().toISOString(), value: m.latest_value }];
        }

        const canvas = document.getElementById('mainChart');
        if (!canvas) return;

        const label = this._chartLabel(host, source, metricName);

        if (typeof Chart === 'undefined') {
            canvas.parentElement.innerHTML = `<div class="empty-state" style="padding:20px">
                <p>${escapeHtml(label)}: <strong>${m?.latest_value !== undefined ? Number(m.latest_value).toFixed(2) + ' ' + escapeHtml(unit) : '—'}</strong></p>
                <p style="color:#64748b;font-size:0.85em">Chart.js unavailable — chart cannot render</p>
            </div>`;
            return;
        }

        if (this._mainChart) { this._mainChart.destroy(); this._mainChart = null; }
        this._mainChart = new Chart(canvas, this._chartConfig(label, unit, points, { host, source, metricName }));
        this._mainChart.$ident = { host, source, metricName };
    },

    async loadHistory(metricKey, rangeMinutes) {
        if (!metricKey) return;
        const { host, source, name: metricName } = this._parseKey(metricKey);
        const m = AppState.metrics.find(x =>
            x.source === source && x.metric_name === metricName && (x.hostname || null) === host);
        const unit = m?.unit || '';

        let points = [];
        try {
            points = await ApiClient.metrics.getHistory(source, metricName,
                { since_minutes: rangeMinutes, hostname: host || '' });
        } catch (e) {
            ToastManager.show(
                `Failed to load history for ${source}/${metricName}: ${e?.message || 'fetch error'}`,
                'error'
            );
        }

        if (points.length === 0 && m?.latest_value !== undefined) {
            points = [{ timestamp: m.latest_timestamp || new Date().toISOString(), value: m.latest_value }];
        }

        const canvas = document.getElementById('historyChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (this._historyChart) { this._historyChart.destroy(); this._historyChart = null; }
        const label = this._chartLabel(host, source, metricName);
        this._historyChart = new Chart(canvas, this._chartConfig(label, unit, points, { host, source, metricName }));
        this._historyChart.$ident = { host, source, metricName };

        // Populate stats
        if (points.length > 0) {
            try {
                const summary = await ApiClient.metrics.getSummary(source, metricName, { window_minutes: rangeMinutes });
                const set = (id, v) => {
                    const el = document.getElementById(id);
                    if (el) el.textContent = ChartManager._fmtStat(v, unit);
                };
                set('statMin', summary.min_value); set('statMax', summary.max_value);
                set('statMean', summary.avg_value); set('statStd', summary.std_dev);
                set('statP50', summary.p50); set('statP95', summary.p95);
                set('statP99', summary.p99);
                const cnt = document.getElementById('statCount');
                if (cnt) cnt.textContent = summary.count != null ? String(summary.count) : '—';
            } catch (e) { /* ignore */ }
        }

        // Populate trend chart
        this.loadTrend(points, `${source}/${metricName}`, unit);
    },
};

// Double-click a metric chart to reset its zoom/pan back to the full range.
document.addEventListener('dblclick', (e) => {
    [ChartManager._mainChart, ChartManager._historyChart, ChartManager._trendChart].forEach(c => {
        if (c && c.canvas === e.target && typeof c.resetZoom === 'function') {
            try { c.resetZoom(); } catch (_) {}
        }
    });
});

// ── Filter Handlers ──

const FilterHandlers = {
    init() {
        // Severity filter
        const severityFilter = document.getElementById('alertSeverityFilter');
        if (severityFilter) {
            severityFilter.addEventListener('change', (e) => {
                AppState.filters.alerts.severity = e.target.value || 'all';
                AlertManager.render();
            });
        }

        // Status filter
        const statusFilter = document.getElementById('alertStatusFilter');
        if (statusFilter) {
            statusFilter.addEventListener('change', (e) => {
                AppState.filters.alerts.status = e.target.value || 'all';
                AlertManager.render();
            });
        }

        // Search
        const searchInput = document.getElementById('alertSearchInput');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                AppState.filters.alerts.search = e.target.value;
                AlertManager.render();
            });
        }

        // Create rule button (rules tab)
        const createRuleBtn = document.getElementById('createRuleBtn');
        if (createRuleBtn) {
            createRuleBtn.addEventListener('click', () => {
                ModalManager.open('create-rule-modal', {
                    onSubmit: async (data) => {
                        try {
                            await ApiClient.rules.create(data);
                            ToastManager.show('Rule created', 'success');
                            ModalManager.close();
                            RuleManager.load();
                        } catch (error) {
                            ToastManager.show('Failed to create rule', 'error');
                        }
                    },
                });
            });
        }

        // Rule template cards
        document.querySelectorAll('.template-card').forEach(card => {
            card.addEventListener('click', () => {
                ModalManager.open('create-rule-modal', {
                    initialRuleType: card.dataset.template,
                    onSubmit: async (data) => {
                        try {
                            await ApiClient.rules.create(data);
                            ToastManager.show('Rule created', 'success');
                            ModalManager.close();
                            RuleManager.load();
                        } catch (e) {
                            ToastManager.show(e.message || 'Failed to create rule', 'error');
                        }
                    },
                });
            });
        });

        // Channel type cards — click to open add-channel modal with type pre-selected
        document.querySelectorAll('.channel-type-card').forEach(card => {
            card.addEventListener('click', () => {
                ModalManager.open('create-channel-modal', {
                    initialChannelType: card.dataset.type,
                    onSubmit: async (data) => {
                        try {
                            await ApiClient.notifications.createChannel(data);
                            ToastManager.show('Channel added', 'success');
                            ModalManager.close();
                            TabManager.loadNotifications();
                        } catch (e) {
                            ToastManager.show(e.message || 'Failed to add channel', 'error');
                        }
                    },
                });
            });
        });

        // Add rule button (dashboard widget)
        const addRuleBtn = document.getElementById('addRuleBtn');
        if (addRuleBtn) {
            addRuleBtn.addEventListener('click', () => TabManager.switchTab('rules'));
        }

        const goToAlerts = (severity) => {
            AppState.filters.alerts.severity = severity || 'all';
            const sevSel = document.getElementById('alertSeverityFilter');
            if (sevSel) sevSel.value = severity || 'all';
            TabManager.switchTab('alerts');
            AlertManager.render();
        };
        const cardLinks = [
            ['.card-metrics',         () => { TabManager.switchTab('dashboard'); TabManager.switchSubtab('dashboard-panel', 'metrics'); }],
            ['.card-active-alerts',   () => goToAlerts('all')],
            ['.card-critical-alerts', () => goToAlerts('critical')],
            ['.card-rules',           () => TabManager.switchTab('rules')],
            ['.card-anomalies',       () => { TabManager.switchTab('dashboard'); TabManager.switchSubtab('dashboard-panel', 'overview'); }],
        ];
        for (const [sel, handler] of cardLinks) {
            const el = document.querySelector(sel);
            if (!el) continue;
            el.classList.add('summary-card-link');
            el.setAttribute('role', 'button');
            el.setAttribute('tabindex', '0');
            el.addEventListener('click', handler);
            el.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
            });
        }

        const clearAllBtn = document.getElementById('clearAllAlertsBtn');
        if (clearAllBtn) {
            clearAllBtn.addEventListener('click', async () => {
                const active = AppState.alerts.filter(a => a.status === 'active');
                if (active.length === 0) {
                    ToastManager.show('No active alerts to clear', 'info');
                    return;
                }
                const ok = await ModalManager.confirm({
                    title: 'Clear All Alerts',
                    message: `Close ${active.length} active alert(s)?`,
                    confirmLabel: 'Clear All',
                    danger: true,
                });
                if (!ok) return;
                clearAllBtn.disabled = true;
                const results = await Promise.allSettled(
                    active.map(a => ApiClient.alerts.close(a.alert_id))
                );
                const failed = results.filter(r => r.status === 'rejected').length;
                if (failed === 0) {
                    ToastManager.show(`Closed ${active.length} alert(s)`, 'success');
                } else {
                    ToastManager.show(`Closed ${active.length - failed}; ${failed} failed`, 'warning');
                }
                clearAllBtn.disabled = false;
                AlertManager.load();
            });
        }

        // Export alerts button
        const exportBtn = document.getElementById('exportAlertsBtn');
        if (exportBtn) {
            exportBtn.addEventListener('click', async () => {
                try {
                    const blob = await ApiClient.alerts.exportAlerts('csv');
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'alerts.csv';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (e) {
                    // Surface the real error message so a 404 / 5xx is
                    // distinguishable from a network failure during diagnosis.
                    ToastManager.show(`Export failed: ${e.message || 'unknown'}`, 'error');
                }
            });
        }

        // Bulk action checkboxes + button
        const _updateBulkBtn = () => {
            const btn = document.getElementById('bulkActionsBtn');
            const sel = document.getElementById('bulkActionSelect');
            const checked = document.querySelectorAll('.alert-checkbox:checked').length;
            if (sel) sel.disabled = checked === 0;
            if (btn) {
                btn.disabled = checked === 0 || !(sel && sel.value);
                btn.textContent = checked > 0 ? `Apply (${checked})` : 'Apply';
            }
        };
        const selectAll = document.getElementById('selectAllAlerts');
        if (selectAll) {
            selectAll.addEventListener('change', () => {
                document.querySelectorAll('.alert-checkbox').forEach(cb => {
                    cb.checked = selectAll.checked;
                });
                _updateBulkBtn();
            });
        }
        document.addEventListener('change', (e) => {
            if (e.target && e.target.classList && e.target.classList.contains('alert-checkbox')) {
                _updateBulkBtn();
            }
        });
        const bulkSel = document.getElementById('bulkActionSelect');
        if (bulkSel) bulkSel.addEventListener('change', _updateBulkBtn);
        const bulkBtn = document.getElementById('bulkActionsBtn');
        if (bulkBtn) {
            bulkBtn.addEventListener('click', async () => {
                const ids = Array.from(document.querySelectorAll('.alert-checkbox:checked'))
                    .map(cb => cb.dataset.alertId);
                if (!ids.length) return;
                const action = (bulkSel?.value || '').trim().toLowerCase();
                if (!['acknowledge', 'close', 'ignore'].includes(action)) {
                    ToastManager.show('Pick a bulk action first', 'info');
                    return;
                }
                if (!confirm(`${action} ${ids.length} alert(s)?`)) return;
                try {
                    const r = await ApiClient.alerts.bulkUpdate(ids, action);
                    ToastManager.show(`${r.updated || 0} alert(s) ${action}d`, 'success');
                    if (typeof AlertManager !== 'undefined' && AlertManager.load) AlertManager.load();
                    if (selectAll) selectAll.checked = false;
                    if (bulkSel) bulkSel.value = '';
                    _updateBulkBtn();
                } catch (e) {
                    ToastManager.show(`Bulk ${action} failed: ${e.message || 'error'}`, 'error');
                }
            });
        }

        // Add notification channel button
        const addChannelBtn = document.getElementById('addChannelBtn');
        if (addChannelBtn) {
            addChannelBtn.addEventListener('click', () => {
                ModalManager.open('create-channel-modal', {
                    onSubmit: async (data) => {
                        try {
                            await ApiClient.notifications.createChannel(data);
                            ToastManager.show('Channel added', 'success');
                            ModalManager.close();
                            TabManager.loadNotifications();
                        } catch (e) {
                            ToastManager.show(e.message || 'Failed to add channel', 'error');
                        }
                    },
                });
            });
        }

        // Add notification config button
        const addConfigBtn = document.getElementById('addConfigBtn');
        if (addConfigBtn) {
            addConfigBtn.addEventListener('click', async () => {
                const channels = AppState.notifications?.methods || [];
                const catalog = await ModalManager._fetchPolicyCatalog();
                ModalManager.open('create-config-modal', {
                    channels,
                    catalog,
                    onSubmit: async (data) => {
                        try {
                            await ApiClient.notifications.createConfig(data);
                            ToastManager.show('Config added', 'success');
                            ModalManager.close();
                            TabManager.loadNotifications();
                        } catch (e) {
                            ToastManager.show(e.message || 'Failed to add config', 'error');
                        }
                    },
                });
            });
        }

        // Dashboard real-time chart controls
        const metricSelect = document.getElementById('metricSelect');
        const chartRangeSelect = document.getElementById('chartRangeSelect');
        const loadMainChart = () => {
            const key = metricSelect?.value;
            const range = parseInt(chartRangeSelect?.value || '15', 10);
            if (key) ChartManager.loadMain(key, range);
        };
        if (metricSelect) metricSelect.addEventListener('change', loadMainChart);
        if (chartRangeSelect) chartRangeSelect.addEventListener('change', loadMainChart);

        // History tab chart controls
        const historyMetricSelect = document.getElementById('historyMetricSelect');
        const historyRangeSelect = document.getElementById('historyRangeSelect');
        const loadHistoryChart = () => {
            const key = historyMetricSelect?.value;
            const range = parseInt(historyRangeSelect?.value || '1440', 10);
            if (key) ChartManager.loadHistory(key, range);
        };
        if (historyMetricSelect) historyMetricSelect.addEventListener('change', loadHistoryChart);
        if (historyRangeSelect) historyRangeSelect.addEventListener('change', loadHistoryChart);

        // History tab CSV export — uses whatever the user has currently
        // selected in the device/metric/range dropdowns. Triggers a real
        // download via a temporary <a> appended to the DOM (Safari and a
        // few embedded webviews ignore .click() on detached anchors).
        const exportHistoryBtn = document.getElementById('exportHistoryBtn');
        if (exportHistoryBtn) {
            exportHistoryBtn.addEventListener('click', async () => {
                const key = historyMetricSelect?.value;
                if (!key) {
                    ToastManager.show('Pick a metric first', 'info');
                    return;
                }
                const { host, source, name } = ChartManager._parseKey(key);
                const since_minutes = parseInt(historyRangeSelect?.value || '1440', 10);
                try {
                    const blob = await ApiClient.metrics.exportCsv({
                        source, metric_name: name, since_minutes, hostname: host,
                    });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    const hostPart = host ? `${host}_` : '';
                    a.download = `${hostPart}${source}_${name}.csv`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (e) {
                    ToastManager.show(`Export failed: ${e.message || 'unknown'}`, 'error');
                }
            });
        }
    },
};

// ── Last Refreshed Indicator ──
//
// Updates the timestamp pill next to the connection-status "Live" indicator
// every time the alerts list is reloaded — gives the user immediate feedback
// on freshness so they know whether they're looking at stale data.

function updateLastRefreshed() {
    const el = document.getElementById('lastRefreshedTime');
    if (!el) return;
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');
    const ss = String(now.getSeconds()).padStart(2, '0');
    el.textContent = `${hh}:${mm}:${ss}`;
    el.dataset.timestamp = now.toISOString();
}

// ── Manual Refresh ──
//
// The alerts-tab "Refresh" button and the top-bar refresh icon both reload
// every section the dashboard surfaces (alerts + rules + metrics + counters).
// We re-run DashboardManager.init() so renderStats() recomputes counters from
// the fresh AppState — no separate counters endpoint to keep in sync.

async function manualRefreshAll(triggerEl) {
    if (triggerEl) {
        triggerEl.disabled = true;
        triggerEl.classList.add('refreshing');
    }
    try {
        await DashboardManager.init();
    } finally {
        if (triggerEl) {
            triggerEl.disabled = false;
            triggerEl.classList.remove('refreshing');
        }
    }
}

// ── Application Initialization ──

// Apply the theme passed by the manager via ?theme=<name> on the iframe URL.
// Falls back to "dark" if missing or unknown. Must run before render so the
// initial paint is in the right theme.
(function _applyThemeFromQuery() {
    const ALLOWED = new Set(['dark', 'medium', 'light', 'modern', 'classic', 'slate', 'enterprise']);
    const params = new URLSearchParams(window.location.search);
    const requested = (params.get('theme') || '').toLowerCase();
    document.documentElement.dataset.theme = ALLOWED.has(requested) ? requested : 'dark';
})();

// Listen for live theme changes from the parent (manager). The manager
// posts {type: 'theme', name: '<name>'} when the operator picks a new
// theme so the alarm SPA updates without a reload.
window.addEventListener('message', (ev) => {
    try {
        const d = ev.data;
        if (d && d.type === 'theme' && typeof d.name === 'string') {
            const ALLOWED = new Set(['dark', 'medium', 'light', 'modern', 'classic', 'slate', 'enterprise']);
            if (ALLOWED.has(d.name)) document.documentElement.dataset.theme = d.name;
        }
    } catch (_) {}
});

document.addEventListener('DOMContentLoaded', () => {
    // Initialize tab navigation
    TabManager.init();

    // Initialize filters
    FilterHandlers.init();

    // Initialize WebSocket
    WebSocketEvents.init();

    // Wire refresh buttons (top-bar icon + alerts-tab button)
    const topRefresh = document.getElementById('refreshBtn');
    if (topRefresh) topRefresh.addEventListener('click', () => manualRefreshAll(topRefresh));
    const alertsRefresh = document.getElementById('refreshAlertsBtn');
    if (alertsRefresh) alertsRefresh.addEventListener('click', () => manualRefreshAll(alertsRefresh));

    // Delegated handler for the alerts-table Rule links — survives row re-renders
    // since it's bound to the (stable) tbody, not the per-render <a> elements.
    const alertsTbody = document.getElementById('alertsTableBody');
    if (alertsTbody) {
        alertsTbody.addEventListener('click', (e) => {
            const link = e.target.closest('.alert-rule-link');
            if (!link) return;
            e.preventDefault();
            if (link.dataset.ruleId) RuleManager.edit(link.dataset.ruleId);
        });
    }

    // Metrics-tab manual refresh: reload real-time + historical chart data only,
    // so the user doesn't pay for a full DashboardManager.init() when all they
    // want is fresher numbers under the time picker.
    const metricsRefresh = document.getElementById('metricsRefreshBtn');
    if (metricsRefresh) {
        metricsRefresh.addEventListener('click', async () => {
            metricsRefresh.disabled = true;
            metricsRefresh.classList.add('refreshing');
            try {
                await MetricsManager.load();
                await DashboardManager.loadHistory();
                updateLastRefreshed();
            } finally {
                metricsRefresh.disabled = false;
                metricsRefresh.classList.remove('refreshing');
            }
        });
    }

    // Load initial data
    DashboardManager.init();

    console.log('Alarm Engine Dashboard initialized');
});