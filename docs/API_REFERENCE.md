# LLM Systems Manager — API Reference

This reference covers every HTTP endpoint exposed by the LLM Systems Manager and the Alarm Engine. It is written for operators, integration authors, and script writers who need to call endpoints directly — not for contributors reading the source code.

The Manager listens on port 5000 (HTTP) and optionally port 5443 (HTTPS). The Alarm Engine runs on port 8081 but is always accessed through the Manager's `/api/alarm/*` proxy — you should never need to call port 8081 directly. All endpoints in this document use the Manager as the entry point.

---

## Authentication

**Browser / UI sessions** authenticate via a login cookie. After `POST /login` succeeds, your browser holds a signed session cookie that is checked on every subsequent request. Sessions expire based on the configured lifetime (default: several days). A session created over the HTTPS listener is stored in a separate `__Secure-session` cookie with its own signing salt; plain-HTTP sessions keep the `session` cookie. The two are independent — a cookie minted on one scheme is not accepted on the other.

While the signed-in user still holds the shipped default password, **every** API returns `403 {"password_change_required": true}` until `POST /api/account/password` succeeds. Only `/login`, `/logout`, and `/api/account/password` are reachable in the meantime.

**Agent-to-Manager calls** authenticate with a bearer token issued at registration: `Authorization: Bearer <token>`. These are internal; you do not need to manage them as an operator.

**Admin-only endpoints** are marked **[Admin]** throughout this document. Reaching them requires both an admin-role session and (where configured) a request originating from an allowed admin network range. Operator-role sessions receive a 403 on admin endpoints.

**Ingest endpoints** on the Alarm Engine accept a separate shared bearer token (`ingest_token`) configured in `llm-systems.toml`. When it is blank the ingest surface is open; when set, agents must present it.

**Management endpoints** on the Alarm Engine — rules, alerts, notifications, config, `dbstats`, and the metrics read routes — accept a dedicated `management_token`, falling back to `ingest_token` when no management token is set. `admin/export` and `admin/import/*` accept **only** `management_token`, with no ingest-token fallback, and fail closed (403) when it is unset — those archives carry every configured secret. An engine with neither token configured runs its management and metrics-read surfaces open and logs an `ALARM ENGINE AUTH` warning at startup. Manager-proxied calls forward whichever token the manager is configured with, so this only matters when calling the Alarm Engine directly.

---

## Health

### `GET /health`
Unauthenticated liveness probe for external monitors and load balancers. Not gated by the login/session flow at all.

**Response:** `{"status": "ok", "version": "<manager version>", "uptime_s": <seconds since startup>}`

---

### `GET /health` (Alarm Engine)
The Alarm Engine's own liveness probe, served on its own port — not proxied through the Manager. Also pings InfluxDB and always returns 200 so a monitor can distinguish "process up" from "InfluxDB unreachable" via the body.

**Response:** `{"status": "ok", "version", "uptime_s", "auth": "open"|"enforced", "ingest_points_per_s", "influx_writes_per_s", "active_alerts", "evaluation_interval_s", "components": {"cache", "influxdb", "influxdb_ping_ms", "influxdb_version", "rule_eval_last_cycle_ms", "tls", "auth": {"management", "ingest", "loopback_only", "open_on_network", "bearer_ok"}}}`. This is what the Manager's `/api/admin/system-health` polls to derive the alarm-engine row.

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
- `?fleet=llama`, `?fleet=lms`, or `?fleet=vllm` — return aggregated history for all agents of that provider type (CPU/RAM/GPU values are aggregated across every agent of that type).

---

### `GET /api/alert`
Returns the current active alert count and the highest severity level in effect. Used by the dashboard header to show the alert badge.

---

### `GET /api/config`
Returns the current polling interval and basic server configuration the frontend needs to self-configure (for example, which provider tabs to show).

**Response fields (polling):** `poll_interval` (effective seconds), `interval_mode` (`auto` | `manual`), `interval_override` (manual seconds or `null`), `interval_reason` (why auto picked its cadence, e.g. `llama awake`, `LMS active`, or `idle`), `poll_interval_idle` and `poll_interval_active` (the configured `[manager] poll_interval` / `fast_poll_interval`).

---

### `POST /api/config/interval`
Updates the dashboard polling interval for every viewer.

**Body:** `{"mode": "auto"}` or `{"mode": "manual", "value": <seconds 5–300>}` (values are clamped; agents sample every 5 s).

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
Returns the llama.cpp server configuration (context window size, GPU layer count, thread count, and other runtime parameters) for the currently active model. There is no query-param variant for reading another model's saved config — use `GET /api/llm/profiles` to see all saved profiles, or `POST /api/llm/config` to write one.

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

### `GET/POST /api/llm/server/svcconfig`
Reads (GET) or writes (POST) the llama-server systemd unit's `ExecStart` arguments directly, for flags not exposed through `/api/llm/config`. POST daemon-reloads the unit and can restart it.

**Body (POST):** A JSON object of `ExecStart` argument overrides.

---

