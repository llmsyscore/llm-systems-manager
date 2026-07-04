// Alarm Engine live toast notifications — connects directly to alarm engine WS
// (port 8081, same host) and shows toasts on any tab of the main dashboard.
//
// Dismissals sync with the alarm-engine iframe (Events tab) via a same-origin
// BroadcastChannel('alarm-toasts'). Without it, dismissing here would leave a
// duplicate showing inside the iframe, and vice-versa.
(function() {
    // Prefer the URL the backend injected (window.__AE_WS_URL__ — same one
    // the AE iframe gets via _inject_alarm_ws_url). It already accounts for
    // the WS proxy and the AE's actual scheme. Fall back to a direct dial
    // only when the backend didn't inject one (older manager, AE TLS off).
    const WS_URL = (typeof window !== 'undefined' && window.__AE_WS_URL__)
        ? window.__AE_WS_URL__
        : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:8081/ws`;
    const container = document.getElementById('alarmToastContainer');
    let reconnectDelay = 3000;

    const dismissedAlertIds = new Set();
    let bus = null;
    try {
        bus = new BroadcastChannel('alarm-toasts');
        bus.onmessage = (e) => {
            const msg = e.data || {};
            if (msg.type === 'dismiss' && msg.alertId) {
                dismissedAlertIds.add(msg.alertId);
                if (!container) return;
                container.querySelectorAll(`.ae-toast[data-alert-id="${msg.alertId}"]`).forEach(el => {
                    if (el._dismiss) el._dismiss(false);
                });
            }
        };
    } catch (_) { /* older browser — just won't sync */ }

    function escapeHtml(s) {
        return String(s || '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
    }

    function inferCategory(title, body, severity) {
        // 'ack' (blue, no buttons) or 'clear' (green, no buttons) for already-
        // resolved/acknowledged alerts; otherwise 'alert' (severity colors +
        // Ack/Close buttons).
        const blob = `${title || ''} ${body || ''}`.toLowerCase();
        if (blob.includes('acknowledg')) return 'ack';
        if (blob.includes('resolv') || blob.includes('clear') || blob.includes('closed')) return 'clear';
        return 'alert';
    }

    function showToast(title, body, severity, sticky, alertId, category, incidentId, incidentSize) {
        if (!container) return;
        if (typeof _activeTab !== 'undefined' && _activeTab === 'events') return;
        if (alertId && dismissedAlertIds.has(alertId)) return;

        const cat = category || 'alert';
        const sev = (severity || 'info').toLowerCase();
        const sevClass = cat === 'ack'   ? 'ae-toast-ack'
                       : cat === 'clear' ? 'ae-toast-clear'
                       : `ae-toast-${sev}`;

        // Same-incident toast already on screen — update it in place instead of stacking.
        if (incidentId) {
            const existing = container.querySelector(
                `.ae-toast[data-incident-id="${CSS.escape(incidentId)}"]`);
            if (existing) {
                const msgEl = existing.querySelector('.ae-toast-message');
                if (msgEl) {
                    const safeTitle = escapeHtml(title) + (incidentSize > 1 ? ` (×${incidentSize})` : '');
                    const safeBody = escapeHtml(body);
                    msgEl.innerHTML = safeBody
                        ? `${safeTitle}<br><small>${safeBody}</small>`
                        : safeTitle;
                }
                // Swap severity/category class only; keep show/hide/clickable state classes.
                Array.from(existing.classList).forEach(c => {
                    if (c.indexOf('ae-toast-') === 0 && c !== 'ae-toast-clickable') {
                        existing.classList.remove(c);
                    }
                });
                existing.classList.add(sevClass);
                if (alertId) existing.dataset.alertId = alertId;
                if (existing._dismissTimer) clearTimeout(existing._dismissTimer);
                if (!sticky && existing._dismiss) {
                    existing._dismissTimer = setTimeout(() => existing._dismiss(true), 10000);
                }
                return;
            }
        }

        const el = document.createElement('div');
        el.className = `ae-toast ${sevClass}`;
        if (alertId) el.dataset.alertId = alertId;
        if (incidentId) el.dataset.incidentId = incidentId;

        const safeTitle = escapeHtml(title) + (incidentSize > 1 ? ` (×${incidentSize})` : '');
        const safeBody  = escapeHtml(body);
        const msgEl = document.createElement('span');
        msgEl.className = 'ae-toast-message';
        msgEl.innerHTML = safeBody
            ? `${safeTitle}<br><small>${safeBody}</small>`
            : safeTitle;
        el.appendChild(msgEl);

        if (alertId && cat === 'alert') {
            const actions = document.createElement('div');
            actions.className = 'ae-toast-actions';
            const mkBtn = (label, cls, title) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = `ae-toast-action ${cls}`;
                b.textContent = label;
                b.title = title;
                return b;
            };
            const ackBtn = mkBtn('Ack', 'ae-toast-ack', 'Acknowledge alert');
            const resBtn = mkBtn('Close', 'ae-toast-resolve', 'Close alert');
            actions.appendChild(ackBtn);
            actions.appendChild(resBtn);
            el.appendChild(actions);

            ackBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const targetId = el.dataset.alertId || alertId;
                try {
                    await fetch(`/api/alarm/alerts/${encodeURIComponent(targetId)}/acknowledge`,
                        { method: 'POST', credentials: 'same-origin' });
                } catch (_) {}
                dismiss(true);
            });
            resBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const targetId = el.dataset.alertId || alertId;
                try {
                    await fetch(`/api/alarm/alerts/${encodeURIComponent(targetId)}/close`,
                        { method: 'POST', credentials: 'same-origin' });
                } catch (_) {}
                dismiss(true);
            });
        }

        const closeBtn = document.createElement('button');
        closeBtn.className = 'ae-toast-close';
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', 'Dismiss');
        closeBtn.textContent = '×';
        el.appendChild(closeBtn);

        if (sticky) {
            const stickyEl = document.createElement('span');
            stickyEl.className = 'ae-toast-sticky-indicator';
            stickyEl.textContent = 'Sticky';
            el.appendChild(stickyEl);
        }

        let dismissed = false;
        function dismiss(broadcast = true) {
            if (dismissed) return;
            dismissed = true;
            const currentId = el.dataset.alertId || alertId;
            // Frees the incident slot.
            delete el.dataset.incidentId;
            if (el._dismissTimer) clearTimeout(el._dismissTimer);
            el.classList.remove('show');
            el.classList.add('hide');
            setTimeout(() => el.remove(), 350);
            if (broadcast && currentId) {
                dismissedAlertIds.add(currentId);
                if (bus) {
                    try { bus.postMessage({ type: 'dismiss', alertId: currentId }); } catch (_) {}
                }
            }
        }
        el._dismiss = dismiss;
        closeBtn.addEventListener('click', (e) => { e.stopPropagation(); dismiss(true); });

        if (alertId) {
            el.classList.add('ae-toast-clickable');
            el.title = 'Click to open Events';
            el.addEventListener('click', () => {
                if (typeof switchTab === 'function') switchTab('events');
                dismiss(true);
            });
        }

        container.appendChild(el);
        requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));

        if (!sticky) el._dismissTimer = setTimeout(() => dismiss(true), 10000);

        // Cap at 5 simultaneous toasts (drop oldest non-sticky first)
        while (container.children.length > 5) {
            const victim = Array.from(container.children).find(
                c => !c.querySelector('.ae-toast-sticky-indicator')
            ) || container.firstChild;
            victim.remove();
        }
    }

    // Coalesces bursts of alert_* WS events into a single indicator refresh.
    let refreshIndicatorsTimer = null;
    function debouncedRefreshTabIndicators() {
        if (refreshIndicatorsTimer) clearTimeout(refreshIndicatorsTimer);
        refreshIndicatorsTimer = setTimeout(() => {
            refreshIndicatorsTimer = null;
            try { refreshTabIndicators(); } catch (_) {}
        }, 1000);
    }

    function connect() {
        let ws;
        try { ws = new WebSocket(WS_URL); } catch(e) { setTimeout(connect, reconnectDelay); return; }
        ws.onopen = () => { reconnectDelay = 3000; };
        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                const type = msg.event || msg.type;
                const payload = msg.data ?? msg.payload ?? msg;
                if (type === 'notification' && payload && payload.action === 'toast') {
                    const cat = payload.category
                        || inferCategory(payload.title, payload.body, payload.severity);
                    showToast(
                        payload.title || 'Alarm',
                        payload.body || '',
                        payload.severity || 'warning',
                        payload.sticky === true,
                        payload.alert_id,
                        cat,
                        payload.incident_id || '',
                        payload.incident_size,
                    );
                }
                if (type === 'alert_created' && payload) {
                    const sev = payload.severity || 'warning';
                    if (sev === 'critical') {
                        try { _setTabDot('tabDotEvents', 'alert'); } catch (_) {}
                    }
                    debouncedRefreshTabIndicators();
                }
                if (typeof type === 'string' && type.indexOf('alert_') === 0 && type !== 'alert_created') {
                    debouncedRefreshTabIndicators();
                }
            } catch(_) {}
        };
        ws.onclose = () => {
            reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
            setTimeout(connect, reconnectDelay);
        };
        ws.onerror = () => ws.close();
    }

    // Defer connection slightly so page renders first
    setTimeout(connect, 1500);
})();
