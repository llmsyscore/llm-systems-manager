# LLM Systems Manager — API Reference

This reference covers every HTTP endpoint exposed by the LLM Systems Manager and the Alarm Engine. It is written for operators, integration authors, and script writers who need to call endpoints directly — not for contributors reading the source code.

The Manager listens on port 5000 (HTTP) and optionally port 5443 (HTTPS). The Alarm Engine runs on port 8081 but is always accessed through the Manager's `/api/alarm/*` proxy — you should never need to call port 8081 directly. All endpoints in this document use the Manager as the entry point.

---

## Authentication

**Browser / UI sessions** authenticate via a login cookie. After `POST /login` succeeds, your browser holds a signed session cookie that is checked on every subsequent request. Sessions expire based on the configured lifetime (default: several days).

**Agent-to-Manager calls** authenticate with a bearer token issued at registration: `Authorization: Bearer <token>`. These are internal; you do not need to manage them as an operator.

**Admin-only endpoints** are marked **[Admin]** throughout this document. Reaching them requires both an admin-role session and (where configured) a request originating from an allowed admin network range. Operator-role sessions receive a 403 on admin endpoints.

**Ingest endpoints** on the Alarm Engine accept a separate shared bearer token configured in `llm-systems.toml`. When the token is blank the ingest surface is open; when set, agents must present it.

---

## Dashboard & Metrics

### `GET /api/metrics`
Returns the current hardware snapshot across all monitored agents: CPU, RAM, GPU temperature and utilisation, network throughput, disk usage, and any active LLM provider state. This is the primary polling endpoint for the dashboard.

**Parameters:** `?agent=<agent_id>` — restrict the response to a single agent's data.

---

### `GET /api/history`
Returns historical time-series data used to draw dashboard charts. The time window and resolution are controlled by the server's configured history settings.

**Parameters:**
- `?agent=<agent_id>` — return history for one specific agent (full host metric set).
- `?fleet=llama` or `?fleet=lms` — return aggregated fleet history for all agents of that provider type (CPU/RAM/GPU values are aggregated across the fleet).

---

### `GET /api/alert`
Returns the current active alert count and the highest severity level in effect. Used by the dashboard header to show the alert badge.

---

### `GET /api/config`
Returns the current polling interval and basic server configuration the frontend needs to self-configure (for example, which provider tabs to show).

---

### `POST /api/config/interval`
Updates the dashboard polling interval.

**Body:** `{"interval": <seconds>}`

---

## LLM Control (llama.cpp)

These endpoints control the llama.cpp inference server running on the GPU host. All of them are proxied transparently through the Manager to the appropriate agent; you do not need to know which agent is handling a request.

### `GET /api/llama-state`
Returns whether the llama.cpp server is currently `awake` or `sleeping`.

**Parameters:** `?agent=<agent_id>` — query a specific agent rather than the default.

---

### `GET /api/llama-state/stream`
Opens a Server-Sent Events (SSE) stream that pushes a new event each time the llama.cpp server changes state (awake ↔ sleeping) or loads a different model. Stays open until the client disconnects.

**Parameters:** `?agent=<agent_id>`

---

### `GET /api/llm/models`
Lists all model files available on the GPU host's model storage path.

---

### `POST /api/llm/load`
Instructs the llama.cpp server to load a specific model. The server will unload any currently loaded model first.

**Body:** `{"model": "<model_id>"}`

---

### `POST /api/llm/unload`
Unloads the currently active model from the llama.cpp server without stopping the server process.

---

### `GET /api/llm/config`
Returns the llama.cpp server configuration (context window size, GPU layer count, thread count, and other runtime parameters).

**Parameters:** `?model=<model_id>` — return the saved config for a specific model rather than the active one.

---

### `POST /api/llm/config`
Saves the llama.cpp server configuration. The saved values are applied the next time the server loads that model.

**Body:** A JSON object containing the configuration fields to save (context size, GPU layers, threads, etc.).

The configuration is stored in INI format. Key names correspond to llama-server command-line flags: `--ctx-size`, `--gpu-layers`, `--threads`, `--batch-size`, `--parallel`, etc. Retrieve the current config with `GET /api/llm/config` to see available keys.