### `.../stream-info` — direct-SSE handoff endpoints
Several of the SSE endpoints above have a sibling `.../stream-info` route that mints a short-lived HMAC-signed token and returns the direct agent stream URL instead of opening the stream itself: `GET /api/llm/server/log/stream-info`, `GET /api/llm/download/stream-info`, `GET /api/llm/build/stream-info`, `GET /api/llm/autotune/stream-info`, and `GET /api/llama-state/stream-info`. The browser uses the returned URL to connect straight to the agent, bypassing the Manager's own SSE proxy pool; when the direct path isn't usable (agent down, no direct port, or a mixed-content HTTPS page) the response signals the browser to fall back to the proxied `.../stream` endpoint instead.

**Response:** `{"ok": true, "url": "<agent-direct-url>?token=<token>", "expires_in": <seconds>}` on success, or `{"ok": false, ...}` when direct streaming isn't available.

`/api/llama-state/stream-info` is the exception: it returns `{"enabled": true, "url": ...}` or `{"enabled": false}`, with no `expires_in`.

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

## vLLM Control

These endpoints control the vLLM inference server, mirroring the llama.cpp control surface above.

### `GET /api/vllm/metrics`
Returns the latest vLLM sample for the default (or `?agent=`) vLLM agent.

---

### `GET /api/vllm/models`
Lists all model files available on the vLLM host's model storage path.

---

### `GET /api/vllm/server/status`
Returns whether the vLLM server process is currently running.

---

### `POST /api/vllm/server/start`
Starts the vLLM server process.

---

### `POST /api/vllm/server/stop`
Stops the vLLM server process.

---

### `POST /api/vllm/server/restart`
Stops then starts the vLLM server in one call.

---

### `GET /api/vllm/server/log`
Returns the most recent lines from the vLLM server's journal.

---

### `GET /api/vllm/log/stream`
Opens an SSE stream that tails the vLLM server log in real time.

---

### `GET/POST /api/vllm/server/svcconfig`
Reads (GET) or writes (POST) the vLLM systemd unit's `ExecStart` arguments — the vLLM equivalent of `/api/llm/server/svcconfig`.

**Body (POST):** A JSON object of `ExecStart` argument overrides.

---

### `POST /api/vllm/lora/load`
Loads a LoRA adapter into the running vLLM server.

**Body:** LoRA load parameters (adapter path/name); passed through to the agent.

---

### `POST /api/vllm/lora/unload`
Unloads a LoRA adapter from the running vLLM server.

**Body:** LoRA unload parameters; passed through to the agent.

---

### `POST /api/vllm/autotune/run`
Starts the `--max-model-len` Auto-Tune wizard for vLLM, which finds the largest context length the currently loaded model can sustain. Progress is streamed via `/api/vllm/autotune/stream`.

---

### `GET /api/vllm/autotune/stream`
Opens an SSE stream reporting vLLM Auto-Tune progress.

---

### `POST /api/vllm/autotune/cancel`
Cancels an in-progress vLLM Auto-Tune run.

---

### `POST /api/vllm/bench/run`
Starts a `vllm bench serve` benchmark run against the currently loaded model. Progress is streamed via `/api/vllm/bench/stream`.

---

### `GET /api/vllm/bench/stream`
Opens an SSE stream reporting vLLM benchmark progress.

---

### `POST /api/vllm/bench/cancel`
Cancels an in-progress vLLM benchmark run.

---

### `POST /api/vllm/terminal/create`
Opens an SSH shell session to the vLLM host, mirroring `/api/lms/terminal/create`. Returns a session ID used with the shared `/api/terminal/*` endpoints below.

---

## Inference Gateway

An OpenAI-compatible chat/completions gateway that fans requests out to whichever backend provider (llama.cpp, LM Studio, or vLLM) is serving the requested model. Requests authenticate either with a bearer token from `[manager.gateway].api_keys`, or with a normal dashboard session cookie.

