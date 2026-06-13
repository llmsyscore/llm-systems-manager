# LLM Systems Manager — Components

This document describes each major component of LLM Systems Manager: what it does, what it is responsible for, and how it fits into the overall system.

The system is made up of seven main pieces: the **Manager** (the central hub and web interface), **Agents** (lightweight processes running on each monitored computer), the **Alarm Engine** (metrics storage and alerting), the **Web Dashboard** (the browser UI), an **Internal Certificate Authority** (handles encrypted communication), and a **Unified Configuration** file shared by the Manager and Alarm Engine. An **Installer** script handles deploying these components onto new hardware.

Each component is designed to degrade gracefully: if the Alarm Engine is temporarily down, agents buffer data and resume; if an agent goes offline, the Manager marks it as offline and the dashboard reflects that; if a certificate is near expiry, the Admin tab warns in advance. No single component failure takes down the whole system.

---

## Manager

### What It Does

The Manager is the central hub of the system. It serves the web dashboard that operators use to monitor and control the AI lab, keeps track of every monitored computer (called an agent), and acts as the secure gateway between the browser and the rest of the fleet. All user interactions — viewing metrics, starting an AI model, changing settings — flow through the Manager.

### Key Responsibilities

- Serves the web dashboard UI to browsers over HTTP and HTTPS
- Authenticates users with username and password (two roles: Admin and Operator)
- Maintains a registry of all agents: which computers are online, approved, and what capabilities they have
- Forwards dashboard API requests to the appropriate agent or alarm engine
- Runs an internal Certificate Authority that issues security certificates to every component in the fleet
- Collects hardware metrics from its own host machine and sends them to the alarm engine
- Stores per-model AI configuration profiles so operators can switch between presets
- Manages user accounts, including password changes and role assignments

### Module Breakdown

| Module | What It Does |
|---|---|
| Main application | Flask web server startup, route wiring, background threads, TLS server setup |
| auth | Handles login, logout, session management, and per-request access control; enforces Admin vs. Operator role limits |
| agent_registry | Tracks every registered agent: approval status, capabilities, last heartbeat, TLS certificate issuance |
| proxies | Routes browser API calls to the correct agent or alarm engine; handles the agent picker (pin, round-robin, default) |
| terminal | Provides interactive shell sessions to the manager host and remote machines, streamed to the browser |
| openclaw | Reads session files and produces usage and cost analytics for the OpenClaw tab |
| provider_state | Holds the most recent hardware and AI metrics pushed by each agent; serves as the fast in-memory state store |
| model_profiles | Stores named configuration presets per AI model so operators can save and restore settings |
| manager_users | Manages user accounts: creation, password hashing, role assignment, lockout tracking |
| _pki | Implements the internal Certificate Authority: generates the root cert on first boot, signs agent and service certs |
| _archive | Handles encrypted export and import of the system configuration for backup and restore |
| stream_pool | Caps how many live streams (logs, terminals, progress feeds) run at once so they cannot exhaust the web server's worker threads |
| `app_context.py` | Shared context dataclass that wires all modules together — carries references to the agent registry, alarm engine session, and other cross-module dependencies |
| `providers/` | Multi-agent provider registry — defines which agent types (llama.cpp, LM Studio) are supported, how their metrics are aggregated across multiple agents, and how agents are routed |

### User Roles

The Manager enforces two access levels:

- **Admin** — full access to every feature, including agent management, user administration, security settings, backups, and the terminal. Admin actions are additionally restricted to requests arriving from trusted network addresses, adding a second layer of protection.
- **Operator** — can view dashboards, control AI models, and manage inference workloads, but cannot access the Admin tab, manage agents or users, export configuration, or open terminal sessions. This makes Operator the right role for day-to-day lab work without granting infrastructure access.

Accounts are protected with industry-standard scrypt password hashing. After several failed login attempts from the same username or network address, the account is temporarily locked to resist brute-force attacks.

### Dependencies

The Manager talks to agents (over HTTPS) to collect live AI state and to proxy control commands. It talks to the Alarm Engine (over HTTPS) to forward metrics, retrieve alert state, and proxy history chart data to the browser. It also reads from its own local SQLite database for AI model benchmark results.

---

## Agent

### What It Does