---

### `DELETE /api/llm/config/<model_id>`
Deletes the saved configuration for the named model, reverting it to defaults on next load.

---

### `GET /api/llm/server/status`
Returns whether the llama.cpp server process is currently running.

---

### `POST /api/llm/server/start`
Starts the llama.cpp server process.

---

### `POST /api/llm/server/stop`
Stops the llama.cpp server process.

---

### `POST /api/llm/server/restart`
Stops then starts the llama.cpp server in one call.

---

### `POST /api/llm/server/wake`
Wakes a sleeping llama.cpp server. The server enters a low-power sleep state after an idle timeout; this call brings it back to the active (ready-to-infer) state.

---

### `GET /api/llm/server/log/tail`
Returns the most recent lines from the llama.cpp server log.

---

### `GET /api/llm/server/log/stream`
Opens an SSE stream that tails the llama.cpp server log in real time. Each event contains one or more new log lines.

---

### `POST /api/llm/download`
Starts an asynchronous download of a model file from HuggingFace. Progress is delivered via `/api/llm/download/stream`.

**Body:** `{"repo": "<huggingface-repo-id>", "file": "<filename>"}`

---

### `POST /api/llm/download/cancel`
Cancels an in-progress model download.

---

### `GET /api/llm/download/stream`
Opens an SSE stream reporting download progress (bytes downloaded, speed, estimated time remaining). The stream closes when the download completes or fails.

---

### `POST /api/llm/build`
Starts an asynchronous build of the llama.cpp binary from source. Progress is delivered via `/api/llm/build/stream`.

---

### `GET /api/llm/build/stream`
Opens an SSE stream reporting build progress and compiler output. The stream closes when the build completes or fails.

---

### `GET /api/llm/cache`
Lists model files currently held in the local model cache on the GPU host.

---

### `POST /api/llm/cache/prune`
Removes cached model files that are not referenced by any saved configuration or active load, freeing disk space.

---

### `POST /api/llm/cache/rm`
Removes a specific file from the model cache.

**Body:** `{"path": "<cache-relative-path>"}`

---

### `GET /api/llm/hf-trending`
Returns the current HuggingFace trending models list, useful for discovering new models to download.

---

### `GET /api/llm/aliases`
Returns the saved model name aliases (human-readable short names mapped to model IDs).

---

### `POST /api/llm/aliases`
Creates or updates a model name alias.

**Body:** `{"model_id": "<id>", "alias": "<short name>"}`

---

### `DELETE /api/llm/aliases/<model_id>`
Removes the alias for the specified model.

---

### `POST /api/benchmark/run`
Starts a benchmark run against the currently loaded model. The benchmark measures prompt processing throughput (tokens/sec) and generation throughput at various context sizes. Results are streamed via `/api/benchmark/stream`.

---

### `GET /api/benchmark/stream`
Opens an SSE stream reporting live benchmark progress (current context size being tested, intermediate results). The stream closes when the benchmark finishes or is cancelled.

---

### `GET /api/benchmark/results`
Returns all saved benchmark results for all models.

---

### `POST /api/benchmark/store`
Saves a benchmark result to persistent storage.

**Body:** A benchmark result object as returned by the benchmark stream.

---

### `DELETE /api/benchmark/results/<model_id>`
Deletes all saved benchmark results for the specified model.

---

### `GET /api/benchmark/models`
Returns the list of models that have at least one saved benchmark result.

---

### `POST /api/benchmark/perf-mode`
Switches the GPU host between performance and power-save operating modes during benchmarking.

**Body:** `{"mode": "performance"}` or `{"mode": "powersave"}`

---

### `POST /api/benchmark/cancel`
Cancels an in-progress benchmark run.

---

### `POST /api/llm/autotune/run`
Starts the Auto-Tune context wizard, which automatically finds the largest context window size the currently loaded model can sustain within GPU memory. Progress is streamed via `/api/llm/autotune/stream`.

---

### `GET /api/llm/autotune/stream`
Opens an SSE stream reporting Auto-Tune progress (context sizes being probed, memory readings, pass/fail results).