### `POST /api/gateway/v1/chat/completions`
OpenAI-compatible chat completion. The target provider is resolved from the request body's `model` field: a model pin wins first, then the live model index built from each provider's `/models` listing, then a `llama` fallback if the model is unrecognized. Within the resolved provider, the host is picked in the order: model pin, then an explicit `?agent=`, then pool round-robin, then the provider default. A pin therefore overrides an explicit `?agent=`; the gateway logs when that happens. (The dashboard's own proxy routes surface the same condition as an `X-Routing-Override: pin` response header; the gateway does not set it.) Supports `"stream": true` for SSE responses.

A miss against a cold or stale model index kicks off a background refresh and waits up to 5 s for it to land before falling back to `llama`; the refresh itself is single-flight, so concurrent completions and `GET /api/gateway/v1/models` calls share one in-flight fan-out rather than each triggering their own.

Successful non-streaming responses carry an `X-Proxied-To: <agent_id prefix>@<hostname>` header identifying which agent actually served the request; streaming responses carry the same header on the initial SSE response.

---

### `POST /api/gateway/v1/completions`
OpenAI-compatible legacy completion endpoint. Same provider-resolution and `X-Proxied-To` behavior as `/api/gateway/v1/chat/completions`.

---

### `GET /api/gateway/v1/models`
Returns the merged OpenAI-style model list (`{"object": "list", "data": [...]}`) across every gateway-enabled provider's pool (currently `llama`, `lms`, `vllm`).

---

### Per-provider gateway twins
Every gateway-enabled provider also gets its own fixed-provider mirror of the three routes above, skipping model-based provider resolution: `POST /api/gateway/<provider>/v1/chat/completions`, `POST /api/gateway/<provider>/v1/completions`, and `GET /api/gateway/<provider>/v1/models`, for `<provider>` in `llama`, `lms`, `vllm`.

---

## GPU Report Card

Runs a standardized benchmark ("report card") against a reference model on a chosen agent/provider, to produce comparable tokens/sec and $/Mtok numbers across hardware. Report card jobs run asynchronously and stream progress over SSE, similar to the benchmark endpoints in the LLM Control section.

### `GET /api/reportcard/preset`
Returns the report card's fixed run parameters: `preset_version`, `gen_tokens`, `reps`, the supported `providers`, the configured `price_kwh`, and the list of reference `models` (`{"key", "label"}`) available for standard runs.

---

### `POST /api/reportcard/run`
Starts a report card run. In standard mode, first checks whether the reference model is ready on the target agent — if a confirmation or download is needed, returns that status instead of starting the job; the caller resubmits with `confirm_vllm`/`confirm_download` set to proceed.

**Body:** `{"agent": "<agent_id>", "provider": "llama"|"lms"|"vllm", "mode": "standard"|"custom", "model": "<model_id>" (custom mode), "model_key": "<reference key>" (standard mode), "price_kwh": <number> (optional), "confirm_vllm": <bool>, "confirm_download": <bool>}`

**Response:** `{"ok": true, "job_id": "<id>"}` once the job is started, or `{"ok": true, "status": "needs_confirm"|"needs_download", ...}` when a precheck blocks the run.

---

### `GET /api/reportcard/models`
Returns the model IDs currently available on a given agent/provider.

**Parameters:** `?agent=<agent_id>&provider=<llama|lms|vllm>` (both required)

---

### `POST /api/reportcard/delete-model`
Deletes a reference model from an agent's local storage/cache. Supported only for `llama` and `lms`.

**Body:** `{"agent": "<agent_id>", "provider": "llama"|"lms", "model_key": "<reference key>"}`

---

### `POST /api/reportcard/cancel/<job_id>`
Cancels an in-progress report card job.

---

### `GET /api/reportcard/stream/<job_id>`
Opens an SSE stream reporting progress for a report card job. Closes when the job emits a `done`, `error`, or `cancelled` event, or after an internal timeout.

---

### `GET /api/reportcard/latest`
Returns the most recent saved report card for an agent/provider pair.

**Parameters:** `?agent=<agent_id>&provider=<llama|lms|vllm>` (both required)

---

### `GET /api/reportcard/history`
Returns saved report card history, optionally filtered.

**Parameters:** `?agent=<agent_id>`, `?provider=<llama|lms|vllm>`, `?model=<model_id>` — all three required; omitting any returns 400.

---

### `DELETE /api/reportcard/history`
Clears all saved report card history. Backs the Tools launcher's *Clear history* action.

---

### `GET /api/reportcard/recent`
Returns the most recent saved report cards across all agents/providers, trimmed to the fields the Tools launcher's tiles need.

**Parameters:** `?limit=<n>` (default 12, max 100)

**Response:** `{"ok": true, "cards": [{"ts", "agent_id", "provider", "eligible", "result": {"model", "gen_tps", "avg_watts", "usd_per_mtok", "gpu_config"}}, ...]}`

---

## Tools Run Ledger

Cross-tool run history behind the LLM Control → Tools launcher: every completed Report Card, Benchmark, and Autotune run is recorded here, fleet-wide and server-side, so any browser or a closed tab still sees the result.

### `POST /api/tools/runs`
Records one completed tool run. Reachable with a normal dashboard session, or with an approved agent's machine bearer token for the agent's own push path (a machine token may only `POST`; `GET` and `DELETE` return 403 for it).

**Body:** `{"tool": "benchmark"|"autotune", "model_id": "<id>", "provider": "<provider>", "ok": <bool>, "run_id": "<id>", ...}` — `provider` is checked against the registered-provider whitelist; any other body fields are kept as a summary, capped at 24 keys, 200 characters per string, and finite numbers only. A machine-token caller may name its own `agent_id`; a browser request is always attributed to whichever agent the call proxies to. `run_id` is used to de-duplicate the agent's own push against a browser's report of the same run.

---

### `GET /api/tools/runs`
Returns the newest rows across all tools, plus per-tool totals and the newest row per tool.

**Parameters:** `?limit=<n>` (default 100, max 100)

**Response:** `{"runs": [...], "totals": {"<tool>": <count>}, "latest": {"<tool>": {...}}}`

---

### `DELETE /api/tools/runs`
Clears the run ledger.

---

### `GET /api/tools/activity`
Returns which tools are running right now, fleet-wide — the manager's own proxy record for an in-flight run, confirmed (or expired) against a background probe of the agent's `/<provider>/tools/state`. Backs the run-activity dot on the LLM Control → Tools sub-tab.

---

## Energy & Cost

Rolls up power-draw metrics into cost figures over configurable time windows, using the configured `$/kWh` and cloud comparison pricing.

> **LM Studio token telemetry comes only from the manager's OpenAI gateway proxy** (`/api/gateway/...`). Requests sent directly to an LM Studio server bypass the gateway's usage counters and are invisible to the energy $/Mtok and cloud-savings math — a low or zero savings figure on a direct-traffic setup reflects missing telemetry, not missing savings. The gateway counters reset on manager restart; the hourly accumulator handles that reset without double-counting. Hourly rows are kept forever by default; set `manager.energy.retention_days` to prune old rows.

### `GET /api/energy/summary`
Returns an energy/cost summary for a time window: total energy, local $ cost, and equivalent cloud-provider cost comparison.

**Parameters:**
- `?days=<n>` or `?month=<YYYY-MM>` — select the summary window (mutually exclusive with the default trailing window)
- `?price_kwh=<number>` — override the configured electricity price for this call
- `?cloud_in=<number>` / `?cloud_out=<number>` — override the configured cloud $/Mtok input/output pricing for comparison

---

### `GET /api/energy/hourly`
Returns hourly energy/cost data points for charting.

**Parameters:**
- `?days=<n>` or `?month=<YYYY-MM>` — mirrors the summary window
- `?hours=<n>` — trailing-window form used when `days`/`month` are absent (default 168, capped)
- `?agent=<agent_id>` — restrict to one agent

---

## Model Autopilot

Automates placement of model entries across the agent pool — deciding which host(s) should serve which model, proposing changes, and (optionally) applying them. All autopilot endpoints require an admin session.

### `GET /api/autopilot`
Returns the current autopilot state, any pending proposals, the last plan timestamp, and per-entry status.

**Access:** [Admin]

**Response:** `{"state": <state document>, "proposals": [...], "last_plan_ts": <epoch seconds>, "entry_status": {...}}`

---

### `PUT /api/autopilot`
Replaces the autopilot state document. The submitted document is validated before being saved — invalid entries are rejected with a 400 and an error message.

**Access:** [Admin]

**Body — state document:**
```json
{
  "enabled": true,
  "entries": [
    {
      "model": "<model_id>",
      "provider": "llama",
      "placement": "auto",
      "failover": "semi",
      "priority": 100,
      "min_replicas": 1,
      "max_replicas": 1,
      "size_mb": 8192,
      "autoscale": {"target_saturation": 0.75, "up_window_s": 120, "down_window_s": 900}
    }
  ],
  "hosts": {}
}
```
- `provider`: `llama`, `vllm`, or `lms`
- `placement`: `"auto"` or a specific agent id
- `failover`: `"semi"` (propose, wait for apply) or `"auto"` (apply automatically)
- `min_replicas` / `max_replicas`: replica bounds; `max_replicas > min_replicas` enables `autoscale`
- `size_mb`: optional explicit model size override used for placement sizing
- `hosts`: reserved; any submitted value is currently ignored (idle sleep is llama-server's own `--sleep-idle-seconds`, not autopilot-managed)

---

### `POST /api/autopilot/proposals/<pid>/apply`
Applies a pending proposal (executes the placement/pool/pin changes it describes).

**Access:** [Admin]

---

### `POST /api/autopilot/proposals/<pid>/dismiss`
Dismisses a pending proposal without applying it.

**Access:** [Admin]

---

### `POST /api/autopilot/tick`
Manually triggers one reconciler tick (observe current state, replan, refresh proposals) outside of its normal schedule.

**Access:** [Admin]

---

## Agent Management

These endpoints manage the pool of monitoring agents. Most are **[Admin]** only. A small number are called internally by agents themselves (marked "Agent-facing") and are not intended for manual use.

### `GET /api/agents`
Returns the list of all registered agents with their status, capabilities, and last-seen timestamp, plus `manager_version` and `collect_interval_s` (the configured metrics-collection interval).

**Access:** [Admin]

---

### `POST /api/agents/register`
Registers a new agent with the Manager. Called automatically by the agent on first start; not a UI-facing endpoint. For a re-registration (same hostname + OS as an existing record), an agent at `v2026.09.04-1` or newer must present its `fingerprint` body field (or a prior bearer token) to re-authenticate; source-IP alone is accepted only for records last written by an older agent.

**Access:** (Agent-facing)

---

### `GET /api/agents/list-by-provider`
Returns agents grouped by provider type (llama, lms, vllm). Available to all authenticated users, including operators, so the agent picker in the dashboard works regardless of role.

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

### `POST /api/agents/<agent_id>/host-role`
Designates (or clears) the specified agent as the Manager's own host agent — the approved agent running on the same machine as the Manager. Used so agent-derived host metrics and version pills resolve correctly even under Docker, where the Manager can't introspect its own host directly.

**Access:** [Admin]

**Body:** `{"set": true}` (default) or `{"set": false}` to clear.

---

### `POST /api/agents/<agent_id>/collection`
Pauses or resumes metric collection on the specified agent without disabling or removing it.

**Access:** [Admin]

**Body:** `{"enabled": true}` or `{"enabled": false}`

---

### `POST /api/agents/<agent_id>/<provider>-pool`
Controls whether this agent participates in the given provider's load-balancing pool. This is not a fixed path — one route is registered per pool-enabled provider (currently `llama`, `lms`, and `vllm`), so the actual paths are `/api/agents/<agent_id>/llama-pool`, `/api/agents/<agent_id>/lms-pool`, and `/api/agents/<agent_id>/vllm-pool`.

**Access:** [Admin]

**Body:** `{"in_pool": true}` or `{"in_pool": false}`, plus an optional `"position"` (integer index) to place the agent at a specific slot in the pool order.

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
Returns aggregated metrics across all agents for the specified provider (`llama`, `lms`, or `vllm`). Used by the Overall tab to show GPU utilisation, throughput, and power aggregated across every agent of that provider type.

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
No-auth endpoint an agent polls to learn whether it has been approved yet. Returns `{"ok": true, "status": "pending"|"approved"|...}`, and includes the agent's bearer token in the response once approved — but only when the caller re-authenticates as that agent. Agents at `v2026.09.04-1` or newer must present the `X-Agent-Fingerprint` header (or a prior bearer token) to receive the token; older agents keep re-authenticating by source IP.

**Access:** (Agent-facing, unauthenticated by path — never gated by the login flow)

---

### `GET /api/agents/<agent_id>/log/stream`
Opens an SSE proxy stream of the specified agent's own process log (the agent daemon's log, not a provider's log). Streams bytes verbatim from the agent's `/agent/log/stream`.

**Access:** [Admin]

---

### `POST /api/agents/<agent_id>/self-update`
Triggers an in-place agent self-update: the agent runs its installer with `--update --from-self-update` (git pull, redeploy code, refresh its venv — no systemd unit changes) and streams stdout/stderr back over SSE. On success the agent exits and systemd's `Restart=always` brings the updated code back up.

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

## PWA Companion

Installable phone companion (#522). The manifest, service worker, and app icons are served without authentication (browsers fetch them without credentials); everything else is session-gated like the rest of the API.

### `GET /companion`
The companion shell page. Unauthenticated browsers are redirected to `/login`.

---

### `GET /manifest.webmanifest`
Web app manifest (`application/manifest+json`): `start_url` `/companion`, scope `/`, standalone display, 192/512 px icons. **Open (no auth).**

---

### `GET /sw.js`
The service worker, stamped with the running manager version (the app-shell cache name keys off it) and served with `Cache-Control: no-cache`. **Open (no auth).**

---

### `GET /api/companion/release`
Release-availability status for the companion's Admin screen. Always returns `{ok, enabled, build, installed, describe, ahead, source, install_kind, repo}`; when the check is enabled it adds `latest` and `update_available` (`true`/`false`/`null`, with a `note` when the install has no release tag to compare — as a last resort the running build stamp is matched against the newest release's notes). The GitHub query result is cached for 24 hours (toggling the switch forces a fresh read) and runs only when enabled (`[manager.companion] release_check`, default off).

---

### `PUT /api/companion/release`
**Body:** `{"enabled": true|false}`. Toggles the release check at runtime (the Settings-screen switch); overrides the config value until restart. Returns 400 without `enabled`.

**Access:** [Admin]

---

### `POST /api/companion/push/notify`
Alarm-engine bridge (#538): fans one alert out to every subscribed device. **Bearer-gated**, not session-gated — the caller presents `[manager.companion] push_notify_token` (falling back to `[alarm_engine].management_token`, then `ingest_token`); with no token configured anywhere, an admin session is required instead. **Body:** `{"title", "body", "severity", "tag", "url"}` (all optional, length-capped; `url` is normalized to a same-origin path). Returns `{ok, subscriptions, sent, failed, pruned}`, 400 on a non-object body, 503 when `pywebpush` is not installed.

---

### `GET /api/companion/push/public-key`
Returns `{ok, key}` — the VAPID application server key (base64url) for `pushManager.subscribe()`. The underlying EC P-256 key pair is generated on first use into the manager data directory.

---

### `GET /api/companion/push/subscriptions`
Returns `{ok, count, endpoints}` — registered push subscriptions (endpoints truncated).

---

### `POST /api/companion/push/subscribe`
Stores a browser `PushSubscription` JSON (`endpoint` + `keys.p256dh` + `keys.auth`). Returns 400 on a malformed subscription or when the subscription cap (32) is reached.

---

### `POST /api/companion/push/unsubscribe`
**Body:** `{"endpoint": "...", "keys": {"auth": "..."}}`. Removes the matching subscription and returns `{ok, removed}`. Admins may remove any device by `endpoint` alone; every other session must include the subscription's own `keys.auth` (the value the browser's `PushSubscription.toJSON()` reports) and gets 403 otherwise. An endpoint that is not registered returns `{ok: true, removed: false}` for any session.

---

### `POST /api/companion/push/test`
Sends a test notification via web push (VAPID). **Body (optional):** `{"endpoint": "..."}` targets one subscription; non-admins must supply their own device's endpoint (403 without one). Admins get `{ok, sent, failed, pruned}` — subscriptions rejected upstream with 404/410 are pruned; non-admins get only `{ok}`. Returns 400 with no subscriptions, 404 for an unknown endpoint, and 503 when `pywebpush` is not installed. The VAPID contact claim comes from `[manager.companion] push_contact`.

---

## Admin

These endpoints require an admin-role session.

### `GET /api/admin/system-health`
Returns a rolled-up health summary of the whole system: agent connectivity, service availability, TLS certificate expiry, InfluxDB status, and recent error counts. Powers the red/green Admin tab indicator dot. Also carries connection counts, the WebSocket relay's state, the alarm engine's ingest/write rates and rule-evaluation time, its probe history, the count of agents with an update available, `ae_restart` (whether/how the Alarm Engine can be restarted from here), and — on the alarm-engine service entry — `auth`, `auth_detail`, and `bearer_configured` describing its auth posture as seen by the Manager.

**Access:** [Admin]

---

### `GET /api/admin/audit-log`
Returns paginated entries from the admin action audit log (who did what, from where, and the outcome).

**Access:** [Admin]

**Parameters:** `?limit=<n>` (default 100, max 500), `?offset=<n>` (default 0), `?q=<text>` (search across actor/action/target/ip/path/detail), `?group=<group>` (one of the audit event catalog's groups), `?actor=<username>|autopilot|system|local|test`, `?outcome=<outcome>`, `?since_hours=<n>`, `?sort=<ts|actor|action|target|ip|outcome|id>`, `?dir=<asc|desc>`, `?hide_automated=1` (drops rows tagged as automated traffic).

**Response:** `{"ok": true, "total": <count>, "page_size": <n>, "entries": [{"ts", "actor", "role", "ip", "auth": "session"|"token"|"bypass"|"test"|"internal", "method", "path", "action", "target", "status", "outcome", "event", "group", "label", "detail": {...} | null}, ...]}`. `detail` is a parsed object (e.g. settings old→new values, with secrets masked and the list clipped at 20 changes).

---

### `GET /api/admin/audit-log/stats`
Returns audit-log bookkeeping: total row count, the oldest row's timestamp, the distinct list of actors, the purge thread's last-run state, and the configured `retention_days` / `page_size`.

**Access:** [Admin]

---

### `GET /api/admin/audit-log/events`
Returns the audit event catalog: 7 groups, each with its member events and whether each is currently enabled (per `[manager.audit].disabled_events`), plus the current `retention_days`, `page_size`, `save_automated`, and `automated_actors` config values.

**Access:** [Admin]

---

### `GET /api/admin/audit-log.csv`
Downloads the audit log as CSV, honouring the same `q`/`group`/`actor`/`outcome`/`since_hours`/`sort`/`dir`/`hide_automated` filters as `GET /api/admin/audit-log`, capped at 10,000 rows. Spreadsheet-formula-triggering cells are neutralised before export.

**Access:** [Admin]

---

### `GET /api/admin/stream-stats`
Returns live SSE-stream and connection health for the Admin tab: Manager stream pool active/peak/refusal counts, Cheroot worker-thread and backlog stats, browser/agent connection counts, and per-agent `/status` stream state.

**Access:** [Admin]

---

### `GET /api/admin/backup-status`
Returns the scheduled-backup configuration (enabled, interval, retention) and the list of backups currently on disk. Each entry now carries `component` (`manager` or `alarm_engine`) and `run` (the run stamp shared by a run's manager and alarm-engine archives), alongside `file`, `bytes`, `mtime`, and `mirrored`. `last` (the most recent run) carries `partial` (true when the alarm-engine archive failed), `components.manager` / `components.alarm_engine` (each with `ok`/`file`/`bytes`/`error`/`remedy`/`skipped`), and `mirror_failed` (file names that failed to copy to the mirror directory). The top-level payload carries `not_covered` — why the alarm engine is outside scheduled runs, when it is. Retention (`keep_last`) now counts backup **runs**, not individual archive files, so `keep_last = 7` can retain up to 14 files.

**Access:** [Admin]

---

### `POST /api/admin/backup-now`
Runs one scheduled-backup cycle immediately, serialised with the scheduler so only one archive operation runs at a time. Returns 409 when scheduled backups are disabled.

**Access:** [Admin]

**Response:** `{"ok": <bool>, "last": {...}}` — the same per-run shape described under `GET /api/admin/backup-status`.

---

### `GET /api/admin/backup-archive/<name>`
Downloads one retained archive by exact file name, matched against the backup folder's own listing; a pruned or unknown name returns 404. Recorded in the audit log as `backup.download`.

**Access:** [Admin]

---

### `POST /api/admin/service/<svc>/restart`
Restarts the Manager or the (co-located) Alarm Engine service. On bare-metal installs this uses a sudoers `NOPASSWD systemctl restart` grant; under containers and Homebrew kegs it restarts by exiting the process so the supervisor respawns it — exit 0 in a container, exit 1 under a brew keg, whose units are `Restart=on-failure`. A co-located Alarm Engine restarts through its own management API rather than a process exit. Restarting the Alarm Engine this way only works when it runs on the same host as the Manager.

**Access:** [Admin]

**Path parameter:** `<svc>` is `manager` or `alarm_engine`.

---

### `GET /api/admin/auth`
Returns the current authentication mode (`required`, `trusted_cidr`, `disabled`, or `auto`) and whether the default credential is still active. Also returns `default_user` — the username of the built-in default admin account, so the UI can name it in the "default password in use" notice.

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

### `GET /api/admin/<provider>-models`
Returns the model registry for the given provider's agents: fans out to every pool member (or, if the pool is empty, every approved agent advertising that provider's capability) and returns which models each agent reports, plus any per-agent errors from the fan-out. This is not a fixed path — one route is registered per pool-enabled provider (currently `llama`, `lms`, and `vllm`): `/api/admin/llama-models`, `/api/admin/lms-models`, `/api/admin/vllm-models`.

**Access:** [Admin]

**Response:** `{"ok": true, "models": [{"id": "<model_id>", "agents": ["<hostname>", ...]}, ...], "errors": [{"agent": "<hostname>", "error": "<status or message>"}, ...]}`

---

### `POST /api/admin/<provider>-pins`
Pins a specific model to a specific agent so that requests for that model are always routed to that agent regardless of the default selection. Registered per provider that declares a pin dict — currently `/api/admin/llama-pins`, `/api/admin/lms-pins`, and `/api/admin/vllm-pins`.

**Access:** [Admin]

**Body:** `{"model_id": "<id>", "agent_id": "<id>"}` — omit or leave `agent_id` blank to clear the pin.

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

### `GET /api/admin/settings`
Returns the Settings catalog: every setting's current value, per-field secret status, and `restart_pending_paths` (which changed fields are waiting on a service restart to take effect). On a split install also returns `topology.split`, `topology.ae_config_reachable`, and — when the Alarm Engine's own config can't be read — `topology.ae_config_error` (`{"kind", "status", "detail", "remedy"}`).

**Access:** [Admin]

---

### `PUT /api/admin/settings`
Applies one or more setting changes.

**Access:** [Admin]

**Body:** `{"changes": {"<dotted.path>": <value>, ...}, "resync_ae": ["<dotted.path>", ...]}`. A value of `null` clears a field back to its default. `resync_ae` re-pushes a shared setting's current value to the Alarm Engine without changing it locally — only valid for settings marked `service: "both"` in the catalog.

**Response:** `{"ok": true, "applied": [...], "restart_paths": [...], "errors": {}}` — `restart_paths` names which of the applied fields need which service restarted to take effect.

---

### `GET /api/admin/gateway/flow`
Returns the live clients → gateway → hosts picture behind the Gateway sub-tab's Inference Gateway card: per-client last model, IP, request rate and an activity tier (`active`, `recent`, or `idle`), and per-host inflight/throughput figures with edge rates between them.

**Access:** [Admin]

---

### `PUT /api/admin/gateway`
Turns the inference gateway on or off. Applied through the settings path and hot-reloaded, so `/api/gateway/v1/*` flips without a restart. Recorded in the audit log as `config.gateway`.

**Access:** [Admin]

**Body:** `{"enabled": true}` or `{"enabled": false}`

---

## Account (Self-Service)

These endpoints are available to any logged-in user regardless of role.

### `GET /api/me`
Returns the current user's username and role. Used by the frontend to decide which UI elements to show (for example, whether to display the Admin tab).

---

### `POST /api/account/password`
Changes the current user's own password. Requires the existing password to be provided. The new password must be at least 8 characters. This is the one route reachable while the default-password wall is up.

**Body:** `{"current_password": "<current>", "new_password": "<new>"}`

**Errors:** A wrong current password returns `403 {"field": "current_password"}`.

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

### `GET /api/alarm-ws-ticket`
Issues a short-lived, single-use ticket authorizing one connection to `GET /ws/alarm`. `EventSource`/`WebSocket` connections can't carry a session cookie's normal headers across the separate WS proxy port, so the ticket is passed as a query parameter instead.

**Response:** `{"ticket": "<ticket>", "ttl_s": <seconds>}`

---

### `GET /ws/alarm`
Upgrades to a WebSocket connection and bridges to the Alarm Engine's live alert event stream. The Manager runs a dedicated WebSocket proxy on a separate port so the browser does not need to trust the internal CA certificate. Events include `alert_created`, `alert_updated`, `alert_acknowledged`, and `alert_resolved`. Requires a `?ticket=` from `GET /api/alarm-ws-ticket`; tickets are path-bound and cannot be reused on `/ws/openclaw`.

---

### `GET /api/openclaw-ws-ticket`
Issues a short-lived, single-use ticket authorizing one connection to `GET /ws/openclaw`. Same shape and TTL as `GET /api/alarm-ws-ticket`, but path-bound to `/ws/openclaw`.

**Response:** `{"ticket": "<ticket>", "ttl_s": <seconds>}`

---

### `GET /ws/openclaw`
Upgrades to a WebSocket connection and bridges to the OpenClaw gateway's control-UI WebSocket, using the same Manager-side bridge as `/ws/alarm`. The upstream target is the resolved OpenClaw host, and the browser's real `Origin` header is forwarded so the OpenClaw gateway's own allowed-origins check still applies. Requires a `?ticket=` from `GET /api/openclaw-ws-ticket`.

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

**Body:** A policy object specifying which severity levels and rule tags trigger delivery to which channel. Includes `toast_dismiss_seconds` (1–600, default 10) — how long a Toast-channel delivery stays on screen when `auto_dismiss` is on.

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
Returns the delivery log: a record of every notification attempt with its outcome (sent, failed, retrying) and timestamp. Each row's `metadata.alert_id` names the alert it was sent for, which backs the alert drawer's delivery timeline.

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

**Access:** Requires the management token (the ingest token is accepted as a fallback); open only when neither is configured.

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

**Access:** Requires the management token (the ingest token is accepted as a fallback); open only when neither is configured.

---

### `GET /api/alarm/metrics/<source>/<metric_name>`
Returns the time-series history for a specific metric from a specific source host. Used by dashboard chart backfill.

**Access:** Requires the management token (the ingest token is accepted as a fallback); open only when neither is configured.

**Query parameters:**
- `since_minutes` — how far back to look, in minutes (default: 60)
- `limit` — maximum number of data points to return (default: 100 000)
- `hostname` — (optional) filter to a specific host

---

### `GET /api/alarm/metrics/<source>/<metric_name>/summary`
Returns summary statistics for a specific metric (min, max, mean, p95) over a query window without returning the full point-by-point history.

**Access:** Requires the management token (the ingest token is accepted as a fallback); open only when neither is configured.

**Query parameters:**
- `window_minutes` — time window in minutes to summarize over (default: 60)

---

### `POST /api/alarm/ingest`
Receives an alert from an outside system and routes it into the alarm engine. The endpoint auto-detects the payload format — InfluxDB notification rules, Grafana alerting webhooks, or a generic JSON/YAML body — and maps it onto an internal alert. Useful for forwarding alerts from tools you already run into this dashboard's Events view.

**Access:** Requires the ingest bearer token when one is configured.

---

## Alarm Engine — Admin & Diagnostics

### `POST /api/alarm/admin/export`
Builds and returns the Alarm Engine's own encrypted backup archive (rules, channels, notification configs, alerts/history). This is what scheduled backups on the Manager call to cover the alarm engine.

**Access:** Requires the management token. No ingest-token fallback; fails closed (403) when no management token is configured, since the archive carries every configured secret.

**Body:** `{"password": "<passphrase>"}` — an empty password produces an unencrypted archive.

---

### `POST /api/alarm/admin/import/preview`
Validates an uploaded Alarm Engine backup archive and returns its manifest, entry list, and topology so the operator can confirm before applying.

**Access:** Requires the management token. No ingest-token fallback.

**Body:** The encrypted archive file as a multipart upload, plus its `password` form field.

---

### `POST /api/alarm/admin/import/apply`
Applies a previously previewed Alarm Engine backup archive, overwriting the current rules/channels/config with the archive contents. The engine must be restarted afterward for the change to take effect.

**Access:** Requires the management token. No ingest-token fallback.

**Body:** The encrypted archive file as a multipart upload, plus its `password` form field.

---

### `GET /api/alarm/dbstats/sqlite`
Returns size/pragma/row-count stats for the Alarm Engine's SQLite databases, backing the Database Performance card. Cached for up to 10 seconds across callers.

**Access:** Requires the management token (the ingest token is accepted as a fallback); open only when neither is configured.

---

## OpenTelemetry (OTLP) Ingest

These endpoints accept telemetry from external pipelines that speak the OpenTelemetry protocol. They are served by the Alarm Engine directly (not under the `/api/alarm/` proxy prefix) and require the ingest bearer token when one is configured. Each payload is converted into metric points and stored alongside the agents' own metrics.

### `POST /v1/metrics`
Ingests OpenTelemetry metrics (counters, gauges, histograms).

### `POST /v1/traces`
Ingests OpenTelemetry trace spans. Each span is recorded as a duration metric.

### `POST /v1/logs`
Ingests OpenTelemetry log records. Each record is recorded as a log-count metric.