An Agent is a small background process that runs on each computer in the lab. Its job is to continuously measure what the hardware is doing — CPU load, memory use, GPU temperature, power draw, and more — and send that data to the Alarm Engine for storage and analysis. On computers that run AI software, the Agent also exposes a control interface so the Manager can start, stop, and configure the AI server remotely.

Each Agent is tailored to its host: on a Linux machine with an AMD GPU and liquid cooling, the Agent enables the GPU, liquidctl, and UPS collectors; on a macOS laptop running LM Studio, it enables the macOS power metrics and LM Studio provider instead. This selective activation keeps each agent lightweight, collecting only what is relevant for that machine.

### Key Responsibilities

- Collects hardware metrics every 5 seconds from CPU, memory, disk, network, GPU, cooling hardware, and UPS
- Batches those metrics and sends them to the Alarm Engine every 15 seconds
- Pushes a live state snapshot to the Manager every 5 seconds (held in memory only, not stored to disk)
- Controls the AI server on its host: load/unload models, adjust settings, stream logs
- Registers itself with the Manager on startup and exchanges a security certificate for encrypted communication
- Buffers data to disk if the Alarm Engine is temporarily unreachable, so no measurements are lost
- Provides interactive terminal sessions for operators who need shell access

### Collector Modules

| Module | What It Reads |
|---|---|
| system | CPU usage and temperature, RAM and swap, disk space and I/O, network traffic |
| gpu | GPU utilization, temperature, memory use, and power draw (AMD via sysfs, NVIDIA via nvidia-smi) |
| liquidctl | All-in-one liquid cooler pump and fan speeds, power supply wattage and efficiency, fan controller readings |
| `_shared.py` | Shared sensor cache — runs `sensors` (hardware monitoring tool) and caches results for all collectors so it is only queried once per polling cycle |
| ups | Uninterruptible power supply battery level, load percentage, and estimated runtime |

### Provider Modules

| Module | What It Controls |
|---|---|
| llama | Full control of the llama.cpp AI server: load and unload models, read generation speed, stream server logs, manage configuration |
| lms | Control of LM Studio: list loaded models, load/unload, read inference metrics, stream logs |
| terminal | Spawns and manages interactive shell sessions (PTY) that stream input and output to the browser |

### Metric Buffering

When the Agent collects a metric sample, it does not send it immediately over the network. Instead, it hands the sample to a component called the BufferedMetricClient, which accumulates samples in memory and sends them to the Alarm Engine in batches every 15 seconds. This batching reduces network overhead and makes the Agent more resilient to brief network hiccups.

If the Alarm Engine becomes unreachable — for example during a restart or a network outage — the BufferedMetricClient does not drop the data. It automatically spills older samples to files on the local disk. Once the Alarm Engine comes back online, the client drains the backlog in controlled batches so that historical data is preserved without overwhelming the engine with a sudden burst of old records.

### Registration and Heartbeat

When an Agent starts up for the first time on a new machine, it registers itself with the Manager and waits to be approved by an administrator. Approval happens through the Admin tab — the administrator reviews the agent's details and clicks Approve. Once approved, the Manager generates a unique TLS certificate for that agent and delivers it securely.

After that, the Agent sends a heartbeat to the Manager roughly once per minute. The Manager uses these heartbeats to know which agents are online and to push configuration updates (such as the address of the Alarm Engine and the ingest token) back to the agent. If an agent stops sending heartbeats, the Manager marks it offline and the dashboard reflects that immediately.

### Dependencies

Each Agent sends metric batches directly to the Alarm Engine (bypassing the Manager for this data path, which keeps the pipeline efficient). It also sends live state pushes to the Manager for the dashboard. The Agent receives its security certificate and configuration updates from the Manager via a registration and heartbeat handshake.

---

## Alarm Engine

### What It Does

The Alarm Engine is the system's data store and alerting brain. It receives a continuous stream of measurements from every Agent, writes them to a time-series database for historical analysis, and constantly checks whether any measurement has crossed a threshold that warrants an alert. When something goes wrong — a GPU overheating, disk filling up, or AI server stalling — the Alarm Engine fires an alert and sends notifications to configured channels.

The Alarm Engine runs as a completely standalone service with its own process, its own Python environment, and its own systemd unit. This separation means a crash or restart of the Alarm Engine does not affect the Manager or agents, and vice versa. Agents continue buffering measurements to disk if the engine is temporarily unavailable and resume delivery once it comes back online.