---

### `GET /api/llm/autotune/stream-info`
Returns metadata about the current or most recent Auto-Tune run without opening a stream.

---

### `POST /api/llm/autotune/cancel`
Cancels an in-progress Auto-Tune run.

---

## LM Studio

These endpoints control and monitor the LM Studio server running on the Apple Silicon host.

### `GET /api/lmstudio/metrics`
Returns the current LM Studio status, including which model is loaded, active requests, memory usage, and server health.

---

### `GET /api/lmstudio/models`
Lists all models available in LM Studio's model library.

---

### `GET /api/lmstudio/server/status`
Returns whether the LM Studio server process is running.

---

### `POST /api/lmstudio/server/start`
Starts the LM Studio server.

---

### `POST /api/lmstudio/server/stop`
Stops the LM Studio server.

---

### `POST /api/lmstudio/server/restart`
Stops then starts the LM Studio server in one call.

---

### `GET /api/lmstudio/server/log`
Returns recent log output from the LM Studio server.

---

### `POST /api/lmstudio/load`
Instructs LM Studio to load a specific model.

**Body:** `{"model": "<model_id>"}`

---

### `POST /api/lmstudio/unload`
Unloads the currently active model from LM Studio.

---

### `POST /api/lmstudio/download`
Starts a model download within LM Studio.

**Body:** `{"model": "<model_id>"}`

---

## Agent Management

These endpoints manage the fleet of monitoring agents. Most are **[Admin]** only. A small number are called internally by agents themselves (marked "Agent-facing") and are not intended for manual use.

### `GET /api/agents`
Returns the list of all registered agents with their status, capabilities, and last-seen timestamp.

**Access:** [Admin]

---

### `POST /api/agents/register`
Registers a new agent with the Manager. Called automatically by the agent on first start; not a UI-facing endpoint.

**Access:** (Agent-facing)

---

### `GET /api/agents/list-by-provider`
Returns agents grouped by provider type (llama, lms). Available to all authenticated users, including operators, so the agent picker in the dashboard works regardless of role.

---

### `GET /api/agents/whoami`
Allows an agent to look up its own registration record using its bearer token. Not a UI-facing endpoint.

**Access:** (Agent-facing)

---

### `POST /api/agents/heartbeat`
Receives a heartbeat from an agent, updating its last-seen timestamp and returning configuration updates (such as a new ingest URL or TLS bundle). Called automatically every 60 seconds by each agent.

**Access:** (Agent-facing)

---

### `POST /api/agents/<agent_id>/approve`
Approves a pending agent, allowing it to start pushing metrics and receive its TLS certificate bundle.

**Access:** [Admin]

---

### `POST /api/agents/<agent_id>/disable`
Disables an approved agent, stopping it from pushing data without removing its registration.

**Access:** [Admin]

---

### `DELETE /api/agents/<agent_id>`
Permanently removes an agent's registration record.

**Access:** [Admin]

---

### `POST /api/agents/<agent_id>/role-primary`
Designates the specified agent as the default agent for its provider type. Dashboard requests with no `?agent=` parameter will be routed here.

**Access:** [Admin]

---

### `POST /api/agents/<agent_id>/llama-pool`
Controls whether this agent participates in the llama.cpp load-balancing pool.

**Access:** [Admin]

**Body:** `{"in_pool": true}` or `{"in_pool": false}`

---

### `POST /api/agents/<agent_id>/cert-bundle`
Delivers a signed TLS certificate bundle to an approved agent. Called automatically during the approval flow; not a UI-facing endpoint.

**Access:** (Agent-facing)

---

### `POST /api/agents/<agent_id>/stream-token`
Issues a short-lived HMAC token that allows the browser to open an SSE stream directly to the agent. EventSource connections cannot carry custom headers, so this token is appended as a query parameter instead.

**Access:** Admin-gated. Issues a short-lived authentication token for SSE streams.

---

### `GET /api/agents/metrics`
Returns per-agent communication statistics: request counts, error rates, and latency.

**Access:** [Admin]

---

