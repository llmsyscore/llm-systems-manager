// Shared helpers for the alarm console: formatting, markup primitives,
// delegated UI behaviours, toasts, the modal shell and the pager.

// ── text + time ──────────────────────────────────────────────────────

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Naive ISO strings from the backend are UTC; force the Z so Date parses them so.
function parseTs(ts) {
    if (!ts) return null;
    if (ts instanceof Date) return isNaN(ts.getTime()) ? null : ts;
    if (typeof ts === 'string' && !ts.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(ts)) {
        ts += 'Z';
    }
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d;
}

const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function fmtTime(ts, seconds = false) {
    const d = parseTs(ts);
    if (!d) return '—';
    let h = d.getHours();
    const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = seconds ? ':' + String(d.getSeconds()).padStart(2, '0') : '';
    return `${h}:${mm}${ss} ${ap}`;
}

// "Sep 3 · 2:27 AM" (year added when it is not the current one).
function fmtWhen(ts, seconds = false) {
    const d = parseTs(ts);
    if (!d) return '—';
    const now = new Date();
    const year = d.getFullYear() === now.getFullYear() ? '' : `, ${d.getFullYear()}`;
    return `${_MONTHS[d.getMonth()]} ${d.getDate()}${year} · ${fmtTime(d, seconds)}`;
}

function fmtAgo(ts, now = Date.now()) {
    const d = parseTs(ts);
    if (!d) return '—';
    const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
    if (s < 45) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
}

// "5 h 29 m", "48 m", "4 h", "2 d 3 h"
function fmtDur(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} s`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} m`;
    const h = Math.floor(m / 60);
    if (h < 24) return m % 60 ? `${h} h ${m % 60} m` : `${h} h`;
    const d = Math.floor(h / 24);
    return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

function fmtNum(v, digits = 1) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    if (!Number.isFinite(n)) return '—';
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + 'G';
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (abs >= 1e4) return (n / 1e3).toFixed(1) + 'k';
    if (Number.isInteger(n)) return String(n);
    return n.toFixed(abs < 10 ? Math.max(digits, 2) : digits);
}

// Value with its unit, "88.5 °C" / "92.4 %" / "0.42 GB" for bytes.
function fmtVal(v, unit) {
    if (v == null || v === '') return '—';
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    if (unit === 'bytes' || unit === 'B') {
        const abs = Math.abs(n);
        if (abs >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + ' TB';
        if (abs >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + ' GB';
        if (abs >= 1024 ** 2) return (n / 1024 ** 2).toFixed(2) + ' MB';
        if (abs >= 1024) return (n / 1024).toFixed(1) + ' KB';
        return n.toFixed(0) + ' B';
    }
    return unit ? `${fmtNum(n)} ${unit}` : fmtNum(n);
}

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };
const ANOMALY_TYPES = new Set(['z_score', 'moving_average', 'percentile', 'rate_of_change']);
const RULE_TYPE_LABELS = {
    threshold_above: 'Threshold above', threshold_below: 'Threshold below',
    threshold_range: 'Threshold range', z_score: 'Z-score anomaly',
    moving_average: 'Moving average', percentile: 'Percentile baseline',
    rate_of_change: 'Rate of change',
};
const CHANNEL_META = {
    toast:   { code: 'toast',   cls: 'popup',   label: 'Browser popups (toast)' },
    email:   { code: 'email',   cls: 'email',   label: 'Email' },
    sms:     { code: 'sms',     cls: 'sms',     label: 'Text message (SMS)' },
    webhook: { code: 'hook',    cls: 'webhook', label: 'Webhook' },
    discord: { code: 'discord', cls: 'discord', label: 'Discord' },
    webpush: { code: 'push',    cls: 'push',    label: 'Phone push (companion app)' },
};

// ── markup primitives ────────────────────────────────────────────────

const _ICON_PATHS = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    snooze: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9 2h6"/>',
    edit: '<path d="M4 20h4l10.5-10.5a2 2 0 0 0-4-4L4 16v4z"/><path d="M13 6l4 4"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
    play: '<path d="M7 5v14l11-7z"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    left: '<path d="M15 5l-7 7 7 7"/>',
    right: '<path d="M9 5l7 7-7 7"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
};