### Key Responsibilities

- Receives metric batches from agents and writes them to InfluxDB for storage and charting
- Evaluates every incoming measurement against all active alarm rules
- Manages the full alert lifecycle: firing, acknowledging, and resolving alerts
- Sends notifications via email, HTTP webhook, Discord, SMS, and in-dashboard pop-ups when alerts fire or clear
- Streams live alert events to the browser via WebSocket so the Events tab updates instantly
- Provides historical metric data to the Manager for dashboard charts
- Stores alarm rules, notification channel configurations, and delivery history in a local database

### How Alerts Work

When a metric batch arrives, the Alarm Engine checks each measurement against every active rule. A rule says something like "alert when GPU temperature exceeds 85 °C." If a measurement crosses that threshold, the engine does not fire an alert immediately — many rules include a **dwell time**, which requires the condition to remain true for a minimum duration (for example, 30 seconds) before the alert actually fires. This prevents false alarms from brief transient spikes.

Once the dwell time is satisfied, the alert moves to the **firing** state and the engine sends notifications to all configured channels. An operator can **acknowledge** the alert to indicate they are aware of it; this silences repeated notifications while the condition persists. When the measurement returns to a safe level, the alert is automatically **resolved** and a resolution notification is sent. All of this history — every state transition — is recorded so operators can review what happened and when.

### Engine Modules

| Module | Responsibility |
|---|---|
| rule_engine | Loads all active rules and runs each incoming measurement through them |
| threshold_evaluator | Applies the actual threshold comparison logic (greater than, less than, equal to) including dwell time tracking |
| anomaly_detector | Optionally checks for statistically unusual values even when no fixed threshold is crossed |
| alert_manager | Manages the alert state machine (firing → acknowledged → resolved) and persists alerts to the database |
| notification_dispatcher | Sends alerts to configured channels: email via SMTP, HTTP webhook, Discord webhook, SMS, and in-dashboard pop-up notifications |

### Storage

| Store | Contents | Who Owns It |
|---|---|---|
| InfluxDB (metrics bucket) | Raw time-series measurements from all agents | Alarm Engine writes; Manager reads via proxy |
| InfluxDB (rollup bucket) | 1-minute downsampled averages for efficient long-range charts | Alarm Engine writes via a Flux task |
| SQLite ae_alarms.db | All alerts: current state, history of every state change | Alarm Engine |
| SQLite ae_notif_rules.db | Alarm rules, notification channels, delivery history | Alarm Engine |

### Notification Channels

Notification policies can be set to alert immediately on first firing, or only after the condition has persisted for a configurable amount of time — avoiding a flood of messages from a brief transient event. Each policy can also specify a cooldown period to suppress repeated notifications once an alert has already been sent.

The Alarm Engine can send alert notifications through five channels, each configured independently:

- **Email** — sends a formatted message via an SMTP server to one or more addresses. Useful for paging on-call staff or creating a permanent audit record.
- **HTTP Webhook** — posts a JSON payload to any URL. This makes it easy to integrate with monitoring platforms, ticketing systems, or custom scripts.
- **Discord Webhook** — posts a formatted message directly to a Discord channel. Useful for teams that use Discord as a communication hub.
- **SMS** — sends a text message through a configured SMS provider. Useful for urgent paging when staff may not be at a screen.
- **Toast** — shows a pop-up notification in the dashboard itself, so anyone watching the Events tab sees the alert without leaving the page.

Each channel can be enabled or disabled independently, and each alarm rule can specify which channels to use. The engine records every delivery attempt — including failures — so operators can confirm notifications were sent.

### Dependencies