### `GET /api/fleet/<provider>/aggregate`
Returns aggregated metrics across all agents for the specified provider (`llama` or `lms`). Used by the LLM Overall tab to show fleet-wide GPU utilisation, throughput, and power.

---

### `POST /api/agents/<agent_id>/status-check`
Tests connectivity to the specified agent and returns a summary of whether the Manager can reach it.

**Access:** [Admin]

---

### `POST /api/agents/<agent_id>/restart`
Instructs the specified agent to restart its own process.

**Access:** [Admin]

---

### `GET /api/agents/<agent_id>/config-file`
Reads the raw YAML configuration file from the specified agent.

**Access:** [Admin]

---

### `PUT /api/agents/<agent_id>/config-file`
Writes a new YAML configuration file to the specified agent.

**Access:** [Admin]

**Body:** The full YAML content of the config file as a JSON-wrapped string or raw text.

---

### `GET /api/agents/<agent_id>/log/tail`
Returns the most recent lines from the specified agent's log.

**Access:** [Admin]

---

### `POST /api/agents/global`
Updates global agent settings that apply to all agents (for example, default poll interval).

**Access:** [Admin]

---

### `GET /api/agent-tarball`
Downloads the agent installation tarball. Used by the Admin tab's self-update flow to push a new agent version.

**(Agent-facing)** Also used directly by the agent installer (`agent/install/install.sh --update`) to fetch the latest agent package; not intended for manual use.

---

### `POST /api/admin/push-ca-to-agents`
Pushes the current internal CA certificate to all approved agents so they can verify Manager HTTPS connections.

**Access:** [Admin]

---

### `GET /api/agents/<agent_id>/status`
Returns detailed status for a single agent: version, uptime, capabilities, last heartbeat, TLS state, and metric buffer depth.

**Access:** [Admin]

---

## Remote Data Push

These endpoints receive live data pushed by agents. They are not intended for manual use.

### `POST /api/remote/host-metrics`
Legacy endpoint: receives a host metrics snapshot from an agent. Superseded by `/api/remote/provider-state` but kept for backward compatibility with older agents.

**Access:** (Agent-facing)

---

### `POST /api/remote/provider-state`
Receives the current provider state (llama or LMS) from an agent, including model name, slots, throughput, and server state. This is the current primary path for live dashboard updates.

**Access:** (Agent-facing)

---

### `POST /api/remote/lmstudio`
Receives the LM Studio dashboard payload (model list, server status, active model metrics) from the LM Studio agent.

**Access:** (Agent-facing)

---

### `GET /api/remote/host-metrics/last`
Returns the most recently received host metrics snapshot for the queried agent. Useful for scripts that want the latest values without subscribing to a stream.

**Parameters:** `?agent=<agent_id>`

---

## Terminal

These endpoints provide browser-based terminal access. Each session is isolated and must be explicitly closed when no longer needed.

### `POST /api/terminal/create`
Opens a new PTY (pseudo-terminal) shell session on the Manager host. Returns a session ID used by all other terminal endpoints.

---

### `POST /api/lms/terminal/create`
Opens an SSH shell session to the LM Studio host. Returns a session ID.

---

### `GET /api/terminal/output/<sid>`
Opens an SSE stream delivering terminal output for the session. Each event contains a chunk of terminal bytes (may include ANSI escape sequences).

---

### `POST /api/terminal/input/<sid>`
Sends keystrokes to the terminal session.

**Body:** `{"data": "<characters to send>"}`

---

### `POST /api/terminal/resize/<sid>`
Resizes the terminal window, signalling the running process to reflow output.

**Body:** `{"rows": <int>, "cols": <int>}`

---

### `POST /api/terminal/close/<sid>`
Closes the terminal session and cleans up the PTY process.

---

## OpenClaw Analytics

### `GET /api/openclaw/analytics`
Returns Claude Code session analytics derived from the session log files on the Manager host: token usage, cost trends, tool attribution, daily cost history, velocity metrics, and anomaly detection. Results are cached for a short period to avoid re-parsing all session files on every request.

---

## Dashboard Layout

