/**
 * API Client Utilities for Alarm Engine
 * Handles all REST API communication with the backend.
 */

// Same-origin /api/alarm/* in both embedded (under /alarm/, served via the
// manager's proxy_alarm_engine route) and standalone (AE direct) deployments.
// Direct-dialing port 8081 broke split installs where the AE lives on a
// different host than the manager — only the manager's port is browser-
// reachable in that topology. The manager proxies every alarm engine API
// route, so a same-origin fetch works everywhere.
const API_BASE = '/api/alarm';

const ApiClient = {
    // ── Generic Request Helper ──
    async _request(endpoint, options = {}) {
        const url = `${API_BASE}${endpoint}`;
        const config = {
            headers: { 'Content-Type': 'application/json' },
            ...options,
            headers: { ...options.headers, 'Content-Type': 'application/json' },
        };

        try {
            const response = await fetch(url, config);
            if (!response.ok) {
                const errorBody = await response.json().catch(() => ({}));
                throw new Error(errorBody.detail || `HTTP ${response.status}: ${response.statusText}`);
            }
            // 204 No Content
            if (response.status === 204) return null;
            return response.json();
        } catch (error) {
            console.error(`API Error (${endpoint}):`, error);
            throw error;
        }
    },

    // ── Rules API ──
    rules: {
        async list() {
            return ApiClient._request('/rules');
        },
        async get(ruleId) {
            return ApiClient._request(`/rules/${ruleId}`);
        },
        async create(ruleData) {
            return ApiClient._request('/rules', {
                method: 'POST',
                body: JSON.stringify(ruleData),
            });
        },
        async update(ruleId, ruleData) {
            return ApiClient._request(`/rules/${ruleId}`, {
                method: 'PUT',
                body: JSON.stringify(ruleData),
            });
        },
        async delete(ruleId) {
            return ApiClient._request(`/rules/${ruleId}`, {
                method: 'DELETE',
            });
        },
        async toggle(ruleId) {
            return ApiClient._request(`/rules/${ruleId}/toggle`, {
                method: 'PATCH',
            });
        },
        async duplicate(ruleId) {
            return ApiClient._request(`/rules/${ruleId}/duplicate`, {
                method: 'POST',
            });
        },
    },

    // ── Alerts API ──
    alerts: {
        async list(filters = {}) {
            const params = new URLSearchParams(filters);
            return ApiClient._request(`/alerts?${params}`);
        },
        async get(alertId) {
            return ApiClient._request(`/alerts/${alertId}`);
        },
        async acknowledge(alertId) {
            return ApiClient._request(`/alerts/${alertId}/acknowledge`, {
                method: 'POST',
            });
        },
        async close(alertId) {
            return ApiClient._request(`/alerts/${alertId}/close`, {
                method: 'POST',
            });
        },
        async ignore(alertId) {
            return ApiClient._request(`/alerts/${alertId}/ignore`, {
                method: 'POST',
            });
        },
        async createException(alertId, exceptionData) {
            return ApiClient._request(`/alerts/${alertId}/exception`, {
                method: 'POST',
                body: JSON.stringify(exceptionData),
            });
        },
        async bulkUpdate(alertIds, action, data = {}) {
            return ApiClient._request('/alerts/bulk', {
                method: 'POST',
                body: JSON.stringify({ alert_ids: alertIds, action, ...data }),
            });
        },
        async delete(alertId) {
            return ApiClient._request(`/alerts/${alertId}`, {
                method: 'DELETE',
            });
        },
        async exportAlerts(format = 'csv') {
            const url = `${API_BASE}/alerts/export?format=${format}`;
            const response = await fetch(url);
            if (!response.ok) throw new Error(`Export failed: ${response.statusText}`);
            return response.blob();
        },
    },

    // ── Metrics API ──
    // Backend routes:
    //   GET  /metrics                              list tracked metrics
    //   POST /metrics                              ingest one metric point
    //   POST /metrics/batch                        ingest a batch
    //   GET  /metrics/{source}/{metric_name}       full history
    //   GET  /metrics/{source}/{metric_name}/summary  aggregated stats
    metrics: {
        async list() {
            return ApiClient._request('/metrics');
        },
        async getHistory(source, metricName, params = {}) {
            // Caller passes { since_minutes, limit, hostname } — hostname is
            // forwarded as a query string so the backend can scope the result
            // to a single device when multiple hosts share a source/metric pair.
            const query = new URLSearchParams();
            for (const [k, v] of Object.entries(params)) {
                if (v != null && v !== '') query.set(k, v);
            }
            const qs = query.toString();
            const path = `/metrics/${encodeURIComponent(source)}/${encodeURIComponent(metricName)}`;
            return ApiClient._request(qs ? `${path}?${qs}` : path);
        },
        async exportCsv({ source, metric_name, since_minutes, hostname }) {
            const q = new URLSearchParams({ source, metric_name, format: 'csv' });
            if (since_minutes) q.set('since_minutes', String(since_minutes));
            if (hostname) q.set('hostname', hostname);
            const url = `${API_BASE}/metrics/export?${q.toString()}`;
            const response = await fetch(url);
            if (!response.ok) throw new Error(`Export failed: ${response.statusText}`);
            return response.blob();
        },
        async getSummary(source, metricName, params = {}) {
            const query = new URLSearchParams(params);
            const qs = query.toString();
            const path = `/metrics/${encodeURIComponent(source)}/${encodeURIComponent(metricName)}/summary`;
            return ApiClient._request(qs ? `${path}?${qs}` : path);
        },
        // Legacy alias for older callers
        getStats(source, metricName) { return ApiClient.metrics.getSummary(source, metricName); },
    },

    // ── Notifications API ──
    // Backend exposes /channels (delivery endpoints) and /configs (rule-like
    // groupings of channels).  Frontend keeps the legacy method/rule names as
    // aliases so existing callers don't have to change.
    notifications: {
        // Channels
        async listChannels() {
            return ApiClient._request('/notifications/channels');
        },
        async getChannel(channelId) {
            return ApiClient._request(`/notifications/channels/${channelId}`);
        },
        async createChannel(channelData) {
            return ApiClient._request('/notifications/channels', {
                method: 'POST',
                body: JSON.stringify(channelData),
            });
        },
        async updateChannel(channelId, channelData) {
            return ApiClient._request(`/notifications/channels/${channelId}`, {
                method: 'PUT',
                body: JSON.stringify(channelData),
            });
        },
        async deleteChannel(channelId) {
            return ApiClient._request(`/notifications/channels/${channelId}`, {
                method: 'DELETE',
            });
        },
        async testChannel(payload) {
            return ApiClient._request('/notifications/test', {
                method: 'POST',
                body: JSON.stringify(payload || {}),
            });
        },

        // Configs
        async listConfigs() {
            return ApiClient._request('/notifications/configs');
        },
        async createConfig(data) {
            return ApiClient._request('/notifications/configs', {
                method: 'POST',
                body: JSON.stringify(data),
            });
        },
        async updateConfig(configId, data) {
            return ApiClient._request(`/notifications/configs/${configId}`, {
                method: 'PUT',
                body: JSON.stringify(data),
            });
        },
        async deleteConfig(configId) {
            return ApiClient._request(`/notifications/configs/${configId}`, {
                method: 'DELETE',
            });
        },

        // Delivery history
        async getHistory(params = {}) {
            const query = new URLSearchParams(params);
            return ApiClient._request(`/notifications/delivery-history?${query}`);
        },

        // Legacy aliases (kept so existing UI code keeps working)
        listMethods() { return this.listChannels(); },
        getMethod(id) { return this.getChannel(id); },
        createMethod(d) { return this.createChannel(d); },
        updateMethod(id, d) { return this.updateChannel(id, d); },
        deleteMethod(id) { return this.deleteChannel(id); },
        testMethod(id) { return this.testChannel({ channel_id: id }); },
        listRules() { return this.listConfigs(); },
        createRule(d) { return this.createConfig(d); },
        updateRule(id, d) { return this.updateConfig(id, d); },
        deleteRule(id) { return this.deleteConfig(id); },
    },

    // ── Health / Status ──
    async health() {
        // Embedded under /alarm/ → fetch via the manager's serve_alarm_frontend
        // catch-all (which forwards non-static paths to <ae>/<path>).
        // Standalone AE → same-origin /health.
        const healthUrl = window.location.pathname.startsWith('/alarm/')
            ? '/alarm/health'
            : '/health';
        try {
            const resp = await fetch(healthUrl);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return resp.json();
        } catch (e) {
            return { status: 'unknown' };
        }
    },
    async status() {
        return this.health();
    },
};