The Alarm Engine receives data from agents (direct metric push) and from the Manager (metric forwarding for the manager's own host). It reads and writes InfluxDB for time-series data. It provides chart history and alert data to the Manager, which proxies those to the browser. It sends notifications outbound to its configured channels (email, webhook, Discord, and SMS providers).

---

## Web Dashboard

### What It Does

The Web Dashboard is the browser-based interface that operators use to see everything happening in the lab at a glance. It is a single web page that updates in real time without requiring a page reload. Operators can view hardware metrics, control AI models, review and acknowledge alerts, run benchmarks, and manage system configuration — all from a web browser.

The dashboard is built entirely with standard browser technologies (HTML, CSS, and JavaScript) and requires no build tools, no compiler, and no framework. This keeps the codebase straightforward to maintain and means the UI can be served directly from the Manager without a separate build step.

### Tabs

| Tab | What It Shows / Does |
|---|---|
| LLM Overall | A combined summary view of all AI activity across both llama.cpp and LM Studio |
| Dashboard — llama.cpp | Live hardware metrics, GPU stats, model status, and performance charts for the llama.cpp host |
| Dashboard — LM Studio | Live metrics and model status for the LM Studio host |
| LLM Control | Load and unload AI models, adjust server settings, run benchmarks, download new models |
| OpenClaw | Analytics for Claude Code usage: token counts, cost trends, tool attribution |
| LLM Chat | Embedded chat interface connecting directly to the running AI model |
| Image Generation | Embedded interface for the image generation server |
| Events | Live alert feed; shows firing, acknowledged, and resolved alerts in real time |
| Admin | Agent management, user accounts, authentication settings, system health, backups, and certificate management |

### Real-Time Updates

The dashboard uses three different mechanisms to keep information current, each chosen to match how that type of data changes:

**Polling** is used for most metrics. The dashboard sends a request to the Manager on a timer — every 2 seconds when the AI is actively generating (to catch fast-changing values like tokens per second), or every 30 seconds when the lab is idle. This adaptive approach avoids unnecessary network traffic when nothing interesting is happening.

**Server-Sent Events (SSE)** are used for long-running operations like downloading a model, running a benchmark, or tailing a log file. Instead of the browser repeatedly asking "are you done yet?", the server pushes each new line of progress as it becomes available. This gives smooth, real-time progress feedback without wasting requests.

**WebSocket** is used for alert events. A persistent two-way connection is maintained between the browser and the Alarm Engine (via the Manager). When an alert fires, is acknowledged, or resolves anywhere in the fleet, that event arrives in the browser within milliseconds — no polling delay.

### Multi-Agent Support

When the lab has more than one computer running the same type of AI software (for example, two machines each running llama.cpp), a small switcher chip appears at the top of each relevant panel. The operator can click it to pin that panel to a specific machine's data. Panels without a selection automatically follow a default agent. The selection is remembered across page reloads so the view stays consistent.

The Manager also supports routing API requests to a specific agent based on which model is currently loaded — so if a request targets a model that is only loaded on one particular machine, it is automatically sent there regardless of the chip-picker selection. A visible indicator in the interface alerts the operator whenever automatic routing overrides a manual selection.

### Chart History

All historical metric charts in the dashboard are backed by data stored in InfluxDB. The Manager fetches chart history from the Alarm Engine and hands it to the browser. To avoid hammering the database on every page load, the Manager caches history responses for a short time. Charts snap incoming data points to a consistent time grid so that live polling and historical backfill align correctly — a spike that arrived just before the page loaded will appear in the right place on the chart rather than being collapsed to the current moment.

---

## Internal Certificate Authority

### What It Does

When computers communicate over a network, **TLS** (Transport Layer Security) encrypts the connection so that data cannot be read or tampered with in transit. Normally, this encryption relies on certificates issued by a trusted third party (a "public CA" like Let's Encrypt). LLM Systems Manager instead runs its own private Certificate Authority (CA) so the fleet can encrypt all internal communications without needing certificates from the public internet.

On first startup, the Manager generates a self-signed root certificate — a cryptographic anchor that the whole fleet trusts. When a new agent is approved by an administrator, the Manager generates a unique certificate for that agent, signed by the root. The Manager signs its own HTTPS certificate the same way. The Alarm Engine also receives a certificate so its connections are encrypted.

### Certificate Lifecycle

Agent certificates are issued at approval time and are valid for one year. The Manager and Alarm Engine certificates are automatically rotated at startup if they are approaching expiry. The Admin tab's system health card shows the status of all certificates and warns when any are nearing expiration, giving operators time to act before a certificate expires and disrupts communication.

### Why This Matters

Every component in the fleet uses these certificates to verify that it is talking to a genuine, approved part of the system — not an imposter on the local network. Because the Manager controls the CA, no external service is needed to issue or renew certificates; rotation is largely automatic. The certificates are used for transport encryption only; the system still requires a valid bearer token (a shared secret) for actual authentication, so encryption and identity verification work as separate, complementary layers.

---

## Unified Configuration

### What It Does

Both the Manager and the Alarm Engine read their settings from a single configuration file written in TOML format (a simple, human-readable key-value format). Having one file means an operator only needs to look in one place to find or change any setting — there is no hunting across multiple config files in different locations. The file lives at a known path on disk and is kept private (readable only by the service account) because it contains credentials.

### Key Settings

| Category | What It Controls |
|---|---|
| Ports | Which network ports the Manager, Alarm Engine, and TLS servers listen on |
| Authentication | Login mode (required, trusted network, or disabled), session duration, lockout thresholds |
| Polling | How often components check for updates, how long to wait before declaring a component offline |
| Notifications | SMTP server address and credentials for email alerts |
| Database | InfluxDB connection details and access tokens for each data bucket |
| TLS | Whether the Alarm Engine serves HTTPS, certificate paths, WebSocket proxy port |
| Ingest security | Whether the alarm engine's metric ingestion endpoint requires a bearer token; when unset, the endpoint accepts data from any source on the network |

### Defaults Behavior

Every setting in the configuration file has a built-in default. A fresh installation without any configuration file at all will start successfully and operate with those defaults. This means operators can get the system running before fine-tuning settings, and a missing or incomplete file is never a hard failure.

The repository includes a fully documented example file that shows every available setting with a description and a placeholder value — operators copy this example to create their own config without risk of accidentally committing real credentials.

Per-host agent settings (such as which hardware providers to enable, where to find binaries, and which processes to watch) are stored separately on each agent host and are not part of this file. This separation means changing a global setting does not require touching every agent machine.

---

## Installer

### What It Does

The Installer is a Bash script that automates setting up LLM Systems Manager on a new server.

### Installation Modes

| Mode | What Gets Installed |
|---|---|
| Full stack | Manager + Alarm Engine + local Agent + InfluxDB on one machine |
| Manager + Alarm Engine | Both services, assuming InfluxDB is already running elsewhere |
| Manager only | Just the web dashboard and API server |
| Alarm Engine only | Just the metrics storage and alerting service |
| Agent only | Just the monitoring and control agent (works on both Linux and macOS) |
| InfluxDB only | Just the time-series database, with all required buckets and tokens provisioned |
| Update | Re-run the installer against an already-installed deployment to sync changes |

### What the Installer Does

A typical full-stack installation proceeds through these steps:

1. **Creates a system user** (`llmsys`) with appropriate permissions; no interactive login allowed for security
2. **Installs Python dependencies** into isolated virtual environments for each component so packages do not conflict
3. **Installs and configures InfluxDB** (the time-series database), creates the required data buckets, and generates scoped access tokens for each
4. **Bootstraps the configuration file** by asking the operator for network addresses, SMTP settings, and an admin password, then writing a properly formatted TOML file with the correct permissions
5. **Issues the Alarm Engine's TLS certificate** using the Manager's Certificate Authority so the Alarm Engine can serve HTTPS from the very first boot
6. **Writes systemd unit files** that define how each service starts, what user runs it, and what environment variables it receives
7. **Enables the systemd units** so they will start automatically on reboot — but does not start them, leaving the operator in control of timing

The installer is intentionally conservative: it never deletes existing data, never overwrites a config file that the operator has already customized, and never starts services without operator consent. If an installation step fails, the installer reports the specific error and stops rather than silently continuing in a broken state.

### What the Agent Installer Handles

The agent installer is a separate sub-script that works on both Linux and macOS. On Linux it writes a systemd unit; on macOS it writes a launchd plist. In both cases it creates the required directories, writes a default configuration file, and registers the agent with the Manager. Remote updates of agents can be triggered from the Admin tab in the dashboard, which re-runs the agent installer in update mode on the remote host.

### Updating

The installer includes a self-update mechanism: before making any changes, it checks whether a newer version of itself is available and automatically re-executes the newer version if one is found. This means running the installer from a `curl` command always uses the latest logic. The dedicated Update mode (mode 7) detects which components are installed, compares files against the current source, backs up anything that changed, syncs only the changed files, refreshes Python dependencies, and restarts affected services — all without touching files that have not changed.