### `GET /api/layout`
Returns the saved dashboard layout: card order, hidden cards, LMS card order, Overall tab card order, borrowed cards, and the active theme name.

---

### `POST /api/layout`
Saves the current dashboard layout. The frontend calls this automatically whenever the user drags a card, hides a card, or changes the theme.

**Body:** A layout JSON object with `order`, `hidden`, `lmsOrder`, `overallOrder`, `overallBorrowed`, and `theme` fields.

---

## Admin

These endpoints require an admin-role session.

### `GET /api/admin/system-health`
Returns a rolled-up health summary of the whole system: agent connectivity, service availability, TLS certificate expiry, InfluxDB status, and recent error counts. Powers the red/green Admin tab indicator dot.

**Access:** [Admin]

---

### `GET /api/admin/auth`
Returns the current authentication mode (`required`, `trusted_cidr`, `disabled`, or `auto`) and whether the default credential is still active.

**Access:** [Admin]

---

### `POST /api/admin/auth`
Updates the authentication mode. When the mode is set in the TOML configuration file (rather than `auto`), this call returns a `restart_required` flag and the `systemctl restart` command to apply the change.

**Access:** [Admin]

**Body:** `{"mode": "required"}` (or `trusted_cidr` / `disabled`)

---

### `GET /api/admin/users`
Returns the list of all user accounts with their role, enabled/disabled status, and lockout state.

**Access:** [Admin]

---

### `POST /api/admin/users`
Creates a new user account.

**Access:** [Admin]

**Body:** `{"username": "<name>", "password": "<initial password>", "role": "admin" | "operator"}`

---

### `PATCH /api/admin/users/<username>`
Updates a user's role or enabled/disabled status.

**Access:** [Admin]

**Body:** Any combination of `{"role": "admin" | "operator", "disabled": true | false}`

---

### `DELETE /api/admin/users/<username>`
Deletes a user account. The system prevents deleting the last enabled admin account or your own account.

**Access:** [Admin]

---

### `POST /api/admin/users/<username>/unlock`
Clears a lockout on a user account that was locked after too many failed login attempts.

**Access:** [Admin]

---

### `GET /api/admin/llama-models`
Returns the model registry for llama.cpp agents: which models each agent has available, with saved configs and benchmark results.

**Access:** [Admin]

---

### `POST /api/admin/llama-pins`
Pins a specific model to a specific agent so that requests for that model are always routed to that agent regardless of the default selection.

**Access:** [Admin]

**Body:** `{"model_id": "<id>", "agent_id": "<id>"}`

---

### `POST /api/admin/export/manager`
Exports an encrypted backup of the Manager configuration, including agent registry, model profiles, and authentication settings. Returns a downloadable archive file.

**Access:** [Admin]

---

### `POST /api/admin/import/manager/preview`
Validates an encrypted config backup archive and returns a summary of what it contains and what would change if applied. Does not modify anything.

**Access:** [Admin]

**Body:** The encrypted archive file as a multipart upload.

---

### `POST /api/admin/import/manager/apply`
Applies a previously previewed config backup. Overwrites the current configuration with the archive contents.

**Access:** [Admin]

**Body:** The encrypted archive file as a multipart upload.

---

## Account (Self-Service)

These endpoints are available to any logged-in user regardless of role.

### `GET /api/me`
Returns the current user's username and role. Used by the frontend to decide which UI elements to show (for example, whether to display the Admin tab).

---

### `POST /api/account/password`
Changes the current user's own password. Requires the existing password to be provided.

**Body:** `{"current_password": "<current>", "new_password": "<new>"}`

---

## Model Profiles

Model profiles let you save named sets of llama.cpp server configuration values (context size, GPU layers, etc.) per model and switch between them quickly.

### `GET /api/llm/profiles`
Returns all saved profiles for all models, keyed by agent and model ID.

---

### `POST /api/llm/profiles/<model>/save`
Saves the current server configuration as a named profile for the specified model.

**Body:** `{"profile_name": "<name>"}`

---

### `POST /api/llm/profiles/<model>/activate`
Activates a saved profile, writing its configuration values to the server's config file.

