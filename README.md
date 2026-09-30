# LLM Systems Manager

A complete, self-hosted operations platform for LLM infrastructure — monitoring, remote control, tuning, routing, alerting, and much more, all in one place.

It currently integrates [llama.cpp](https://github.com/ggerganov/llama.cpp), [vLLM](https://github.com/vllm-project/vllm), [LM Studio](https://lmstudio.ai/), [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp), and [OpenClaw](https://github.com/openclaw/openclaw) session telemetry, but the agent reports general host metrics for any Linux or macOS machine. New integrations with Ollama are on the roadmap.

## Install

The **script installer** is the preferred path — one interactive command handles prerequisites, InfluxDB, config, TLS, and agents. It enables the systemd units and asks before it starts a service:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
```

<details>
<summary><b>Native packages (.deb / .rpm)</b> — hosts standardized on apt or dnf</summary>

Packages ship with every [release](https://github.com/llmsyscore/llm-systems-manager/releases). They create the service user, start the systemd units, and prompt for the admin login.

```bash
# Debian / Ubuntu — resolves the newest .deb from the latest release
url=$(curl -fsSL https://api.github.com/repos/llmsyscore/llm-systems-manager/releases/latest \
        | grep -oE '"https://[^"]+_all\.deb"' | tr -d '"')
curl -fsSLO "$url" && sudo apt install ./"${url##*/}"

# RHEL / Fedora
url=$(curl -fsSL https://api.github.com/repos/llmsyscore/llm-systems-manager/releases/latest \
        | grep -oE '"https://[^"]+\.noarch\.rpm"' | tr -d '"')
curl -fsSLO "$url" && sudo dnf install ./"${url##*/}"
```
</details>

<details>
<summary><b>Docker Compose</b> — containerized control plane</summary>

Brings up the manager, alarm engine, and InfluxDB from multi-arch images on ghcr.io. Fill in `.env` first. Agents still install natively on each host, since they need sensor, GPU, and systemd access.

```bash
curl -fsSLO https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/.env.example -o .env
docker compose up -d
```
</details>

<details>
<summary><b>Homebrew</b> — macOS (Apple Silicon) and Linux</summary>

Installs the control plane from the project tap, onboards InfluxDB, and starts both services. `brew upgrade` tracks new releases automatically.

```bash
brew tap llmsyscore/tap && brew trust llmsyscore/tap
brew install llm-systems-manager llm-systems-alarm-engine influxdb@2 influxdb-cli
llm-systems-influx-setup
brew services start llm-systems-manager
brew services start llm-systems-alarm-engine
```
</details>

<details>
<summary><b>Agent binary tarball</b> — agent-only hosts without Python</summary>

A self-contained agent binary for Linux and macOS, for hosts where you want manual layout control. See [Agent installation](#agent-installation).
</details>

Full details for every method, including split installs, offline installs, and updates: [Installation options](#installation-options).

## Top features

**1. Tower assistant.** An AI assistant you can ask about the manager, your hosts, models, alerts, energy, tool runs and more, in plain language. Tower opens as a drawer on every page (**Alt+T**). It answers through a model the gateway is already serving, or through a dedicated Tower model you download from the Settings page. It proposes actions (load, unload, wake, acknowledge, restart) as approval prompts that are recorded in the audit log, looks into each new alert and writes up what it found, and runs checks on a timer. Enable it in **Settings → Tower assistant** ([screenshot](#screenshots)).

**2. Forecast.** Scheduled trend analysis that finds and reports problems early: disk fill, load shift, memory headroom, thermal trend, power and cost, throughput, model errors, slot pressure, model churn, alarm patterns, agent and service health, bench outcomes, capacity, idle waste, and a weekly digest. Findings show as a 30-day **Outlook** or a per-host **Briefing**, can raise alerts from a chosen severity, and each one offers *Ask Tower* for more help. **Dashboards → Forecast** ([screenshot](#screenshots)).

**3. Jobs.** One scheduler for everything that runs later: benchmark, autotune and Report Card runs wait their turn behind a busy host, and overnight batches, Tower timers and Forecast runs are queued the same way. Jobs keep running when you close the tab or restart the manager, show on every dashboard, and can be cancelled from **Admin → Jobs** ([screenshot](#screenshots)).

**4. Benchmarking and autotuning.** **Benchmark → Live** measures the running server through its own API with workload presets and concurrency sweeps; runs feed the Report Card and Autopilot's speed ranking, and pinned baselines are re-checked on a schedule with a regression alert. **Autotune** picks a goal (Fit / Speed / Balanced / Serve) and tunes seven settings in order — context, KV-cache type, MoE CPU offload, threads, speculative decoding, parallel slots, sampling defaults — then verifies the result under traffic. Overnight batches can tune a whole library. Works on `llama.cpp`, LM Studio and vLLM hosts ([screenshot](#screenshots)).

**5. Model Autopilot.** Configure which models should stay available; Autopilot places each one only on a host that has the memory to hold it (VRAM, or RAM on CPU-only hosts), brings it up elsewhere when a host drops out, and scales copies with demand. Includes an optional *Protect other models* setting that keeps it from displacing models it doesn't manage. **Admin → Gateway** ([screenshot](#screenshots)).

**6. Energy and cost intelligence.** What inference costs in **$/Mtok**, monthly savings against hosted-API pricing, idle power, and energy, cost and tokens broken down **per model**. A per-host performance controller switches the host between full power and quiet mode as models load, run and unload, and confirms each switch took effect ([screenshot](#screenshots)).

**7. OpenAI-compatible inference gateway.** One endpoint serves `llama.cpp`, LM Studio, and vLLM providers together. `/v1/models` returns the merged model catalog; requests can route by per-model pin, pool round-robin, or failover to a live host. Apps and agents target one URL. See [Inference gateway](#inference-gateway).

**8. GPU Report Card.** One standardized benchmark that shows time-to-first-token, prefill and generation throughput, tokens/joule, measured $/Mtok, and the GPU it ran on. The same preset runs on all three providers, so results compare across machines.

**9. Model management with profiles and cache control.** Pull models straight from Hugging Face and prune files to reclaim disk space. Every model keeps named profiles (chat / code / general) that reload it with those settings in one click.

**10. Remote control of the whole infrastructure.** Run the servers, hot-swap models, edit configs, update `llama.cpp`, tail logs, and open an in-browser terminal. A Discord bot exposes the same commands, one agent covers Linux and macOS/Apple Silicon, and the **Overall** tab rolls every host into a single pane.

**11. LLM-aware telemetry and alerting.** Live inference internals — slots, tokens/sec, prompt processing, KV cache, context — beside GPU, PSU, UPS, and cooling stats. A standalone alarm engine stores every sample, evaluates threshold and anomaly rules, notifies by email/toast/webhook/Discord, buffers through outages, and collapses related issues into one incident.

*Also included:* a **Tools** launcher that hosts Report Card, Benchmark, Autotune and Quality guard as one in-tab workspace with a system wide run ledger and comparison tool; a **layout/appearance system** (Grid or Flow engines, role presets, compact density, seven themes, per-tab pause), an installable phone companion (PWA) with push alerts, multi-user roles + admin audit log, encrypted scheduled backups covering the manager and the alarm engine, OpenClaw cost/budget analytics, an image generation tab, and TLS/mTLS on every connection — see the [full feature list](#full-included-features) below.

---

## Screenshots

**Video tour** — sign-in, Overall, every dashboard including Forecast, Tower, LLM Control, the Tools launcher with a Live benchmark, chat, image generation, the alarm console, every Admin page including Jobs, and the settings drawer:

<video src="https://github.com/user-attachments/assets/499a19e5-7224-4b9f-a67c-47df49a6a9a2" controls muted width="900"></video>

<img width="1920" height="1080" alt="Sign-in screen" src="docs/screenshots/login.webp" />

**[▶ Open the screenshot viewer](https://www.llmsyscore.com/#screenshots)** — step through all 30 screens full-size with the arrows.

Or open any screen right here:

<details>
<summary><b>Overall</b> — provider throughput, power and energy over the last 24 hours, the Forecast strip, per-provider rollups, every agent, and pinned cards from any dashboard</summary>

Provider throughput, power and energy over the last 24 hours, the Forecast strip, per-provider rollups, every agent, and pinned cards from any dashboard.

<img width="1920" height="1080" alt="Overall" src="docs/screenshots/overall.webp" />
</details>

<details>
<summary><b>llama.cpp dashboard</b> — live server internals beside GPU, CPU, RAM, disk and network cards for the selected host</summary>

Live server internals beside GPU, CPU, RAM, disk and network cards for the selected host.

<img width="1920" height="1080" alt="llama.cpp dashboard" src="docs/screenshots/dashboard-llama.webp" />
</details>

<details>
<summary><b>LM Studio dashboard</b> — loaded models, host metrics, and Apple-silicon powermetrics</summary>

Loaded models, host metrics, and Apple-silicon powermetrics.

<img width="1920" height="1080" alt="LM Studio dashboard" src="docs/screenshots/dashboard-lmstudio.webp" />
</details>

<details>
<summary><b>Energy & cost</b> — measured $/Mtok, monthly savings against hosted-API pricing, per-host coverage, hourly energy, and the by-model breakdown</summary>

Measured $/Mtok, monthly savings against hosted-API pricing, per-host coverage, hourly energy, and the by-model breakdown.

<img width="1920" height="1080" alt="Energy &amp; cost" src="docs/screenshots/dashboard-energy.webp" />
</details>

<details>
<summary><b>Forecast — Outlook</b> — a 30-day line of dated predictions from sixteen trend checks, with the finding list and a details drawer</summary>

A 30-day line of dated predictions from sixteen trend checks, with the finding list and a details drawer.

<img width="1920" height="1080" alt="Forecast — Outlook" src="docs/screenshots/forecast-outlook.webp" />
</details>

<details>
<summary><b>Forecast — Briefing</b> — the same findings grouped by host or check, each with an Ask Tower button and a jump to the dashboard it came from</summary>

The same findings grouped by host or check, each with an Ask Tower button and a jump to the dashboard it came from.

<img width="1920" height="1080" alt="Forecast — Briefing" src="docs/screenshots/forecast-briefing.webp" />
</details>

<details>
<summary><b>Tower assistant</b> — the drawer answering a question from live tool reads, with an approval prompt for a proposed action</summary>

The drawer answering a question from live tool reads, with an approval prompt for a proposed action.

<img width="1920" height="1080" alt="Tower assistant" src="docs/screenshots/tower.webp" />
</details>

<details>
<summary><b>OpenClaw dashboard</b> — session metrics, task runs, delivery queue, and cost analytics for OpenClaw agents</summary>

Session metrics, task runs, delivery queue, and cost analytics for OpenClaw agents.

<img width="1920" height="1080" alt="OpenClaw dashboard" src="docs/screenshots/dashboard-openclaw.webp" />
</details>

<details>
<summary><b>Manager dashboard</b> — service, database, stream and connection health of the manager itself</summary>

Service, database, stream and connection health of the manager itself.

<img width="1920" height="1080" alt="Manager dashboard" src="docs/screenshots/dashboard-manager.webp" />
</details>

<details>
<summary><b>LLM Control — llama.cpp</b> — Model cards with per-model profiles, live-bench numbers and autotune state, server controls, and the live server log</summary>

Model cards with per-model profiles, live-bench numbers and autotune state, server controls, and the live server log.

<img width="1920" height="1080" alt="LLM Control — llama.cpp" src="docs/screenshots/model-control.webp" />
</details>

<details>
<summary><b>LLM Control — list view</b> — the same library as a sortable list; Compact, List and Cards views are one click apart</summary>

The same library as a sortable list; Compact, List and Cards views are one click apart.

<img width="1920" height="1080" alt="LLM Control — list view" src="docs/screenshots/model-control-list.webp" />
</details>

<details>
<summary><b>LLM Control — LM Studio</b> — load and unload LM Studio models, control the server, and tail its log</summary>

Load and unload LM Studio models, control the server, and tail its log.

<img width="1920" height="1080" alt="LLM Control — LM Studio" src="docs/screenshots/model-control-lmstudio.webp" />
</details>

<details>
<summary><b>Tools launcher</b> — Report Card, Benchmark, Autotune and Quality guard as in-tab modules, with the per-host queue and a global run ledger underneath</summary>

Report Card, Benchmark, Autotune and Quality guard as in-tab modules, with the per-host queue and a global run ledger underneath.

<img width="1920" height="1080" alt="Tools launcher" src="docs/screenshots/tools.webp" />
</details>

<details>
<summary><b>GPU Report Card</b> — one standard test per GPU — generation tok/s, prompt processing, first token, power draw, and cost per Mtok</summary>

One standard test per GPU — generation tok/s, prompt processing, first token, power draw, and cost per Mtok.

<img width="1920" height="1080" alt="GPU Report Card" src="docs/screenshots/report-card.webp" />
</details>

<details>
<summary><b>Autotune</b> — pick a goal and tune seven settings in order, from context and KV-cache type to speculative decoding and slots, with a before/after recommendation table</summary>

Pick an objective and tune seven dimensions in order, from context and KV-cache type to speculative decoding and slots, with a before/after recommendation table.

<img width="1920" height="1080" alt="Autotune" src="docs/screenshots/autotune.webp" />
</details>

<details>
<summary><b>Benchmark — Live</b> — speed-bench against the running server: workload presets, a concurrency sweep, energy per token, and a pinned baseline to re-check on a schedule</summary>

speed-bench against the running server: workload presets, a concurrency sweep, energy per token, and a pinned baseline to re-check on a schedule.

<img width="1920" height="1080" alt="Benchmark — Live" src="docs/screenshots/benchmark-live.webp" />
</details>

<details>
<summary><b>Benchmark — Offline</b> — llama-bench and vllm bench serve across the model library, with the KV-cache sweep and the prompt × output heatmap</summary>

llama-bench and vllm bench serve across the model library, with the KV-cache sweep and the prompt × output heatmap.

<img width="1920" height="1080" alt="Benchmark — Offline" src="docs/screenshots/benchmark.webp" />
</details>

<details>
<summary><b>LLM Chat</b> — chat against any model the gateway serves, with file upload and a working directory</summary>

Chat against any model the gateway serves, with file upload and a working directory.

<img width="1920" height="1080" alt="LLM Chat" src="docs/screenshots/llm-chat.webp" />
</details>

<details>
<summary><b>Image generation</b> — native async image and video generation on a local stable-diffusion.cpp server</summary>

Native async image and video generation on a local stable-diffusion.cpp server.

<img width="1920" height="1080" alt="Image generation" src="docs/screenshots/image-generation.webp" />
</details>

<details>
<summary><b>Alarm console</b> — active alerts, anomalies, suppressions, the 24-hour severity band, the event timeline and per-alert delivery history, inside the Events tab</summary>

Active alerts, anomalies, suppressions, the 24-hour severity band, the event timeline and per-alert delivery history, inside the Events tab.

<img width="1920" height="1080" alt="Alarm console" src="docs/screenshots/alarm-console.webp" />
</details>

<details>
<summary><b>Admin — System Health & Agents</b> — the live data-flow diagram, the live-jobs strip and the service log viewer above the agent roster with capabilities, pool membership, TLS state and version</summary>

The live data-flow diagram, the live-jobs strip and the service log viewer above the agent roster with capabilities, pool membership, TLS state and version.

<img width="1920" height="1080" alt="Admin — System Health &amp; Agents" src="docs/screenshots/admin-console.webp" />
</details>

<details>
<summary><b>Access Control</b> — login policy, trusted networks, users and roles</summary>

Login policy, trusted networks, users and roles.

<img width="1920" height="1080" alt="Access Control" src="docs/screenshots/admin-access.webp" />
</details>

<details>
<summary><b>Audit Log</b> — a filtered ledger of every admin action with actor, target, source and result</summary>

A filtered ledger of every admin action with actor, target, source and result.

<img width="1920" height="1080" alt="Audit Log" src="docs/screenshots/admin-audit.webp" />
</details>

<details>
<summary><b>Jobs</b> — every scheduled, queued, running and finished job — tool runs, Tower timers, overnight batches, Forecast runs — with filters and a detail panel</summary>

Every scheduled, queued, running and finished job — tool runs, Tower timers, overnight batches, Forecast runs — with filters and a detail panel.

<img width="1920" height="1080" alt="Jobs" src="docs/screenshots/admin-jobs.webp" />
</details>

<details>
<summary><b>Backups</b> — Manager and alarm-engine archives, the backup schedule, retained runs, and the mirror state</summary>

Manager and alarm-engine archives, the backup schedule, retained runs, and the mirror state.

<img width="1920" height="1080" alt="Backups" src="docs/screenshots/admin-backups.webp" />
</details>

<details>
<summary><b>Gateway</b> — the inference-gateway flow from clients to hosts, with throughput, latency, in-flight and energy tiles</summary>

The inference-gateway flow from clients to hosts, with throughput, latency, in-flight and energy tiles.

<img width="1920" height="1080" alt="Gateway" src="docs/screenshots/gateway.webp" />
</details>

<details>
<summary><b>Model Autopilot</b> — placement entries, proposals, pool order and model pins</summary>

Placement entries, proposals, pool order and model pins.

<img width="1920" height="1080" alt="Model Autopilot" src="docs/screenshots/autopilot.webp" />
</details>

<details>
<summary><b>Settings</b> — every configuration key in a category rail with nested groups, searchable, applied hot or flagged as restart-pending</summary>

Every configuration key in a category rail with nested groups, searchable, applied hot or flagged as restart-pending.

<img width="1920" height="1080" alt="Settings" src="docs/screenshots/admin-settings.webp" />
</details>

<details>
<summary><b>Settings drawer</b> — pinned cards, layout engine, density, themes and refresh cadence for the page you are on</summary>

Pinned cards, layout engine, density, themes and refresh cadence for the page you are on.

<img width="1920" height="1080" alt="Settings drawer" src="docs/screenshots/settings-drawer.webp" />
</details>

---

## Full included features

The eleven headline capabilities plus everything else that ships in the box:

- **Tower assistant.** An AI assistant in a drawer on every page that answers questions about hosts, models, alerts, energy, jobs and runs through a model the gateway already serves. It reads host and hardware details, metric history, loaded models and profiles, alarms, energy, gateway traffic, recent runs, service health, logs, configuration and the audit log. It can load, unload or wake a model, acknowledge, close or resume an alert, start a benchmark, cancel a job or restart a provider, each behind an approval prompt that is written to the audit log as `tower via <user>`. Capability levels: *Answer only / Answer and act / incl. admin actions*. It looks into each new alert and stores what it found as an insight for every user. Model evaluation and a curated download list in Settings help you pick a Tower model; a Thinking budget applies on llama.cpp, LM Studio and vLLM. **Settings → Tower assistant**.
- **Forecast.** Sixteen scheduled trend checks over stored history. Findings clear after two clean runs, can be dismissed, and can raise alerts from a chosen severity. Outlook (30-day predictions) and Briefing (by host or check) views, a strip on the Overall page, and a Tower effort setting that adds an explanation and a next step to each finding. **Dashboards → Forecast**, **Settings → Forecast**.
- **Jobs.** One scheduler for tool runs waiting behind a busy host, Report Card runs, Tower timers, overnight autotune batches, Forecast runs and Tower evaluations. Jobs keep running when you close the tab or restart the manager, show on every dashboard, and can be cancelled by their owner or an admin. **Admin → Jobs** lists them all; **System Health** shows the live and scheduled ones.
- **Model Autopilot.** Places each declared model on a host with the memory to hold it, moves it when a host goes offline, and adds or removes copies as demand changes. **Admin → Gateway**.
- **GPU Report Card.** One standardized benchmark across all three providers producing a comparable, shareable card — TTFT, throughput, tokens/joule, measured $/Mtok, and the GPU it ran on. Runs are stored so you can trend them.
- **Energy & cost intelligence.** Measured **$/Mtok** from real power draw, monthly savings against hosted-API pricing, idle-power accounting, and energy, cost and tokens per model (a *By model* table on the Energy tab). The global figure counts hosts that report both power and tokens.
- **Discord bot.** Slash commands for host and Tower queries, model load/unload, and alarm acknowledgement, limited to an allowlist of users.
- **OpenAI-compatible inference gateway.** One endpoint (`/api/gateway/v1`) fronts every provider; `/v1/models` merges all pools, deduped and tagged. Per-model pin, then pool round-robin, then pre-first-token failover. Dashboard sessions by default, API keys for external clients — a key can carry a `label=secret` form so it shows by name (not position) in the Gateway card's flow diagram. See [Inference gateway](#inference-gateway).
- **Benchmarking & autotuning.** **Benchmark → Live** runs `speed-bench` against the running server through its API (workload presets, concurrency sweeps, energy per token, a results chart); runs attach to the Report Card, feed Autopilot's cross-host speed ranking, and can be pinned as baselines that are re-checked on a schedule with an alarm-engine regression alert. **Benchmark → Offline** runs `llama-bench` or `vllm bench serve` with a KV-cache type sweep, energy per run and a prompt × output heatmap. **Autotune** takes a goal (Fit / Speed / Balanced / Serve) and tunes seven settings in order — context, KV-cache type with a quality check, MoE CPU offload, CPU threads, speculative decoding with draft-model discovery on Hugging Face, parallel slots, author-recommended sampling — then verifies the result under traffic. Overnight batches, a Quiet power-cap goal, a stale-tune reminder and a standalone Quality guard round it out. Live benchmark and autotune run on `llama.cpp`, LM Studio and vLLM hosts, and any two runs from the ledger can be compared.
- **Model management.** A built-in Hugging Face browser downloads and prunes models file-by-file; named profiles (chat / code / general) swap and reload from the model card in one click.
- **Energy & thermal control.** A performance controller sets the CPU governor, GPU power limit and fan profile with inference load — full power under work, quiet when idle. It follows the model's state (loading, awake, sleeping, unloaded), waits out short changes before switching, and confirms each switch took effect.
- **Remote control, no SSH.** Run the servers, hot-swap models, edit configs, update `llama.cpp` (source, conda, Homebrew, release binaries, or your own script), tail logs, and open an in-browser PTY terminal.
- **LLM runtime visibility.** Slots, tokens/sec, prompt-processing rate, KV cache, context, idle/awake, chat template, and modalities, plus LM Studio loaded models and sessions.
- **Every host in one pane.** A picker switches views and controls per agent, and the **Overall** tab rolls combined throughput, hottest GPU, total power, and active models into one view.
- **Cross-platform agent.** One agent for Linux and macOS/Apple Silicon auto-detects what each host runs and enables only what's relevant — a bare host just reports system metrics, all over TLS.
- **Live host telemetry.** CPU, RAM, disk, network, GPU utilization, PSU, UPS battery, and AIO cooling stats.
- **Alerting.** A standalone alarm engine persists every metric to InfluxDB, evaluates threshold and anomaly rules, and routes alerts by email, toast, webhook, or Discord. Agents buffer to disk when it's down and replay when it returns.
- **Incident correlation.** Several rules triggering on one host at once become a single **incident** — one notification, with the Events table collapsing members behind a "+N related" count. Resolved alerts roll into a retention-managed history.
- **At-a-glance status.** Dots on the **Events** and **Admin** tabs turn red on active critical alerts or degraded system health, and amber when a new release is available. Both update from any tab. A separate run-activity dot on **LLM Control → Tools** tracks tool runs globally.
- **Phone companion (PWA).** An installable app at `/companion` — Home, Alerts, Tower (ask, prompts and insights), Energy, Models, Admin, and Settings screens sized for a phone, with alarm-engine alerts delivered as native push notifications even when the app is closed. Model swaps, pins, autopilot approvals, and service restarts each sit behind a confirm sheet, restricted to the admin role. See [Phone companion](#phone-companion-pwa).
- **Direct LLM chat.** Talk to any loaded model through the embedded `llama.cpp` web interface.
- **OpenClaw cost analytics.** Session logs become token-usage, cost, and tool-attribution dashboards with monthly spend projection and — given a budget — warning, ceiling, and cost-anomaly alerts.
- **Image generation.** An optional tab drives `stable-diffusion.cpp` for text-to-image creation.
- **Multi-user access control.** **Admin** / **Operator** roles — operators can manage LLMs and watch dashboards, without access to the Admin tab, agent management, secrets, and shells. Self-service password change plus username + source-IP lockout.
- **Admin audit log.** Every mutating admin action is recorded and browsable in **Admin → Audit Log**. Rows are purged past a configurable retention window (60 days by default) behind a 100,000-row backstop, categorized against an event catalog with per-event toggles, searchable and filterable, and exportable to CSV.
- **Scheduled backups.** Full export archives (config, agent registry, CA, users, model profiles, benchmarks) on an interval, with retention pruning, optional AES-256-GCM encryption, and an optional mirror directory. Each run writes a manager archive and, when `[alarm_engine].management_token` is set, an alarm-engine archive alongside it. Archives restore through **Restore…** and can be downloaded straight from the Backups card.
- **Encrypted everywhere.** All agent ↔ manager and agent ↔ alarm-engine traffic runs over TLS, with per-agent leaf certs signed by the manager's internal CA. Every manager response carries baseline browser-hardening headers (`nosniff`, same-origin framing, strict referrer policy).
- **Bring your own TLS certificate.** Point `[manager].tls_cert_file`/`tls_key_file` at a public or corporate-CA cert and the HTTPS port serves it via SNI for the hostnames it covers, while agents pinned to the internal CA keep working untouched. Required for installing the phone companion from another device.


## Donations

If you find this project useful, please consider leaving a donation

<!--START_SECTION:buy-me-a-coffee-->
<a href="https://www.buymeacoffee.com/llmsystems" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-blue.png" alt="Buy Me A Coffee" height="41" width="174"></a>
<!--END_SECTION:buy-me-a-coffee-->
---

## Installation options

The **fully automated script installer** (Quickstart below) is the preferred path — it handles prerequisites, InfluxDB, config, TLS, agents, and updates end-to-end. The alternatives cover specific scenarios:

| Method | Best for |
|---|---|
| **Script installer** (preferred) | Everything: full stack, split installs, agents, offline installs, updates — see [Quickstart](#quickstart--single-host) |
| [Native packages (`.deb`/`.rpm`)](#native-packages-deb--rpm) | Hosts standardized on apt/dnf package management |
| [Docker Compose](#docker-compose-control-plane-only) | Containerized control plane (manager + alarm engine + InfluxDB) |
| [Homebrew](#homebrew-control-plane) | brew-managed hosts (macOS Apple Silicon, Linux x86_64/arm64) — [agent](#homebrew-macos--linux) and control-plane formulas, auto-updating |
| [Agent binary tarball](#agent-binary-no-python-required) | Agent-only hosts without Python (Linux/macOS), manual layout control |

## Quickstart — single host

For a quick installation on one host, choose the full install option:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
```

The installer is interactive: it prompts for SMTP credentials (if you want email alerts), the manager admin login, and confirms before installing system packages. It then enables the systemd units but **does not start anything automatically** — it prints the exact `systemctl start` commands so you stay in control of timing.

The installer deploys the **latest [GitHub Release](https://github.com/llmsyscore/llm-systems-manager/releases)** — a source tarball whose SHA-256 checksum is verified before anything is installed; a mismatch aborts. To pin a specific version, or to track the development tip from a git clone of `main` instead (the advanced/bare-metal path — code that hasn't been cut into a release yet):

```bash
# pin a specific release
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh) --ref v1.0.0

# track unreleased main (advanced)
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh) --source git
```

The same `--ref` / `--source` flags apply to every install mode and to `--update`.

### Offline / air-gapped install

Hosts with no access to GitHub can install from a release tarball staged out-of-band. On a connected machine, download `llm-systems-manager-<tag>.tar.gz` from the [Releases page](https://github.com/llmsyscore/llm-systems-manager/releases) (verify it against the published `.sha256` yourself — the offline path trusts the tree you hand it). Copy it to the target host, then:

```bash
tar -xzf llm-systems-manager-v1.0.0.tar.gz
sudo bash llm-systems-manager-v1.0.0/tools/installer/install.sh --source local
```

`--source local` installs the extracted tree the script lives in: no release download, no git clone, no installer self-update (`git` itself is not required on the target host). It works with every install mode and with `--update` (offline update of an existing install). Note the scope: only GitHub access is eliminated — installing system packages and the Python virtualenvs still uses `apt` and `pip`, so a fully air-gapped host needs local mirrors for those (or pre-provisioned dependencies).

After install:

1. Start the services if they were not started at installation, the commands to start them will be shown by the installer.
2. Open `http://<this-host>:5000/` in a browser. Log in with the admin credentials you set.
3. From the **Admin** tab, approve any agents that have registered. Approval issues each agent a per-host TLS certificate and unlocks remote control.

That's it for a single-host lab. Everything else below is for adding more hosts or pointing the dashboard at inference servers you already run.

### Docker Compose (control plane only)

Prefer containers? No repo checkout needed — `curl` down `docker-compose.yml` + `.env.example`, fill in the secrets, and `docker compose up -d` brings up the manager + alarm engine + InfluxDB from multi-arch images published to ghcr.io on every release — see [docker/README.md](docker/README.md). Agents still install natively on each host (they need sensor/GPU/systemd access).

### Native packages (.deb / .rpm)

Every [release](https://github.com/llmsyscore/llm-systems-manager/releases) also ships native packages for Debian/Ubuntu and RHEL-family distros: `llm-systems-manager` (manager + alarm engine; InfluxDB stays external — declared as a Recommends, with a pointer printed if it's unreachable) and per-arch `llm-systems-agent` packages built around the self-contained binary:

```bash
sudo apt install ./llm-systems-manager_<version>_all.deb        # debconf prompts for admin login + SMTP
sudo dnf install ./llm-systems-manager-<version>-1.noarch.rpm   # EL9 needs python3.11 first; defaults, then edit config
sudo apt install ./llm-systems-agent_<version>_amd64.deb        # agent; prompts for the manager URL
```

Packages create the `llmsys` user, install + start the systemd units, and build the Python venvs at install time (network to PyPI required; the agent package needs none — it's a single binary). Config survives upgrades; `apt purge` removes everything the package created (state from another install method is kept). Install methods don't mix — packages and the script installer refuse to overwrite each other. Details, RPM variants, and uninstall behavior: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#installing-from-native-packages-deb--rpm).

### Homebrew (control plane)

The manager and alarm engine also install from the project's [Homebrew tap](https://github.com/llmsyscore/homebrew-tap) — macOS (Apple Silicon) or Linux:

```bash
brew tap llmsyscore/tap
brew trust llmsyscore/tap        # newer Homebrew requires trusting third-party taps
brew install llm-systems-manager llm-systems-alarm-engine influxdb@2 influxdb-cli
```

Each formula builds its own Python venv from the release source tarball. Shared config is seeded at `$(brew --prefix)/etc/llm-systems-manager/llm-systems.toml` (alarm-engine ingest/management tokens pre-generated); state lives under `$(brew --prefix)/var/llm-systems-manager/` and survives upgrades. Bring the stack up in this order — the manager's first boot creates the internal CA and issues the alarm engine's TLS cert:

```bash
llm-systems-influx-setup        # onboards InfluxDB, creates the buckets + scoped
                                # tokens, and writes [influxdb.tokens] into the config
brew services start llm-systems-manager
brew services start llm-systems-alarm-engine
```

`llm-systems-influx-setup` (installed by the manager formula) needs both `influxdb@2` (the v2 server — Homebrew's plain `influxdb` formula is InfluxDB 3.x, whose API this stack does not speak) and `influxdb-cli` (the `influx` command ships separately). To do it by hand instead: `brew services start influxdb@2`, `influx setup`, create the buckets/tokens, and fill `[influxdb.tokens]` in the TOML.

`brew upgrade` tracks new releases automatically (the same tap cron that bumps the agent formula bumps these). The dashboard is at `http://<host>:5000`; the alarm engine can run without InfluxDB, but history and alert evaluation stay degraded until the tokens are filled in.

---

## Agent installation

The agent is what pushes all data into the dashboard. Run the installer and use the mode 5 (agent installation) option on every machine you want to monitor and control (Linux or macOS):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
```

The agent registers itself with the manager on first launch. From **Admin → Agents**, click **Approve** — the manager signs a TLS cert for that agent and starts polling it. Global actions (Approve all pending, Update all, Push CA, the agent-auth slider) live under the **Manage ▾** menu on that same tab.

### Homebrew (macOS / Linux)

On macOS (Apple Silicon) or a Linux host with [Homebrew](https://docs.brew.sh/Homebrew-on-Linux) (x86_64 or arm64), install the agent from the project's Homebrew tap:

```bash
brew tap llmsyscore/tap
brew trust llmsyscore/tap        # newer Homebrew requires trusting third-party taps
brew install llm-systems-agent
```

The formula picks the right prebuilt binary for the platform. Set `MANAGER_URL` in `$(brew --prefix)/etc/llm-systems-agent/agent_config.yaml` (the fully documented `agent_config.yaml.example` is installed alongside it for reference), then run the agent as a service (launchd on macOS, a systemd user unit on Linux):

```bash
brew services start llm-systems-agent
```

`brew upgrade llm-systems-agent` picks up new releases automatically — a scheduled job in the tap tracks each GitHub Release and bumps the formula. The dashboard's **Admin → Agents → Update** self-update also works, but a later `brew upgrade` replaces the binary again, so prefer `brew` on Homebrew-managed hosts. Uninstall with `brew services stop llm-systems-agent && brew uninstall llm-systems-agent`.

### Agent binary (no Python required)

Every [release](https://github.com/llmsyscore/llm-systems-manager/releases) also ships the agent as a per-platform tarball (`llm-systems-agent-linux-x86_64.tar.gz`, `-linux-arm64.tar.gz`, `-macos-arm64.tar.gz`) with a `.sha256` checksum — no Python or venv needed on the host. Each tarball bundles the self-contained binary, a fully documented `agent_config.yaml.example`, and the platform's service-manager unit (`llm-systems-agent-binary.service.tmpl` on Linux, `com.llm-systems-agent-binary.plist.tmpl` on macOS), so one download + extract gives you a ready-to-edit install. On Linux:

```bash
sudo mkdir -p /opt/llm-systems-agent && cd /opt/llm-systems-agent
sudo curl -fsSLO https://github.com/llmsyscore/llm-systems-manager/releases/latest/download/llm-systems-agent-linux-x86_64.tar.gz
sudo curl -fsSLO https://github.com/llmsyscore/llm-systems-manager/releases/latest/download/llm-systems-agent-linux-x86_64.tar.gz.sha256
sha256sum -c llm-systems-agent-linux-x86_64.tar.gz.sha256   # macOS: shasum -a 256 -c <file>.sha256
sudo tar -xzf llm-systems-agent-linux-x86_64.tar.gz         # -> binary + agent_config.yaml.example + .service.tmpl
sudo chmod +x llm-systems-agent
sudo cp agent_config.yaml.example agent_config.yaml         # then edit: at minimum set MANAGER_URL
sudo chown -R <run-as-user>: /opt/llm-systems-agent
```

Then install the systemd unit from the extracted `llm-systems-agent-binary.service.tmpl` (substitute `${AGENT_USER}`, `${AGENT_GROUP}`, `${AGENT_INSTALL_DIR}`) into `/etc/systemd/system/llm-systems-agent.service` and `systemctl enable --now llm-systems-agent`. 

Provider flags (`LLAMA_ENABLED`, `LMS_ENABLED`, sudo wrappers for service control, udev rules for liquidctl) are what the full installer automates — every option is documented inline in `agent_config.yaml.example`, so set them in your copied `agent_config.yaml` as needed. 

On macOS, download the `-macos-arm64.tar.gz` tarball instead; it bundles the same binary + `agent_config.yaml.example` plus the `com.llm-systems-agent-binary.plist.tmpl` launchd unit. Clear the quarantine attribute first (`xattr -d com.apple.quarantine llm-systems-agent`), then use the extracted `com.llm-systems-agent-binary.plist.tmpl` (substitute `${AGENT_USER}`, `${AGENT_USER_HOME}`, `${AGENT_INSTALL_DIR}`) as the launchd unit. Linux binaries need glibc 2.35+ (Ubuntu 22.04 / Debian 12 or newer).

Agents can also be upgraded from **Admin → Agents**
Per-agent **Update**: the agent downloads the latest release tarball for
its platform, verifies the `.sha256`, extracts and smoke-tests the staged
binary, swaps it atomically (previous binary kept beside it as
`.self-update.bak.<ts>`), and restarts. 
**Update all**, upgrading all agents in one click, now
lives in the **Manage ▾** menu with a pending-count badge: agents
run one at a time, each has to report the new version before the next starts,
and the sequence stops at the first failure with the remainder left untouched.

Approve a second agent that runs the same provider (e.g. a second `llama.cpp` box) and a host picker automatically appears on the matching dashboard sub-tabs — every approved agent is independently viewable and controllable. One agent is the *default* (what the dashboard shows when you haven't picked); set it from **Admin**.

## Multiple Hosts

Typical lab topology:

```
                ┌─────────────────────┐
                │  Manager + Alarm    │  
                │  Engine + InfluxDB  │  
                │  + local agent      │
                └──────────┬──────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   ┌────▼─────┐      ┌─────▼────┐       ┌─────▼────┐
   │  GPU     │      │  Mac     │       │  Other   │
   │  host    │      │  Studio  │       │  hosts…  │
   │  agent   │      │  agent   │       │  agent   │
   │  +llama  │      │  + LMS   │       │          │
   └──────────┘      └──────────┘       └──────────┘
```

When you want the **InfluxDB on its own host**, use mode 6 (InfluxDB only) option there first, then choose mode 2 (Manager + alarm) on the manager/alarm-engine host. The installer will prompt for the InfluxDB URL and the Influxdb tokens that were printed during the InfluxDB installation.

When you want the **manager and alarm engine on separate hosts**, use mode 3 (manager only) on the manager hose and mode 4 (alarm engine) on the alarm engine host. 

The installer will prompt for the cross-host URLs and then gives you the exact commands required to copy the alarm engine's TLS certs from the manager host to the alarm engine host.

The alarm engine's `management_token` is **required** on a split install. It locks the engine's rules/alerts/notifications API (which is otherwise open to anyone on the network) and lets Admin → Settings edit alarm-engine settings. Mode 4 prints a generated value; the mode 3 prompt accepts that paste or `new` to generate one on the manager host, and cannot be skipped. Non-interactive mode 3 installs can pass it as `LLMSYS_CFG_AE_MGMT_TOKEN`; otherwise one is generated and printed at the end. The same value must then be live on **both** hosts — until it is, the engine logs `ALARM ENGINE AUTH` at startup and Admin → System Health shows an `auth open` chip on the alarm-engine row.

### Choosing the run-as user

By default the manager and alarm engine run as a dedicated `llmsys` system account (auto-created, password-locked). Passing `--user <name>` to the installer allows you to use a different account, you can also enter the account name during the installation as well:

If the account exists, its real primary group is preserved; if it doesn't, the installer creates it as a system user. The agent installer also accepts the same `--user` flag.

---

## Pointing the agent at your own services

The agent ships with sensible defaults and attempts to automatically configure itself. If your inference servers run on different ports, hosts, or paths, you can override them in the `agent/agent_config.yaml` file on each agent host (the installer drops a template alongside the agent). 

Common keys:

| Key | What it points at | Default |
|---|---|---|
| `LLAMA_API_URL` | Your `llama-server` HTTP endpoint (llama.cpp has announced the default port moves to `:9931`) | `http://localhost:8080` |
| `LMS_API_URL` | Your LM Studio API endpoint | `http://localhost:1235` |
| `LLAMA_BIN` | Path to the `llama-server` binary (only needed for the agent's auto-restart / config-edit flows) | auto-detected |
| `LLAMA_CONFIG_INI` | Path to `config.ini` driving `llama-server` | auto-detected |
| `LLAMA_LOG_FILE` | Path to `llama-server.log` (for log-tail + state detection) | auto-detected |
| `LLAMA_BUILD_METHOD` | How the "Update llama.cpp" button installs/upgrades: `custom_script` / `source` / `release_binary` / `conda` / `homebrew` | auto-detected at install |
| `LMS_CMD` | Path to the `lms` CLI | auto-detected (`which lms`) |
| `LMS_LOAD_TIMEOUT_S` | How long to wait for an LM Studio model load before reporting failure (keep under 200) | `180` |
| `LMS_UNLOAD_TIMEOUT_S` | How long to wait for an LM Studio model unload before reporting failure (keep under 90) | `60` |
| `PROCESS_WATCHLIST` | Process names the agent should report on (psutil-style) | sensible defaults — see the example |

The installer fills most of these in at deploy time via auto-detect and prompts; the file above lists what to override after installation. Any field can also be set via environment variable `LSA_<NAME>` (e.g. `LSA_LLAMA_API_URL=http://...`).

Enable only what's relevant — the agent installer offers `--enable-llama`, `--enable-lms`, `--enable-vllm`, and `--enable-perf` flags, and auto-detects most of these from what's installed on the host. 

A host with neither `llama-server` nor LM Studio just reports generic system metrics.

---

## Inference gateway

One OpenAI-compatible endpoint (http://<manager-host>:5000/api/gateway/v1) on the manager serves every approved agent across all three providers — `llama.cpp`, LM Studio, and vLLM. Instead of targeting one backend by host:port, your apps call the manager and it picks a healthy one for each request:

- `POST /api/gateway/v1/chat/completions`
- `POST /api/gateway/v1/completions`
- `GET  /api/gateway/v1/models`

`GET /v1/models` returns the merged catalog from every pool, each entry tagged with its `provider` and deduplicated by id, and the owning provider is resolved per request from the model you ask for. Provider-scoped requests (`/api/gateway/llama/v1/*`, `/api/gateway/lms/v1/*`, `/api/gateway/vllm/v1/*`) are available when you want to force one.

Routing follows the same precedence as the dashboard: a per-model **pin** first, then an explicit `?agent=` pick, then **pool round-robin**, finally the system **default**. If the chosen backend can't be reached, the gateway **fails over** to the next live agent that actually serves that model. Both streaming (`"stream": true`) and non-streaming requests work, and each response carries an `X-Proxied-To` header naming the agent that served it.

**Access.** By default the gateway is reachable from a logged-in dashboard session only. To allow external clients, add one or more keys to the Gateway API Keys setting or the `[manager.gateway].api_keys` key in the TOML and restart the manager — each key is a bearer accepted only on `/api/gateway/*`. A key can optionally be labelled (`name=secret`); a labelled key names the client in the Gateway card's flow diagram, and an unlabelled one shows there as `key-1`, `key-2`, … by position:

```
api_keys = ["laptop=sk-abc123", "sk-plainkey"]   # empty = dashboard-session access only
```

**Call it like any OpenAI endpoint:**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<manager-host>:5000/api/gateway/v1",
    api_key="sk-your-secret-key",       # any configured key
)
resp = client.chat.completions.create(
    model="<model-id>",                 # from GET /v1/models; drives pin routing
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

or with curl:

```bash
curl http://<manager-host>:5000/api/gateway/v1/chat/completions \
  -H "Authorization: Bearer sk-your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Hello!"}]}'
```

The gateway forwards over the existing agent TLS channel; admin and control endpoints are not part of it.

---

## Phone companion (PWA)

`/companion` serves an installable phone app built from the same manager. Seven screens: **Home** (global at-a-glance with live graphs), **Alerts**, **Tower** (ask, cards and insights), **Energy**, **Models**, **Admin**, and **Settings**. Alarm-engine alerts arrive as native push notifications even when the app is closed. Control actions — swap or pin a model, approve autopilot proposals, restart a service or agent — ask for confirmation and need the admin role.

To install it on a phone:

1. **Serve trusted HTTPS.** Browsers only install a PWA (and only deliver web push) from a certificate the device already trusts. Set `[manager].tls_cert_file` / `tls_key_file` to a PEM full-chain + key for your domain — a Let's Encrypt or corporate-CA cert both work. The cert is selected by SNI for the hostnames it covers; agents dialing by IP or internal names still get the internal-CA cert, so nothing else changes. Set `[manager].ws_proxy_tls_port` (default `5446`) so the alerts screen's WebSocket isn't mixed-content-blocked on the HTTPS page.
2. **Open `https://<your-domain>:5443/companion`** on the phone, sign in, and use the browser's *Add to Home Screen* / *Install* prompt.
3. **Enable push** from the Settings screen (set `[manager.companion].push_contact` to a reachable operator address first). The **Send test notification** button confirms end-to-end delivery.

An opt-in release check (`[manager.companion].release_check`, also toggleable from Settings) surfaces a newer manager release on the Admin screen — it is the manager's only outbound call to github.com and defaults to off.

---

## Architecture

```
                              ┌────────────────────────┐
                              │       Browser          │
                              │  (single-page dash)    │
                              └───────────┬────────────┘
                                          │ HTTP / SSE / WebSocket
                                          ▼
                              ┌────────────────────────┐
                              │   Manager (Flask)      │
                              │  • UI + REST API       │
                              │  • Reverse proxies     │
                              │  • Agent registry      │
                              │  • Internal CA (mTLS)  │
                              └─┬──────────────────┬───┘
                  proxies       │                  │  forwards control
                                │                  │
                 ┌──────────────▼────────┐    ┌────▼────────────────┐
                 │   Alarm Engine        │    │  Agents (FastAPI)   │
                 │   (FastAPI)           │    │  TLS, bearer auth   │
                 │  • Ingests metrics    │◀───┤  • Host telemetry   │
                 │  • Rule evaluation    │    │  • llama / LMS ctrl │
                 │  • Notifications      │    │  • PTY + log tail   │
                 │  • WebSocket → UI     │    │  • Disk buffer      │
                 └──────────────┬────────┘    └─────────────────────┘
                                │
                       ┌────────▼─────────────┐
                       │   InfluxDB v2        │
                       │  metrics time-series │
                       │  (raw + rollups)     │
                       ├──────────────────────┤
                       │   SQLite (WAL)       │
                       │  alerts · rules ·    │
                       │  channels · history  │
                       └──────────────────────┘
```

### The three services

| Service | Role | Where it runs |
|---|---|---|
| **Manager** | Web UI, REST API, reverse proxies for sub-services, agent approval, internal certificate authority, layout/state persistence. | One Linux host. |
| **Alarm Engine** | Ingests every metric sample, persists to InfluxDB, evaluates rules, fires/acks/resolves alerts, dispatches notifications, streams events to the UI over WebSocket. | Same host as the manager, or its own server. |
| **Agent** | Lives on every monitored host. Polls the kernel, sensors, GPU, llama.cpp, LM Studio. Buffers samples to disk if the network is down. Exposes a TLS-only API for remote control. | Every host you want to monitor. |

### How a metric travels

1. The agent samples the host every few seconds, builds a flat JSON sample, and pushes it via a buffered client to the alarm engine.
2. The alarm engine writes the sample into InfluxDB, evaluates active rules, and — if a threshold trips — fires an alert through the notification dispatcher.
3. The browser keeps a WebSocket open to the alarm engine for alert state, and polls the manager for live metrics. The frontend dashboard renders both.

### Storage

InfluxDB v2 is the database for the **time-series metrics** — raw samples plus a one-minute rollup for long-range history. Everything transactional lives in **SQLite** (WAL mode, owned by the alarm engine): alerts and alert history in one database, alarm rules / notification channels / notification policies / delivery history in another. Three small SQLite files sit beside the manager: `manager.db` (benchmark results, Report Card and tool-run ledgers, Tower threads, jobs, model metadata), `audit.db` (the admin audit log) and `energy.db` (hourly energy and token accounting, per host and per model). UI state (card order, theme) lives in a JSON file beside the manager.

### Security model

- **Login and roles.** Named users with two roles: **Admin** (everything) and **Operator** (run models and watch dashboards, no Admin tab, agents, secrets or shells). Admins manage accounts in **Admin → Access Control**; every user can change their own password. Passwords are stored as hashes, repeated failed logins lock the username and source IP for a while, and an account still on the shipped default password must change it before it can continue. Login mode can be `required`, `trusted_cidr`, `disabled` or `auto`.
- **Agents.** Each agent gets a bearer token at registration and a TLS certificate signed by the manager's internal CA on approval, and presents a hardware fingerprint when it checks in.
- **TLS.** The manager serves HTTPS on `[manager].tls_port` (default `5443`) with a certificate from the internal CA, and agents move to it once approved. You can point `[manager].tls_cert_file` / `tls_key_file` at your own certificate; it is served for the hostnames it covers while agents keep using the internal CA. HTTPS sessions use their own `__Secure-session` cookie, HSTS is available with `[manager].hsts_max_age_s`, and every response carries the standard browser-hardening headers.
- **Alarm engine.** Agents send metrics to the alarm engine with a shared token (`[alarm_engine].ingest_token`) over HTTPS (`[alarm_engine].tls_enabled`). The dashboard reaches its WebSocket through a manager-side bridge (`[manager].ws_proxy_port`, `ws_proxy_tls_port`) that requires a short-lived ticket for each connection.
- **Gateway keys.** The OpenAI-compatible gateway accepts dashboard sessions, or bearer keys from `[manager.gateway].api_keys` for outside clients, and reuses the agent TLS channel to reach backends.
- **Secrets** (InfluxDB tokens, SMTP password) live in one config file with restrictive permissions. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full detail.

### Frontend

The frontend polls the manager every few seconds when something is active and slows down when the lab is idle, also opens event streams for downloads, builds, log tails, and the in-browser terminal.

---

## Configuration

Configuration options are accesible via the settings section of the Admin tab or the runtime config file: `config/llm-systems.toml`. Both the manager and the alarm engine read from the same file. A documented template ships as `config/llm-systems.toml.example` — the installer renders the live file from the template and prompts you for the values that have to be host-specific (IPs, SMTP credentials, InfluxDB tokens).

Edit the config, then restart the affected service:

```bash
sudo systemctl restart llm-systems-manager
# or
sudo systemctl restart llm-systems-alarm-engine
```

Per-agent settings live in `agent/agent_config.yaml` on each agent host and can also be edited via the agents tab.

---

## Updating

Re-running the installer is safe: existing configs are backed up with a timestamp before any rewrite, and existing virtual environments are reused. 

For an in-place update of an installed host:

```bash
# Detect, diff, back up, sync only what changed, restart affected services
sudo bash /opt/llm-systems-manager/tools/installer/install.sh --update
```

Or pick **mode 7 (Update)** from the interactive menu. Update preserves the run-as user that was already in place — you don't need to re-pass `--user`.

---

## Supported platforms

The manager, alarm engine, and InfluxDB are tested on **Debian and Ubuntu derivatives**.

- **Other Linux distros** (Fedora, Arch, openSUSE, Alpine): the agent (mode 5) auto-detects `dnf` / `yum` / `brew` and works out of the box. The manager / alarm engine / InfluxDB modes (1–4, 6) will halt at the pre-requisites step with a hint for your package manager — install the listed packages by hand, then re-run.
- **macOS** (Apple Silicon, tested on M2 Pro): agent only.

The installer checks for: `python3` (≥ 3.11), `python3-venv`, `git`, `jq`, `curl`, and `rsync`.

---

## Troubleshooting and Uninstall

| Symptom | Where to look |
|---|---|
| Dashboard won't load / 502 in the browser | `sudo systemctl status llm-systems-manager` then `sudo journalctl -u llm-systems-manager -n 100 --no-pager`. |
| Host doesn't appear in the dashboard | Agent installed but not approved: **Admin → Agents → Approve**. Approved but no data: check the agent log with `sudo journalctl -u llm-systems-agent -f` on that host. |
| Agent shows up but metrics are flat | The agent is probably not reaching the alarm engine. On the agent host: `curl -i http://<manager-host>:8081/health` (or `https://...` if AE TLS is on). 401 means the agent doesn't have the ingest token yet — wait one heartbeat (≤60 s) or restart it. |
| Alarm engine red dot in the Admin tab | Open `http://<manager-host>:5000/api/admin/system-health` to see which component is degraded. Common causes: AE TLS cert missing on a split multi server install (copy `ae-tls.{crt,key}` from manager → AE host ../data directory), ingest token mismatch (both hosts must carry the same value), InfluxDB down. |
| Need to start over | `bash /opt/llm-systems-manager/tools/installer/install.sh --uninstall` walks through removing services, the install tree, the runtime user, and (with confirmation) InfluxDB itself. |

---

## Project layout

```
llm-systems-manager/        Flask manager — backend/ (auth, multi-user management, agent registry, terminal, reverse proxies, OpenClaw analytics, tool run tracking, shared app context, internal CA, archive) and frontend/ (single-page UI)
agent/                      Cross-platform telemetry + control agent (+ install/)
llm-systems-alarm-engine/   Standalone alarm engine (FastAPI)
config/                     Unified TOML config + typed loader
tools/                      Universal installer (tools/installer/), smoke tests, benchmark harness
docs/                       Architecture notes, prereqs, screenshots
```

---
## Contributing / Donations

Issues and pull requests are welcome.

If you find this project useful, please consider leaving a donation

<!--START_SECTION:buy-me-a-coffee-->
<a href="https://www.buymeacoffee.com/llmsystems" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
<!--END_SECTION:buy-me-a-coffee-->

---

## License

[GNU Affero General Public License v3.0](LICENSE) — full text in the `LICENSE` file at the repo root.