function icon(name) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${_ICON_PATHS[name] || ''}</svg>`;
}

function ibtn(name, tip, cls = '', attrs = '') {
    const safeTip = escapeHtml(tip);
    return `<button type="button" class="ib ${cls}" data-tip="${safeTip}" aria-label="${safeTip}" ${attrs}>${icon(name)}</button>`;
}

function kebabBtn(attrs = '') {
    return `<button type="button" class="ib" data-tip="More" aria-label="More" data-menu ${attrs}>⋯</button>`;
}

function toggleHtml(on, label = '', attrs = '') {
    const lbl = label ? `<span class="tlbl">${escapeHtml(label)}</span>` : '';
    return `<button type="button" class="mc-toggle${on ? ' on' : ''}" role="switch" aria-checked="${on ? 'true' : 'false'}" ${attrs}><span class="track"></span>${lbl}</button>`;
}

function sevHtml(sev, word = true) {
    const s = String(sev || 'info').toLowerCase();
    return `<span class="sev ${escapeHtml(s)}"><span class="dot"></span>${word ? escapeHtml(s) : ''}</span>`;
}

function hostHtml(host) {
    return host
        ? `<span class="host" title="${escapeHtml(host)}">${escapeHtml(host)}</span>`
        : '<span class="host any">any host</span>';
}

const _STATUS_PILL = { active: 'crit', acknowledged: 'note', ignored: 'dim', closed: 'ok', exception: 'info' };
function statusPill(status) {
    const s = String(status || '').toLowerCase();
    return `<span class="pill ${_STATUS_PILL[s] || 'dim'}">${escapeHtml(s || '—')}</span>`;
}

// items: [{act, label, glyph, danger}] or 'hr'
function menuHtml(items, attrs = '') {
    const rows = items.map(it => it === 'hr'
        ? '<hr>'
        : `<button type="button" data-act="${escapeHtml(it.act)}" class="${it.danger ? 'danger' : ''}"><span class="mi${it.wide ? ' t' : ''}">${escapeHtml(it.glyph || '')}</span>${escapeHtml(it.label)}</button>`);
    return `<div class="mc-menu" ${attrs}>${rows.join('')}</div>`;
}

// ── delegated behaviours ─────────────────────────────────────────────

const UI = {
    _openMenu: null,

    init() {
        document.addEventListener('click', (e) => {
            const menuBtn = e.target.closest('[data-menu]');
            if (menuBtn) {
                const menu = menuBtn.nextElementSibling;
                if (menu && menu.classList.contains('mc-menu')) {
                    e.stopPropagation();
                    if (this._openMenu === menu) this.closeMenus();
                    else this.openMenu(menuBtn, menu);
                    return;
                }
            }
            if (!e.target.closest('.mc-menu')) this.closeMenus();
            const h = e.target.closest('.card-h.tog');
            if (h && !e.target.closest('button, select, input, a')) {
                h.closest('.card')?.classList.toggle('collapsed');
            }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeMenus();
                if (typeof ModalManager !== 'undefined' && ModalManager.isOpen()) ModalManager.close();
                return;
            }
            if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
                const t = e.target;
                if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
                const box = document.querySelector('.panel.active .mc-filter input');
                if (box) { e.preventDefault(); box.focus(); box.select(); }
            }
        });
        window.addEventListener('scroll', () => { this.closeMenus(); this.hideTip(); }, true);
        window.addEventListener('resize', () => { this.closeMenus(); this.hideTip(); });
        document.addEventListener('mouseover', (e) => { const t = e.target.closest('[data-tip]'); if (t) this.showTip(t); });
        document.addEventListener('mouseout', (e) => { const t = e.target.closest('[data-tip]'); if (t && !t.contains(e.relatedTarget)) this.hideTip(); });
        document.addEventListener('focusin', (e) => { const t = e.target.closest('[data-tip]'); if (t) this.showTip(t); });
        document.addEventListener('focusout', () => this.hideTip());
        document.addEventListener('click', () => this.hideTip(), true);
    },

    // One fixed-position tip element so overflow containers never clip it.
    showTip(el) {
        const text = el.getAttribute('data-tip');
        if (!text) return;
        let tip = document.getElementById('uiTip');
        if (!tip) { tip = document.createElement('div'); tip.id = 'uiTip'; document.body.appendChild(tip); }
        tip.textContent = text;
        tip.hidden = false;
        const r = el.getBoundingClientRect();
        const w = tip.offsetWidth, h = tip.offsetHeight;
        let left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
        let top = r.bottom + 6;
        if (top + h > window.innerHeight - 8) top = r.top - h - 6;
        tip.style.left = `${left}px`;
        tip.style.top = `${top}px`;
    },

    hideTip() {
        const tip = document.getElementById('uiTip');
        if (tip) tip.hidden = true;
    },

    // Click flips the switch and swaps its label between the on/off wording.
    bindToggle(btn, onLabel, offLabel, onChange) {
        if (!btn) return;
        const paint = () => { const l = btn.querySelector('.tlbl'); if (l) l.textContent = btn.classList.contains('on') ? onLabel : offLabel; };
        btn.addEventListener('click', () => { btn.classList.toggle('on'); btn.setAttribute('aria-checked', btn.classList.contains('on')); paint(); if (onChange) onChange(btn.classList.contains('on')); });
        paint();
    },

    openMenu(btn, menu) {
        this.closeMenus();
        menu.classList.add('open');
        const r = btn.getBoundingClientRect();
        const mw = menu.offsetWidth, mh = menu.offsetHeight;
        let left = Math.min(r.right - mw, window.innerWidth - mw - 8);
        if (left < 8) left = 8;
        let top = r.bottom + 6;
        if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
        menu.style.left = `${left}px`;
        menu.style.top = `${top}px`;
        this._openMenu = menu;
    },

    closeMenus() {
        document.querySelectorAll('.mc-menu.open').forEach(m => m.classList.remove('open'));
        this._openMenu = null;
    },

    // state: ok | warn | crit | busy
    setStamp(state, date = new Date()) {
        const ic = document.getElementById('stampIcon');
        const tx = document.getElementById('stampText');
        const btn = document.getElementById('stamp');
        if (ic) ic.className = `rf ${state === 'busy' ? 'ok' : state}`;
        if (btn) btn.classList.toggle('busy', state === 'busy');
        if (tx && state !== 'busy') tx.textContent = fmtTime(date, true);
    },

    // state: live | offline | reconnecting
    setLive(state) {
        const wrap = document.getElementById('liveState');
        if (!wrap) return;
        const dot = wrap.querySelector('.dot');
        const txt = document.getElementById('liveText');
        const cls = { live: 'ok', offline: 'crit', reconnecting: 'warn' }[state] || '';
        if (dot) dot.className = `dot ${cls}`;
        if (txt) txt.textContent = state;
    },

    setNavCount(n) {
        const el = document.getElementById('navAlertCount');
        if (!el) return;
        el.textContent = String(n);
        el.hidden = !(n > 0);
    },

    // Wires a .mc-seg so a click marks the button and reports its data-v.
    seg(el, onChange) {
        if (!el || el.dataset.wired) return;
        el.dataset.wired = '1';
        el.addEventListener('click', (e) => {
            const b = e.target.closest('button');
            if (!b || !el.contains(b)) return;
            el.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
            onChange(b.dataset.v ?? b.textContent.trim());
        });
    },

    segSet(el, value) {
        if (!el) return;
        el.querySelectorAll('button').forEach(b => b.classList.toggle('on', (b.dataset.v ?? b.textContent.trim()) === String(value)));
    },
};

// ── toasts ───────────────────────────────────────────────────────────

let _toastBus = null;
const _dismissedAlertIds = new Set();
try {
    _toastBus = new BroadcastChannel('alarm-toasts');
    _toastBus.onmessage = (e) => {
        const msg = e.data || {};
        if (msg.type === 'dismiss' && msg.alertId) ToastManager._receiveDismiss(msg.alertId);
    };
} catch (_) { /* no cross-frame sync on very old browsers */ }

const ToastManager = {
    maxToasts: 5,

    show(message, type = 'info', options = {}) {
        if (typeof options === 'number') options = { duration: options };
        const sticky = options.sticky === true;
        const duration = options.duration != null ? options.duration : 10000;
        const alertId = options.alertId || null;
        const subtitle = options.subtitle || '';
        const incidentId = options.incidentId || null;
        const incidentSize = options.incidentSize || 0;
        if (alertId && _dismissedAlertIds.has(alertId)) return;

        let container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.className = 'toast-container';
            document.body.appendChild(container);
        }
        const safeTitle = escapeHtml(message) + (incidentSize > 1 ? ` (×${incidentSize})` : '');
        const subtitleHtml = subtitle ? `<small>${escapeHtml(subtitle)}</small>` : '';

        if (incidentId) {
            const existing = container.querySelector(`.toast[data-incident-id="${CSS.escape(incidentId)}"]`);
            if (existing) {
                const msgEl = existing.querySelector('.toast-message');
                if (msgEl) msgEl.innerHTML = `${safeTitle}${subtitleHtml}`;
                Array.from(existing.classList).forEach(c => {
                    if (c.indexOf('toast-') === 0 && c !== 'toast-clickable') existing.classList.remove(c);
                });
                existing.classList.add(`toast-${type}`);
                if (alertId) existing.dataset.alertId = alertId;
                if (existing._dismissTimer) clearTimeout(existing._dismissTimer);
                if (!sticky && existing._dismiss) {
                    existing._dismissTimer = setTimeout(() => existing._dismiss(true), duration);
                }
                return;
            }
        }

        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        if (alertId) toast.dataset.alertId = alertId;
        if (incidentId) toast.dataset.incidentId = incidentId;
        const actions = alertId
            ? `<div class="toast-actions"><button class="toast-action toast-ack" type="button" title="Acknowledge alert">Ack</button><button class="toast-action toast-resolve" type="button" title="Close alert">Close</button></div>`
            : '';
        toast.innerHTML = `<span class="toast-message">${safeTitle}${subtitleHtml}</span>${actions}<button class="toast-close" type="button" aria-label="Dismiss">×</button>${sticky ? '<span class="toast-sticky-indicator">Sticky</span>' : ''}`;

        const dismiss = (broadcast = true) => {
            if (toast._dismissed) return;
            toast._dismissed = true;
            const currentId = toast.dataset.alertId || alertId;
            delete toast.dataset.incidentId;
            if (toast._dismissTimer) clearTimeout(toast._dismissTimer);
            toast.classList.remove('show');
            toast.classList.add('hide');
            setTimeout(() => toast.remove(), 350);
            if (broadcast && currentId) {
                _dismissedAlertIds.add(currentId);
                if (_toastBus) {
                    try { _toastBus.postMessage({ type: 'dismiss', alertId: currentId }); } catch (_) {}
                }
            }
        };
        toast.querySelector('.toast-close').addEventListener('click', (e) => { e.stopPropagation(); dismiss(true); });
        toast._dismiss = dismiss;

        if (alertId) {
            toast.classList.add('toast-clickable');
            toast.title = 'Open this alert';
            toast.addEventListener('click', () => {
                if (typeof AlertsView !== 'undefined') AlertsView.openFor(toast.dataset.alertId || alertId);
                dismiss(true);
            });
            const act = async (kind) => {
                const targetId = toast.dataset.alertId || alertId;
                if (typeof AlertManager === 'undefined') return;
                if (kind === 'ack') await AlertManager.acknowledge(targetId);
                else await AlertManager.close(targetId);
                dismiss(true);
            };
            toast.querySelector('.toast-ack').addEventListener('click', (e) => { e.stopPropagation(); act('ack'); });
            toast.querySelector('.toast-resolve').addEventListener('click', (e) => { e.stopPropagation(); act('close'); });
        }

        container.appendChild(toast);
        requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));
        if (!sticky) toast._dismissTimer = setTimeout(() => dismiss(true), duration);
        while (container.children.length > this.maxToasts) {
            const victim = Array.from(container.children).find(el => !el.querySelector('.toast-sticky-indicator')) || container.firstChild;
            victim.remove();
        }
    },

    _receiveDismiss(alertId) {
        _dismissedAlertIds.add(alertId);
        const container = document.getElementById('toastContainer');
        if (!container) return;
        container.querySelectorAll(`.toast[data-alert-id="${CSS.escape(alertId)}"]`).forEach(el => {
            if (el._dismiss) el._dismiss(false);
        });
    },
};

// ── modal shell ──────────────────────────────────────────────────────

const ModalManager = {
    _onSubmit: null,
    _onClose: null,
    _submitting: false,
    _seq: 0,
    _wired: false,

    _wireOnce() {
        if (this._wired) return;
        this._wired = true;
        const overlay = document.getElementById('modalOverlay');
        document.getElementById('modalClose')?.addEventListener('click', () => this.close());
        document.getElementById('modalCancel')?.addEventListener('click', () => this.close());
        document.getElementById('modalSubmit')?.addEventListener('click', () => this._submit());
        overlay?.addEventListener('mousedown', (e) => { if (e.target === overlay) this.close(); });
        overlay?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && e.target && e.target.tagName === 'INPUT') { e.preventDefault(); this._submit(); }
        });
    },

    isOpen() {
        return !!document.getElementById('modalOverlay')?.classList.contains('open');
    },

    // opts: title, meta, bodyHtml, submitLabel, cancelLabel, footNote, footActions, width, danger, onSubmit, onOpen
    open(opts = {}) {
        this._wireOnce();
        const overlay = document.getElementById('modalOverlay');
        const modal = document.getElementById('modalBox');
        if (!overlay || !modal) return;
        this._seq++;
        this._submitting = false;
        this._onSubmit = opts.onSubmit || null;
        this._onClose = opts.onClose || null;
        document.getElementById('modalTitle').textContent = opts.title || '';
        document.getElementById('modalMeta').textContent = opts.meta || '';
        document.getElementById('modalBody').innerHTML = opts.bodyHtml || '';
        const note = document.getElementById('modalNote');
        note.textContent = opts.footNote || '';
        note.className = 'msg';
        const acts = document.getElementById('modalActions');
        if (acts) {
            acts.innerHTML = '';
            (opts.footActions || []).forEach(a => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = `mcbtn mcbtn-sm ${a.cls || 'mcbtn-ghost'}`; b.textContent = a.label;
                b.addEventListener('click', () => a.onClick(b));
                acts.appendChild(b);
            });
        }
        const submit = document.getElementById('modalSubmit');
        submit.textContent = opts.submitLabel || 'Save';
        submit.className = `mcbtn mcbtn-sm ${opts.danger ? 'mcbtn-ghost crit' : 'mcbtn-pri'}`;
        submit.disabled = false;
        submit.hidden = opts.submitLabel === null;
        document.getElementById('modalCancel').textContent = opts.cancelLabel || 'Cancel';
        modal.className = `modal ${opts.width || ''}`;
        overlay.classList.add('open');
        if (opts.onOpen) opts.onOpen(document.getElementById('modalBody'));
        const first = document.getElementById('modalBody').querySelector('input, select, textarea, button');
        if (first && opts.autofocus !== false) setTimeout(() => first.focus(), 0);
    },

    close() {
        const overlay = document.getElementById('modalOverlay');
        if (!overlay || !overlay.classList.contains('open')) return;
        overlay.classList.remove('open');
        this._seq++;
        this._submitting = false;
        const onClose = this._onClose;
        this._onSubmit = null;
        this._onClose = null;
        if (onClose) onClose();
    },

    setError(text) {
        const note = document.getElementById('modalNote');
        if (!note) return;
        note.textContent = text || '';
        note.className = text ? 'msg err' : 'msg';
    },

    _submit() {
        if (this._submitting || !this._onSubmit) { if (!this._onSubmit) this.close(); return; }
        this._submitting = true;
        const seq = this._seq;
        const btn = document.getElementById('modalSubmit');
        if (btn) btn.disabled = true;
        Promise.resolve()
            .then(() => this._onSubmit())
            .catch(e => { if (seq === this._seq) this.setError(e?.message || 'Could not save'); })
            .finally(() => {
                if (seq !== this._seq) return;
                this._submitting = false;
                if (btn) btn.disabled = false;
            });
    },

    // Resolves true on confirm, false on cancel/close.
    confirm({ title = 'Confirm', message = 'Are you sure?', confirmLabel = 'Confirm', danger = false } = {}) {
        return new Promise(resolve => {
            let done = false;
            const finish = (v) => { if (!done) { done = true; resolve(v); } };
            this.open({
                title, width: 'dialog', danger,
                bodyHtml: `<p>${escapeHtml(message)}</p>`,
                submitLabel: confirmLabel,
                onSubmit: () => { finish(true); this.close(); },
                onClose: () => finish(false),
            });
        });
    },

    // Resolves the chosen hours, or null when cancelled.
    chooseDuration({ title = 'Ignore alert', confirmLabel = 'Ignore', label = 'Ignore this alert for' } = {}) {
        const presets = [[1, '1 hour'], [6, '6 hours'], [12, '12 hours'], [24, '24 hours'],
            [72, '3 days'], [168, '7 days'], [720, '30 days']];
        const options = presets.map(([h, l]) => `<option value="${h}"${h === 24 ? ' selected' : ''}>${l}</option>`).join('');
        return new Promise(resolve => {
            let done = false;
            const finish = (v) => { if (!done) { done = true; resolve(v); } };
            this.open({
                title, width: 'dialog', submitLabel: confirmLabel,
                bodyHtml: `<div class="st-field solo"><label for="ignoreDurationSelect">${escapeHtml(label)}</label><div class="row"><select id="ignoreDurationSelect" class="sel">${options}</select></div><div class="help">Suppresses the alert for the selected time period.</div></div>`,
                onSubmit: () => {
                    const sel = document.getElementById('ignoreDurationSelect');
                    const hours = sel ? parseInt(sel.value, 10) : 24;
                    finish(Number.isFinite(hours) ? hours : 24);
                    this.close();
                },
                onClose: () => finish(null),
            });
        });
    },
};

// ── pager ────────────────────────────────────────────────────────────

const Pager = {
    // Renders into el; returns [start, end) of the visible slice.
    render(el, { total, page, pageSize, sizes = [25, 50, 100, 0], onPage, onSize, noun = 'rows' }) {
        if (!el) return [0, total];
        const all = pageSize <= 0;
        const pages = all ? 1 : Math.max(1, Math.ceil(total / pageSize));
        const p = Math.min(Math.max(1, page), pages);
        const start = all ? 0 : (p - 1) * pageSize;
        const end = all ? total : Math.min(total, p * pageSize);
        const range = total === 0 ? `no ${noun}` : all ? `all <b>${total}</b>` : `<b>${start + 1}–${end}</b> of ${total}`;
        const opts = sizes.map(s => `<option value="${s}"${s === pageSize ? ' selected' : ''}>${s === 0 ? 'All' : s}</option>`).join('');
        el.innerHTML = `<span>Rows per page</span><select class="sel" data-pg="size">${opts}</select><span>${range}</span><div class="pg">${ibtn('left', 'Previous page', '', 'data-pg="prev"')}<b>${p}</b> / ${pages}${ibtn('right', 'Next page', '', 'data-pg="next"')}</div>`;
        el.querySelector('[data-pg="prev"]').disabled = p <= 1;
        el.querySelector('[data-pg="next"]').disabled = p >= pages;
        if (!el.dataset.wired) {
            el.dataset.wired = '1';
            el.addEventListener('click', (e) => {
                const b = e.target.closest('[data-pg]');
                if (!b || b.tagName !== 'BUTTON') return;
                onPage(b.dataset.pg === 'prev' ? -1 : 1);
            });
            el.addEventListener('change', (e) => {
                if (e.target.dataset.pg === 'size') onSize(parseInt(e.target.value, 10) || 0);
            });
        }
        return [start, end];
    },
};