**Body:** `{"profile_name": "<name>"}`

---

### `POST /api/llm/profiles/<model>/rename`
Renames a saved profile.

**Body:** `{"old_name": "<current name>", "new_name": "<new name>"}`

---

### `DELETE /api/llm/profiles/<model>/delete`
Deletes a saved profile for the specified model.

**Body:** `{"profile_name": "<name>"}`

---

## Authentication Pages

### `GET /login`
Serves the login page. If authentication is disabled or the request comes from a trusted network (when the mode is `trusted_cidr`), this redirects to the dashboard instead.

---

### `POST /login`
Submits login credentials. On success, sets the session cookie and redirects to the dashboard. On failure, returns the login page with an error.

**Body:** `{"username": "<name>", "password": "<password>"}` (form-encoded)

---

### `GET /logout`
Clears the session cookie and redirects to the login page. If authentication is disabled or not required for the current request, redirects to the dashboard instead.

---

## Proxy Routes

The Manager transparently proxies several external services, adding authentication and routing without exposing those services directly.

### `/proxy/llmchat/*`
Proxies requests to the llama.cpp built-in chat UI. Content-Security-Policy headers are stripped so the chat UI loads correctly through the proxy.

---

### `/proxy/openclaw/*`
Proxies requests to the local OpenClaw service. Only accessible when an OpenClaw process is running on the Manager host.

---

### `/proxy/imggen/*` and `/sdcpp/*`
Proxies requests to the stable-diffusion.cpp image generation server on the LM Studio host. Both prefixes map to the same upstream.

---

### `/api/alarm/*`
Proxies all Alarm Engine API calls. Every endpoint in the **Alarm Engine** sections below is reached through this prefix. For example, `GET /api/alarm/alerts` reaches the Alarm Engine's alert listing endpoint.

---

### `/alarm/*`
Serves the Alarm Engine's single-page application (SPA). Navigating to `/alarm/` in a browser opens the dedicated Alarm Engine UI.

---

### `GET /ws/alarm`
Upgrades to a WebSocket connection and bridges to the Alarm Engine's live alert event stream. The Manager runs a dedicated WebSocket proxy on a separate port so the browser does not need to trust the internal CA certificate. Events include `alert_created`, `alert_updated`, `alert_acknowledged`, and `alert_resolved`.

---

## Alarm Engine — Alerts

All Alarm Engine endpoints are accessed through the `/api/alarm/` proxy prefix described above.

### `GET /api/alarm/alerts`
Returns a list of alerts. By default only active and acknowledged alerts are returned; pass `include_closed=true` to also include closed ones.

**Parameters:**
- `?status=` — filter by status (`active`, `acknowledged`, `closed`, `ignored`)
- `?severity=` — filter by severity (`critical`, `warning`, `info`)
- `?rule_id=` — filter to alerts raised by a specific rule
- `?metric_name=` — filter to alerts for a specific metric
- `?only_active=true` — return only active/unresolved alerts
- `?include_closed=true` — include closed alerts in the result set
- `?limit=` — maximum number of results (default 100, max 1000)

---

### `GET /api/alarm/alerts/active`
Returns only currently active (firing, unacknowledged) alerts.

---

### `GET /api/alarm/alerts/counters`
Returns alert counts broken down by status and severity. Used by the dashboard badge and Events tab indicator.

---

### `GET /api/alarm/alerts/export`
Downloads all alerts as a JSON file, useful for audit or analysis.

---

### `GET /api/alarm/alerts/<alert_id>`
Returns full detail for a single alert, including its history of state changes.

---

### `POST /api/alarm/alerts/<alert_id>/read`
Marks an alert as read (seen) without changing its status.

---

### `POST /api/alarm/alerts/<alert_id>/acknowledge`
Acknowledges a firing alert, indicating that an operator is aware of it. The alert remains in the system until it resolves or is closed.

---

### `POST /api/alarm/alerts/<alert_id>/close`
Closes a resolved alert, removing it from the active view. Only resolved alerts can be closed.

---

### `POST /api/alarm/alerts/<alert_id>/ignore`
Ignores an alert, suppressing future notifications for it.

