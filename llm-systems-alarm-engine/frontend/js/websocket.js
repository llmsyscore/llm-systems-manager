/**
 * WebSocket Connection Handler for Live Dashboard Updates
 * Connects to the backend WebSocket endpoint for real-time alerts and metrics.
 */

class WebSocketHandler {
    constructor(options = {}) {
        this.ws = null;
        this.reconnectInterval = options.reconnectInterval || 3000;
        this.maxReconnectAttempts = options.maxReconnectAttempts || 10;
        this.reconnectAttempts = 0;
        this.messageHandlers = {};
        this.onConnect = options.onConnect || null;
        this.onDisconnect = options.onDisconnect || null;
        this.onError = options.onError || null;
    }

    /**
     * Connect to WebSocket server
     */
    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        // Embedded via /alarm/ → connect to /ws/alarm on the same origin so
        // the manager's proxy_alarm_websocket route handles the upgrade.
        // Direct-dialing port 8081 broke split installs where the AE lives
        // on a different host.
        // Standalone AE → same-origin /ws.
        const isEmbedded = window.location.pathname.startsWith('/alarm/') ||
                           window.ALARM_WS_URL != null;
        const wsPath = isEmbedded ? '/ws/alarm' : '/ws';
        const wsUrl = window.ALARM_WS_URL ||
                      `${protocol}//${window.location.host}${wsPath}`;

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.reconnectAttempts = 0;
                if (this.onConnect) this.onConnect();
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    // Backend emits {event, data, ts}; legacy senders use {type, payload}
                    const type = data.event || data.type;
                    const payload = data.data ?? data.payload;
                    this._dispatch(type, payload);
                } catch (e) {
                    console.error('WebSocket message parse error:', e);
                }
            };

            this.ws.onclose = () => {
                console.log('WebSocket disconnected');
                if (this.onDisconnect) this.onDisconnect();
                this._attemptReconnect();
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                if (this.onError) this.onError(error);
            };
        } catch (error) {
            console.error('WebSocket connection failed:', error);
            this._attemptReconnect();
        }
    }

    /**
     * Disconnect from WebSocket server
     */
    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    /**
     * Attempt to reconnect with exponential backoff
     */
    _attemptReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.error('Max reconnection attempts reached');
            return;
        }

        this.reconnectAttempts++;
        const delay = this.reconnectInterval * Math.pow(1.5, this.reconnectAttempts - 1);
        console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);

        setTimeout(() => this.connect(), delay);
    }

    /**
     * Register a handler for a specific message type
     */
    on(type, handler) {
        if (!this.messageHandlers[type]) {
            this.messageHandlers[type] = [];
        }
        this.messageHandlers[type].push(handler);
    }

    /**
     * Remove a handler for a specific message type
     */
    off(type, handler) {
        if (!this.messageHandlers[type]) return;
        this.messageHandlers[type] = this.messageHandlers[type].filter(
            (h) => h !== handler
        );
    }

    /**
     * Dispatch message to registered handlers
     */
    _dispatch(type, payload) {
        const handlers = this.messageHandlers[type] || [];
        handlers.forEach((handler) => handler(payload));
    }

    /**
     * Send a message to the server.  Backend protocol: {action, event_type, ...}
     * (legacy callers used `type` — we map it to `action` here for compatibility).
     */
    send(typeOrAction, payload = {}) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            const msg = { action: typeOrAction, ...payload };
            this.ws.send(JSON.stringify(msg));
        }
    }
}

// ── Global Event Handlers ──

const WebSocketEvents = {
    /**
     * Initialize all WebSocket event handlers
     */
    init() {
        const ws = new WebSocketHandler({
            onConnect: () => {
                console.log('WebSocket connected, subscribing to updates...');
                ws.send('subscribe', { channels: ['alerts', 'metrics', 'rules'] });
                UIStates.setConnected(true);
            },
            onDisconnect: () => {
                UIStates.setConnected(false);
            },
            onError: (error) => {
                console.error('WebSocket error:', error);
            },
        });

        // Alert events
        ws.on('alert_created', (payload) => {
            AlertManager.handleNewAlert(payload);
            const src = [payload.metric_source, payload.metric_name].filter(Boolean).join('/');
            const host = payload.source_host || '';
            const subtitle = [host, src].filter(Boolean).join(' · ');
            const title = payload.rule_name || 'New Alert';
            ToastManager.show(title, payload.severity || 'warning', {
                alertId: payload.alert_id,
                subtitle,
            });
        });

        ws.on('alert_acknowledged', (payload) => {
            AlertManager.handleAlertUpdate(payload);
        });

        ws.on('alert_closed', (payload) => {
            AlertManager.handleAlertUpdate(payload);
            const src = [payload.metric_source, payload.metric_name].filter(Boolean).join('/');
            const host = payload.source_host || '';
            const subtitle = [host, src].filter(Boolean).join(' · ');
            const title = subtitle
                ? (payload.rule_name || 'Alert resolved')
                : `Alert resolved: ${payload.rule_name || ''}`;
            ToastManager.show(title, 'success', { subtitle });
        });

        ws.on('alert_ignored', (payload) => {
            AlertManager.handleAlertUpdate(payload);
        });

        ws.on('alert_exception', (payload) => {
            AlertManager.handleAlertUpdate(payload);
            ToastManager.show(`⚠️ Exception created for: ${payload.rule_name || ''}`, 'info');
        });

        ws.on('alert_threshold_changed', (payload) => {
            AlertManager.handleAlertUpdate(payload);
        });

        // Metric events
        ws.on('metric_update', (payload) => {
            MetricsManager.handleMetricUpdate(payload);
        });

        // Rule events
        ws.on('rule_updated', (payload) => {
            RuleManager.handleRuleUpdate(payload);
        });

        ws.on('rule_created', (payload) => {
            RuleManager.handleRuleUpdate(payload);
            ToastManager.show('📋 New alarm rule created', 'info');
        });

        ws.on('rule_deleted', (payload) => {
            RuleManager.handleRuleDelete(payload);
        });

        // Toast channel notifications — show a browser toast on any tab
        ws.on('notification', (payload) => {
            if (payload && payload.action === 'toast') {
                const severity = payload.severity || 'info';
                const title = payload.title || 'Notification';
                const body  = payload.body  || '';
                ToastManager.show(title, severity, {
                    sticky: payload.sticky === true,
                    alertId: payload.alert_id,
                    subtitle: body,
                });
            }
        });

        // Dashboard refresh
        ws.on('dashboard_update', (payload) => {
            DashboardManager.refresh(payload);
        });

        // Actually open the connection (the constructor only stores config).
        ws.connect();

        return ws;
    },
};