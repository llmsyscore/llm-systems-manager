# LLM Systems Manager — Features

Everything LLM Systems Manager does, with a screenshot of each screen. To install it, see the [README](../README.md#install) or the [Deployment guide](DEPLOYMENT.md).

## Everything that ships

- **Tower assistant.** An AI assistant in a drawer on every page that answers questions about hosts, models, alerts, energy, jobs and runs through a model the gateway already serves. It reads host and hardware details, metric history, loaded models and profiles, alarms, energy, gateway traffic, recent runs, service health, logs, configuration and the audit log. It can load, unload or wake a model, acknowledge, close or resume an alert, start a benchmark, cancel a job or restart a provider, each behind an approval prompt that is written to the audit log as `tower via <user>`. Capability levels: *Answer only / Answer and act / incl. admin actions*. It looks into each new alert and stores what it found as an insight for every user. Model evaluation and a curated download list in Settings help you pick a Tower model; a Thinking budget applies on llama.cpp, LM Studio and vLLM. **Settings → Tower assistant**.
- **Forecast.** Sixteen scheduled trend checks over stored history. Findings clear after two clean runs, can be dismissed, and can raise alerts from a chosen severity. Outlook (30-day predictions) and Briefing (by host or check) views, a strip on the Overall page, and a Tower effort setting that adds an explanation and a next step to each finding. **Dashboards → Forecast**, **Settings → Forecast**.
- **Jobs.** One scheduler for tool runs waiting behind a busy host, Report Card runs, Tower timers, overnight autotune batches, Forecast runs and Tower evaluations. Jobs keep running when you close the tab or restart the manager, show on every dashboard, and can be cancelled by their owner or an admin. **Admin → Jobs** lists them all; **System Health** shows the live and scheduled ones.
- **Model Autopilot.** Places each declared model on a host with the memory to hold it, moves it when a host goes offline, and adds or removes copies as demand changes. **Admin → Gateway**.
- **GPU Report Card.** One standardized benchmark across all three providers producing a comparable, shareable card — TTFT, throughput, tokens/joule, measured $/Mtok, and the GPU it ran on. Runs are stored so you can trend them.
- **Energy & cost intelligence.** Measured **$/Mtok** from real power draw, monthly savings against hosted-API pricing, idle-power accounting, and energy, cost and tokens per model (a *By model* table on the Energy tab). The global figure counts hosts that report both power and tokens.
- **Discord bot.** Slash commands for host and Tower queries, model load/unload, and alarm acknowledgement, limited to an allowlist of users.
- **OpenAI-compatible inference gateway.** One endpoint (`/api/gateway/v1`) fronts every provider; `/v1/models` merges all pools, deduped and tagged. Per-model pin, then pool round-robin, then pre-first-token failover. Dashboard sessions by default, API keys for external clients — a key can carry a `label=secret` form so it shows by name (not position) in the Gateway card's flow diagram. See [Inference gateway](DEPLOYMENT.md#inference-gateway).
- **Benchmarking & autotuning.** **Benchmark → Live** runs `speed-bench` against the running server through its API (workload presets, concurrency sweeps, energy per token, a results chart); runs attach to the Report Card, feed Autopilot's cross-host speed ranking, and can be pinned as baselines that are re-checked on a schedule with an alarm-engine regression alert. **Benchmark → Offline** runs `llama-bench` or `vllm bench serve` with a KV-cache type sweep, energy per run and a prompt × output heatmap. **Autotune** takes a goal (Fit / Speed / Balanced / Serve) and tunes seven settings in order — context, KV-cache type with a quality check, MoE CPU offload, CPU threads, speculative decoding with draft-model discovery on Hugging Face, parallel slots, author-recommended sampling — then verifies the result under traffic. Overnight batches, a Quiet power-cap goal, a stale-tune reminder and a standalone Quality guard round it out. Live benchmark and autotune run on `llama.cpp`, LM Studio and vLLM hosts, and any two runs from the ledger can be compared.
- **Model management.** A built-in Hugging Face browser downloads and prunes models file-by-file; named profiles (chat / code / general) swap and reload from the model card in one click.
- **Energy & thermal control.** A performance controller sets the CPU governor, GPU power limit and fan profile with inference load — full power under work, quiet when idle. It follows the model's state (loading, awake, sleeping, unloaded), waits out short changes before switching, and confirms each switch took effect.
- **Remote control, no SSH.** Run the servers, hot-swap models, edit configs, update `llama.cpp` (source, conda, Homebrew, release binaries, or your own script), tail logs, and open an in-browser PTY terminal.
- **LLM runtime visibility.** Slots, tokens/sec, prompt-processing rate, KV cache, context, idle/awake, chat template, and modalities, plus LM Studio loaded models and sessions.
- **Every host in one pane.** A picker switches views and controls per agent, and the **Overall** tab rolls combined throughput, hottest GPU, total power, and active models into one view.
- **Cross-platform agent.** One agent for Linux and macOS/Apple Silicon auto-detects what each host runs and enables only what's relevant — a bare host just reports system metrics, all over TLS.
- **Live host telemetry.** CPU, RAM, disk, network, GPU utilization, PSU, UPS battery, and AIO cooling stats.
- **Alerting.** A standalone alarm engine persists every metric to InfluxDB, evaluates threshold and anomaly rules, and routes alerts by email (with the mail server's certificate verified), toast, webhook, or Discord. Agents buffer to disk when it's down and replay when it returns.
- **Incident correlation.** Several rules triggering on one host at once become a single **incident** — one notification, with the Events table collapsing members behind a "+N related" count. Resolved alerts roll into a retention-managed history.
- **At-a-glance status.** Dots on the **Events** and **Admin** tabs turn red on active critical alerts or degraded system health, and amber when a new release is available. Both update from any tab. A separate run-activity dot on **LLM Control → Tools** tracks tool runs globally.
- **Phone companion (PWA).** An installable app at `/companion` — Home, Alerts, Tower (ask, prompts and insights), Energy, Models, Admin, and Settings screens sized for a phone, with alarm-engine alerts delivered as native push notifications even when the app is closed. Model swaps, pins, autopilot approvals, and service restarts each sit behind a confirm sheet, restricted to the admin role. See [Phone companion](DEPLOYMENT.md#phone-companion-pwa).
- **Direct LLM chat.** Talk to any loaded model through the embedded `llama.cpp` web interface.
- **OpenClaw cost analytics.** Session logs become token-usage, cost, and tool-attribution dashboards with monthly spend projection and — given a budget — warning, ceiling, and cost-anomaly alerts.
- **Image generation.** An optional tab drives `stable-diffusion.cpp` for text-to-image creation.
- **Multi-user access control.** **Admin** / **Operator** roles — operators can manage LLMs and watch dashboards, without access to the Admin tab, agent management, secrets, shells, or the Save buttons of service settings. There is no built-in password: the installer creates a temporary one for the first sign-in, and `admin_password.py reset` makes a new one at any time. Self-service password change plus username + source-IP lockout.
- **Admin audit log.** Every mutating admin action is recorded and browsable in **Admin → Audit Log**. Rows are purged past a configurable retention window (60 days by default) behind a 100,000-row backstop, categorized against an event catalog with per-event toggles, searchable and filterable, and exportable to CSV.
- **Scheduled backups.** Full export archives (config, agent registry, CA, users, model profiles, benchmarks) on an interval, with retention pruning, optional AES-256-GCM encryption, and an optional mirror directory. Each run writes a manager archive and, when `[alarm_engine].management_token` is set, an alarm-engine archive alongside it. Archives restore through **Restore…** and can be downloaded straight from the Backups card.
- **Encrypted everywhere.** All agent ↔ manager and agent ↔ alarm-engine traffic runs over TLS, with per-agent leaf certs signed by the manager's internal CA. Each certificate names what it belongs to (the manager, the alarm engine or one agent) and each side requires the role it expects. Every manager response carries baseline browser-hardening headers (`nosniff`, same-origin framing, strict referrer policy).
- **Controlled agent enrollment.** New agents need the enrollment secret and the manager's certificate fingerprint, both on **Admin → Agents → Manage ▾ → Enrollment** next to a ready-made install command, and are accepted only while enrollment is open (a 15-minute window after each manager start, or Open / Extend / Close by hand). Registration requests are size-limited, rate-limited per address and capped while waiting for approval, and the routes that need no login share a request budget that System Health reports.
- **System Health.** A live data-flow diagram of the manager, alarm engine, InfluxDB and every agent with status pills per service, warnings with advice, and, when all is quiet, three views of the last day: Recent operations from the audit log, Capacity bars for CPU, memory, disk and worker threads, and Trends for request volume and latency. The manager and alarm engine logs are readable from the same page.
- **Bring your own TLS certificate.** Point `[manager].tls_cert_file`/`tls_key_file` at a public or corporate-CA cert and the HTTPS port serves it via SNI for the hostnames it covers, while agents pinned to the internal CA keep working untouched. Required for installing the phone companion from another device.

---

## Screenshots

**Video tour** — sign-in, Overall, every dashboard including Forecast, Tower, LLM Control, the Tools launcher with a Live benchmark, chat, image generation, the alarm console, every Admin page including System Health, agent enrollment and Jobs, and the settings drawer:

https://github.com/user-attachments/assets/b2da9caf-9437-4001-acdc-cd12567fa786

<img width="1920" height="1080" alt="Sign-in screen" src="screenshots/login.webp" />

**[▶ Open the screenshot viewer](https://www.llmsyscore.com/#screenshots)** — step through all 30 screens full-size with the arrows.

Or open any screen right here:

<details>
<summary><b>Overall</b> — provider throughput, power and energy over the last 24 hours, the Forecast strip, per-provider rollups, every agent, and pinned cards from any dashboard</summary>

Provider throughput, power and energy over the last 24 hours, the Forecast strip, per-provider rollups, every agent, and pinned cards from any dashboard.

<img width="1920" height="1080" alt="Overall" src="screenshots/overall.webp" />
</details>

<details>
<summary><b>llama.cpp dashboard</b> — live server internals beside GPU, CPU, RAM, disk and network cards for the selected host</summary>

Live server internals beside GPU, CPU, RAM, disk and network cards for the selected host.

<img width="1920" height="1080" alt="llama.cpp dashboard" src="screenshots/dashboard-llama.webp" />
</details>

<details>
<summary><b>LM Studio dashboard</b> — loaded models, host metrics, and Apple-silicon powermetrics</summary>

Loaded models, host metrics, and Apple-silicon powermetrics.

<img width="1920" height="1080" alt="LM Studio dashboard" src="screenshots/dashboard-lmstudio.webp" />
</details>

<details>
<summary><b>Energy & cost</b> — measured $/Mtok, monthly savings against hosted-API pricing, per-host coverage, hourly energy, and the by-model breakdown</summary>

Measured $/Mtok, monthly savings against hosted-API pricing, per-host coverage, hourly energy, and the by-model breakdown.

<img width="1920" height="1080" alt="Energy &amp; cost" src="screenshots/dashboard-energy.webp" />
</details>

<details>
<summary><b>Forecast — Outlook</b> — a 30-day line of dated predictions from sixteen trend checks, with the finding list and a details drawer</summary>

A 30-day line of dated predictions from sixteen trend checks, with the finding list and a details drawer.

<img width="1920" height="1080" alt="Forecast — Outlook" src="screenshots/forecast-outlook.webp" />
</details>

<details>
<summary><b>Forecast — Briefing</b> — the same findings grouped by host or check, each with an Ask Tower button and a jump to the dashboard it came from</summary>

The same findings grouped by host or check, each with an Ask Tower button and a jump to the dashboard it came from.

<img width="1920" height="1080" alt="Forecast — Briefing" src="screenshots/forecast-briefing.webp" />
</details>

<details>
<summary><b>Tower assistant</b> — the drawer answering a question from live tool reads, with an approval prompt for a proposed action</summary>

The drawer answering a question from live tool reads, with an approval prompt for a proposed action.

<img width="1920" height="1080" alt="Tower assistant" src="screenshots/tower.webp" />
</details>

<details>
<summary><b>OpenClaw dashboard</b> — session metrics, task runs, delivery queue, and cost analytics for OpenClaw agents</summary>

Session metrics, task runs, delivery queue, and cost analytics for OpenClaw agents.

<img width="1920" height="1080" alt="OpenClaw dashboard" src="screenshots/dashboard-openclaw.webp" />
</details>

<details>
<summary><b>Manager dashboard</b> — service, database, stream and connection health of the manager itself</summary>

Service, database, stream and connection health of the manager itself.

<img width="1920" height="1080" alt="Manager dashboard" src="screenshots/dashboard-manager.webp" />
</details>

<details>
<summary><b>LLM Control — llama.cpp</b> — Model cards with per-model profiles, live-bench numbers and autotune state, server controls, and the live server log</summary>

Model cards with per-model profiles, live-bench numbers and autotune state, server controls, and the live server log.

<img width="1920" height="1080" alt="LLM Control — llama.cpp" src="screenshots/model-control.webp" />
</details>

<details>
<summary><b>LLM Control — list view</b> — the same library as a sortable list; Compact, List and Cards views are one click apart</summary>

The same library as a sortable list; Compact, List and Cards views are one click apart.

<img width="1920" height="1080" alt="LLM Control — list view" src="screenshots/model-control-list.webp" />
</details>

<details>
<summary><b>LLM Control — LM Studio</b> — load and unload LM Studio models, control the server, and tail its log</summary>

Load and unload LM Studio models, control the server, and tail its log.

<img width="1920" height="1080" alt="LLM Control — LM Studio" src="screenshots/model-control-lmstudio.webp" />
</details>

<details>
<summary><b>Tools launcher</b> — Report Card, Benchmark, Autotune and Quality guard as in-tab modules, with the per-host queue and a global run ledger underneath</summary>

Report Card, Benchmark, Autotune and Quality guard as in-tab modules, with the per-host queue and a global run ledger underneath.

<img width="1920" height="1080" alt="Tools launcher" src="screenshots/tools.webp" />
</details>

<details>
<summary><b>GPU Report Card</b> — one standard test per GPU — generation tok/s, prompt processing, first token, power draw, and cost per Mtok</summary>

One standard test per GPU — generation tok/s, prompt processing, first token, power draw, and cost per Mtok.

<img width="1920" height="1080" alt="GPU Report Card" src="screenshots/report-card.webp" />
</details>

<details>
<summary><b>Autotune</b> — pick a goal and tune seven settings in order, from context and KV-cache type to speculative decoding and slots, with a before/after recommendation table</summary>

Pick an objective and tune seven dimensions in order, from context and KV-cache type to speculative decoding and slots, with a before/after recommendation table.

<img width="1920" height="1080" alt="Autotune" src="screenshots/autotune.webp" />
</details>

<details>
<summary><b>Benchmark — Live</b> — speed-bench against the running server: workload presets, a concurrency sweep, energy per token, and a pinned baseline to re-check on a schedule</summary>

speed-bench against the running server: workload presets, a concurrency sweep, energy per token, and a pinned baseline to re-check on a schedule.

<img width="1920" height="1080" alt="Benchmark — Live" src="screenshots/benchmark-live.webp" />
</details>

<details>
<summary><b>Benchmark — Offline</b> — llama-bench and vllm bench serve across the model library, with the KV-cache sweep and the prompt × output heatmap</summary>

llama-bench and vllm bench serve across the model library, with the KV-cache sweep and the prompt × output heatmap.

<img width="1920" height="1080" alt="Benchmark — Offline" src="screenshots/benchmark.webp" />
</details>

<details>
<summary><b>LLM Chat</b> — chat against any model the gateway serves, with file upload and a working directory</summary>

Chat against any model the gateway serves, with file upload and a working directory.

<img width="1920" height="1080" alt="LLM Chat" src="screenshots/llm-chat.webp" />
</details>

<details>
<summary><b>Image generation</b> — native async image and video generation on a local stable-diffusion.cpp server</summary>

Native async image and video generation on a local stable-diffusion.cpp server.

<img width="1920" height="1080" alt="Image generation" src="screenshots/image-generation.webp" />
</details>

<details>
<summary><b>Alarm console</b> — active alerts, anomalies, suppressions, the 24-hour severity band, the event timeline and per-alert delivery history, inside the Events tab</summary>

Active alerts, anomalies, suppressions, the 24-hour severity band, the event timeline and per-alert delivery history, inside the Events tab.

<img width="1920" height="1080" alt="Alarm console" src="screenshots/alarm-console.webp" />
</details>

<details>
<summary><b>Admin — System Health & Agents</b> — the live data-flow diagram, the live-jobs strip and the service log viewer above the agent roster with capabilities, pool membership, TLS state and version</summary>

The live data-flow diagram, the live-jobs strip and the service log viewer above the agent roster with capabilities, pool membership, TLS state and version.

<img width="1920" height="1080" alt="Admin — System Health &amp; Agents" src="screenshots/admin-console.webp" />
</details>

<details>
<summary><b>Access Control</b> — login policy, trusted networks, users and roles</summary>

Login policy, trusted networks, users and roles.

<img width="1920" height="1080" alt="Access Control" src="screenshots/admin-access.webp" />
</details>

<details>
<summary><b>Audit Log</b> — a filtered ledger of every admin action with actor, target, source and result</summary>

A filtered ledger of every admin action with actor, target, source and result.

<img width="1920" height="1080" alt="Audit Log" src="screenshots/admin-audit.webp" />
</details>

<details>
<summary><b>Jobs</b> — every scheduled, queued, running and finished job — tool runs, Tower timers, overnight batches, Forecast runs — with filters and a detail panel</summary>

Every scheduled, queued, running and finished job — tool runs, Tower timers, overnight batches, Forecast runs — with filters and a detail panel.

<img width="1920" height="1080" alt="Jobs" src="screenshots/admin-jobs.webp" />
</details>

<details>
<summary><b>Backups</b> — Manager and alarm-engine archives, the backup schedule, retained runs, and the mirror state</summary>

Manager and alarm-engine archives, the backup schedule, retained runs, and the mirror state.

<img width="1920" height="1080" alt="Backups" src="screenshots/admin-backups.webp" />
</details>

<details>
<summary><b>Gateway</b> — the inference-gateway flow from clients to hosts, with throughput, latency, in-flight and energy tiles</summary>

The inference-gateway flow from clients to hosts, with throughput, latency, in-flight and energy tiles.

<img width="1920" height="1080" alt="Gateway" src="screenshots/gateway.webp" />
</details>

<details>
<summary><b>Model Autopilot</b> — placement entries, proposals, pool order and model pins</summary>

Placement entries, proposals, pool order and model pins.

<img width="1920" height="1080" alt="Model Autopilot" src="screenshots/autopilot.webp" />
</details>

<details>
<summary><b>Settings</b> — every configuration key in a category rail with nested groups, searchable, applied hot or flagged as restart-pending</summary>

Every configuration key in a category rail with nested groups, searchable, applied hot or flagged as restart-pending.

<img width="1920" height="1080" alt="Settings" src="screenshots/admin-settings.webp" />
</details>

<details>
<summary><b>Settings drawer</b> — pinned cards, layout engine, density, themes and refresh cadence for the page you are on</summary>

Pinned cards, layout engine, density, themes and refresh cadence for the page you are on.

<img width="1920" height="1080" alt="Settings drawer" src="screenshots/settings-drawer.webp" />
</details>