---

### `DELETE /api/alarm/alerts/<alert_id>`
Permanently deletes an alert record.

---

### `POST /api/alarm/alerts/close-all`
Closes all alerts that are currently in the resolved state.

---

### `POST /api/alarm/alerts/bulk`
Performs an action on multiple alerts in one call.

**Body:** `{"action": "acknowledge" | "close" | "ignore", "alert_ids": ["<id>", ...]}`

---

### `POST /api/alarm/alerts/ignore-all`
Ignores all currently firing alerts.

---

## Alarm Engine — Alarm Rules

### `GET /api/alarm/rules`
Returns all configured alarm rules with their thresholds, severity levels, and enabled/disabled status.

---

### `POST /api/alarm/rules`
Creates a new alarm rule.

**Body:**
```json
{
  "name": "GPU temperature too high",
  "description": "Optional explanation",
  "metric_source": "gpu",
  "metric_name": "temperature_celsius",
  "rule_type": "threshold_above",
  "config": {
    "threshold": {
      "value": 85.0,
      "warning": 80.0,
      "critical": 90.0
    }
  },
  "severity": "warning",
  "enabled": true,
  "notification_channel_ids": [],
  "auto_resolve_cycles": 2
}
```
- `metric_source`: `gpu`, `cpu`, `ram`, `disk`, `network`, `psu`
- `rule_type`: `threshold_above` (alert when value exceeds threshold), `threshold_below` (alert when value falls below), `threshold_range` (alert outside a range)
- `severity`: `info`, `warning`, `critical`
- `auto_resolve_cycles`: number of consecutive OK evaluations before auto-closing the alert (0 = never auto-close)

---

### `GET /api/alarm/rules/<rule_id>`
Returns the full definition of a single rule.

---

### `PUT /api/alarm/rules/<rule_id>`
Updates an existing rule's definition.

**Body:** The same shape as the create body; all fields are replaced.

---

### `DELETE /api/alarm/rules`
Deletes all alarm rules. Use with caution — this cannot be undone.

---

### `DELETE /api/alarm/rules/<rule_id>`
Deletes a single alarm rule.

---

### `PATCH /api/alarm/rules/<rule_id>/toggle`
Toggles a rule between enabled and disabled without deleting it. Disabled rules are not evaluated against incoming metrics.

---

## Alarm Engine — Notifications

### `GET /api/alarm/notifications/channels`
Returns all configured notification channels (email, webhook, Discord).

---

### `POST /api/alarm/notifications/channels`
Creates a new notification channel.

**Body — email channel:**
```json
{
  "name": "My Email Channel",
  "channel_type": "email",
  "config": {
    "email": {
      "to_email": "alerts@example.com",
      "subject_prefix": "[ALARM]"
    }
  },
  "enabled": true
}
```

**Body — webhook channel:**
```json
{
  "name": "My Webhook",
  "channel_type": "webhook",
  "config": {
    "webhook": {
      "url": "https://your-endpoint.example.com/hook",
      "method": "POST",
      "headers": {}
    }
  }
}
```

**Body — Discord channel:**
```json
{
  "name": "Discord Alerts",
  "channel_type": "discord",
  "config": {
    "discord": {
      "webhook_url": "https://discord.com/api/webhooks/..."
    }
  }
}
```

---

### `GET /api/alarm/notifications/channels/<channel_id>`
Returns the configuration for a single notification channel.

---

### `PUT /api/alarm/notifications/channels/<channel_id>`
Updates a notification channel's configuration.

**Body:** The same shape as the create body.

---

### `DELETE /api/alarm/notifications/channels/<channel_id>`
Deletes a notification channel.

---

### `GET /api/alarm/notifications/configs`
Returns all notification policies — the rules that determine which channels receive which alerts at what severity.

---

### `POST /api/alarm/notifications/configs`
Creates a new notification policy.

**Body:** A policy object specifying which severity levels and rule tags trigger delivery to which channel.

---

### `GET /api/alarm/notifications/configs/<config_id>`
Returns a single notification policy.

---

