// Alarm console shell: state, view switching, polls and boot.

const AppState = {
    currentTab: 'console',
    alerts: [],        // every alert the ledger fetch returned (open + recent closed)
    rules: [],
    metrics: [],
    health: null,
    lastRefresh: null,
    lastRefreshOk: true,
    filters: {
        alerts: { severity: 'all', status: 'open', search: '', sort: 'fired', dir: 'desc', page: 1, pageSize: 25 },
        console: { severity: 'all', sort: 'fired', dir: 'desc' },
        rules: { search: '', type: 'all', state: 'all', host: '', sort: 'name', dir: 'asc', page: 1, pageSize: 25 },
        metrics: { host: '', key: '', minutes: 60, offset: 0, mark: null },
        deliveries: { type: '', result: '', page: 1, pageSize: 20 },
    },
};

const UIStates = {
    _loading: new Set(),
    setConnected(value) {
        UI.setLive(value ? 'live' : 'offline');
    },
    setReconnecting() {
        UI.setLive('reconnecting');
    },
    setLoading(resource, value) {
        if (value) this._loading.add(resource); else this._loading.delete(resource);
    },
};

// websocket.js calls DashboardManager.refresh on a dashboard_update event.
const DashboardManager = {
    refresh() { TabManager.refreshCurrent(); },
};

const TabManager = {
    _pollTimer: null,

    init() {
        document.querySelectorAll('.sub-tab-btn[data-tab]').forEach(btn => {
            btn.addEventListener('click', () => this.switchTab(btn.dataset.tab));
        });
        document.addEventListener('click', (e) => {
            const link = e.target.closest('[data-tab-link]');
            if (link) this.switchTab(link.dataset.tabLink);
        });
        document.getElementById('stamp')?.addEventListener('click', () => this.refreshCurrent(true));
        window.addEventListener('hashchange', () => this._fromHash());
    },

    _fromHash() {
        const m = /^#(console|alerts|metrics|rules|notifications)\b/.exec(location.hash || '');
        if (m && m[1] !== AppState.currentTab) this.switchTab(m[1], false);
    },

    switchTab(tabName, pushHash = true) {
        AppState.currentTab = tabName;
        document.querySelectorAll('.sub-tab-btn[data-tab]').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });
        document.querySelectorAll('.panel').forEach(p => {
            p.classList.toggle('active', p.id === `panel-${tabName}`);
        });
        const status = document.getElementById('pageStatus');
        const hdr = document.querySelector(`#panel-${tabName} .hdr`);
        if (status && hdr && status.parentElement !== hdr) hdr.appendChild(status);
        if (pushHash) {
            try { history.replaceState(null, '', `#${tabName}`); } catch (_) {}
        }
        if (tabName === 'alerts') {
            const f = AppState.filters.alerts;
            if (f.viaOpen) f.viaOpen = false;
            else if (f.auto) {
                f.auto = false; f.status = 'open'; f.page = 1;
                const sel = document.getElementById('alertStatus'); if (sel) sel.value = 'open';
            }
        }
        UI.closeMenus();
        this._armPoll();
        this.refreshCurrent();
    },

    // Console every 30 s, Alerts every 15 s, Metrics every 60 s while live, Rules every 60 s.
    _armPoll() {
        if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
        const every = { console: 30000, alerts: 15000, metrics: 60000, rules: 60000, notifications: 60000 }[AppState.currentTab];
        if (!every) return;
        this._pollTimer = setInterval(() => {
            if (document.hidden) return;
            if (AppState.currentTab === 'metrics' && (AppState.filters.metrics.minutes > 60 || AppState.filters.metrics.offset > 0)) return;
            this.refreshCurrent();
        }, every);
    },

    async refreshCurrent(manual = false) {
        if (manual) UI.setStamp('busy');
        let ok = true;
        try {
            switch (AppState.currentTab) {
                case 'console':
                    await Promise.all([AlertManager.load(), RuleManager.load(), MetricsManager.load()]);
                    ConsoleView.render();
                    break;
                case 'alerts':
                    await Promise.all([AlertManager.load(), RuleManager.load()]);
                    AlertsView.render();
                    break;
                case 'metrics':
                    await Promise.all([MetricsManager.load(), RuleManager.load()]);
                    await ChartManager.refresh();
                    break;
                case 'rules':
                    await Promise.all([RuleManager.load(), AlertManager.load(), MetricsManager.load(), RuleManager.loadHealth()]);
                    RuleManager.render();
                    break;
                case 'notifications':
                    await Promise.all([NotificationsManager.load(), MetricsManager.load().catch(() => {})]);
                    NotificationsManager.render();
                    break;
            }
        } catch (e) {
            ok = false;
            console.error('refresh failed:', e);
        }
        AppState.lastRefresh = new Date();
        AppState.lastRefreshOk = ok;
        UI.setStamp(ok ? 'ok' : 'crit', AppState.lastRefresh);
        UI.setNavCount(AlertManager.active().length);
    },
};

// Stamp turns amber once the last refresh is older than 60 s.
setInterval(() => {
    if (!AppState.lastRefresh || !AppState.lastRefreshOk) return;
    if (Date.now() - AppState.lastRefresh.getTime() > 60000) UI.setStamp('warn', AppState.lastRefresh);
}, 15000);

// ── theme: ?theme= on the iframe URL, then live postMessage from the manager ──
const THEMES = new Set(['dark', 'medium', 'light', 'modern', 'slate', 'enterprise', 'oled', 'graphite', 'frost']);
const LEGACY_THEMES = { classic: 'oled' };
(function _applyThemeFromQuery() {
    const params = new URLSearchParams(window.location.search);
    const requested = (params.get('theme') || '').toLowerCase();
    const name = LEGACY_THEMES[requested] || requested;
    document.documentElement.dataset.theme = THEMES.has(name) ? name : 'dark';
})();
window.addEventListener('message', (ev) => {
    try {
        const d = ev.data;
        const name = d && d.type === 'theme' && typeof d.name === 'string' ? (LEGACY_THEMES[d.name] || d.name) : '';
        if (name && THEMES.has(name)) {
            document.documentElement.dataset.theme = name;
            if (typeof ChartManager !== 'undefined') ChartManager.retint();
        }
    } catch (_) {}
});

document.addEventListener('DOMContentLoaded', () => {
    const embedded = window.location.pathname.startsWith('/alarm/');
    const head = document.getElementById('siteHead');
    if (head) head.hidden = embedded;

    UI.init();
    TabManager.init();
    AlertsView.init();
    ConsoleView.init();
    MetricsView.init();
    RuleManager.init();
    NotificationsManager.init();
    WebSocketEvents.init();

    const m = /^#(console|alerts|metrics|rules|notifications)\b/.exec(location.hash || '');
    TabManager.switchTab(m ? m[1] : 'console', false);
});