### `PUT /api/alarm/notifications/configs/<config_id>`
Updates a notification policy.

**Body:** The same shape as the create body.

---

### `DELETE /api/alarm/notifications/configs/<config_id>`
Deletes a notification policy.

---

### `GET /api/alarm/notifications/delivery-history`
Returns the delivery log: a record of every notification attempt with its outcome (sent, failed, retrying) and timestamp.

---

### `POST /api/alarm/notifications/send`
Sends a notification immediately, bypassing policy evaluation. Useful for testing or manual escalation. Target either a saved policy (`config_id`) or a single channel (`channel_id`).

**Body:**
```json
{
  "title": "Disk almost full",
  "body": "The data volume is at 95% capacity.",
  "severity": "warning",
  "config_id": "<policy-id>",
  "channel_id": "<channel-id>",
  "metadata": {}
}
```
- `title` and `body` are required
- supply `config_id` (a notification policy) **or** `channel_id` (a single channel)
- `severity` and `metadata` are optional

---

### `POST /api/alarm/notifications/test`
Sends a test message through a channel to verify it is configured correctly.

**Body:** `{"channel_id": "<id>"}`

---

## Alarm Engine — Metrics

### `GET /api/alarm/metrics`
Queries the time-series metric store. Returns data points for dashboard history and analysis.

**Query parameters:**
- `source` — (optional) filter to a specific metric source (e.g. `gpu`, `cpu`, `ram`, `disk`, `network`, `psu`)
- `hostname` — (optional) filter to a specific host
- `limit` — (optional, default 1000) maximum number of results to return

---

### `POST /api/alarm/metrics`
Ingests a single metric data point.

**Access:** Requires the ingest bearer token when one is configured.

**Body:** A single `MetricPoint` object with `source`, `metric_name`, `value`, `timestamp`, and `tags`.

---

### `POST /api/alarm/metrics/batch`
Ingests a batch of metric data points in one call. This is the primary path used by agents — batching reduces per-request overhead.

**Access:** Requires the ingest bearer token when one is configured.

**Body:** `{"points": [<MetricPoint>, ...]}`

---

### `POST /api/alarm/metrics/ingest`
Alternative single-point ingest path provided for compatibility with certain forwarding setups.

**Access:** Requires the ingest bearer token when one is configured.

**Body:** A single `MetricPoint` object.

---

### `GET /api/alarm/metrics/export`
Downloads all stored metrics as a file, useful for backup or external analysis.

---

### `GET /api/alarm/metrics/<source>/<metric_name>`
Returns the time-series history for a specific metric from a specific source host. Used by dashboard chart backfill.

**Query parameters:**
- `since_minutes` — how far back to look, in minutes (default: 60)
- `limit` — maximum number of data points to return (default: 100 000)
- `hostname` — (optional) filter to a specific host

---

### `GET /api/alarm/metrics/<source>/<metric_name>/summary`
Returns summary statistics for a specific metric (min, max, mean, p95) over a query window without returning the full point-by-point history.

**Query parameters:**
- `window_minutes` — time window in minutes to summarize over (default: 60)

---

### `POST /api/alarm/ingest`
Receives an alert from an outside system and routes it into the alarm engine. The endpoint auto-detects the payload format — InfluxDB notification rules, Grafana alerting webhooks, or a generic JSON/YAML body — and maps it onto an internal alert. Useful for forwarding alerts from tools you already run into this dashboard's Events view.

**Access:** Requires the ingest bearer token when one is configured.

---

## OpenTelemetry (OTLP) Ingest

These endpoints accept telemetry from external pipelines that speak the OpenTelemetry protocol. They are served by the Alarm Engine directly (not under the `/api/alarm/` proxy prefix) and require the ingest bearer token when one is configured. Each payload is converted into metric points and stored alongside the fleet's own metrics.

### `POST /v1/metrics`
Ingests OpenTelemetry metrics (counters, gauges, histograms).

### `POST /v1/traces`
Ingests OpenTelemetry trace spans. Each span is recorded as a duration metric.

### `POST /v1/logs`
Ingests OpenTelemetry log records. Each record is recorded as a log-count metric.
