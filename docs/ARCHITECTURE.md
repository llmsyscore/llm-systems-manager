# LLM Systems Manager — Architecture

## What Is This System?

LLM Systems Manager is a monitoring and control dashboard for an AI lab. It watches over
multiple servers running AI models — tracking temperatures, memory usage, processing speeds, and
whether models are responding — and displays everything in a live web dashboard accessible from a
browser on the local network.

When something goes wrong  the system can automatically send an alert by email, a chat webhook, or Discord. Operators can also use the
dashboard to load and unload AI models, run benchmarks, and review recent performance history.

The system is made up of four main pieces: a central control server (the Manager), lightweight
monitoring programs installed on each machine being watched (Agents), a dedicated alert-processing
service (the Alarm Engine), and a time-series database that stores all the historical numbers
(InfluxDB). Everything communicates over the local network using encrypted connections.

---

## System Overview

| Component | What It Does |
|---|---|
| **Manager** | The central hub. Hosts the web dashboard that operators use in their browser. Keeps track of which Agents are registered and approved. Forwards metric history requests to the Alarm Engine and proxies LLM control commands to Agents. Runs on ports 5000 (HTTP) and 5443 (HTTPS). |
| **Agent** | A lightweight program installed on each monitored computer. Reads hardware sensors (CPU, GPU, RAM, temperatures, fans, power) every 5 seconds and ships those readings to the Alarm Engine and the Manager. Also exposes controls so operators can start, stop, and configure AI models on that machine. Runs on port 8082 (HTTPS only). |
| **Alarm Engine** | Receives all incoming metric batches, stores them in InfluxDB, and checks every reading against configured alarm rules. When a threshold is crossed, it creates an alert, sends notifications (email, webhook, Discord), and streams the event live to the dashboard. Runs on port 8081 (HTTPS). |
| **Time-Series Database** | InfluxDB stores every metric reading over time so the dashboard can display history charts. It keeps two copies of the data: full-resolution readings (one every 5 seconds) and a compressed summary (one per minute) for longer time windows. Runs on port 8086. |

---

## Network Topology

```
  ┌─────────┐                ┌───────────────────────────────────────────────────────┐
  │         │  :5000 / 5443  │                    Manager Server                     │
  │ Browser │───────────────►│  ┌──────────────┐  :8081   ┌──────────────┐           │
  │         │                │  │   Manager    │─────────►│ Alarm Engine │           │
  └─────────┘                │  │  (Flask)     │◄─────────│  (FastAPI)   │           │
                             │  └──────┬───────┘  proxy/  └──────┬───────┘           │
                             │         │           push           │ :8086            │
                             │         │                          ▼                  │
                             │         │                 ┌────────────────┐          │
                             │         │                 │    InfluxDB    │          │
                             │         │                 │   (port 8086)  │          │
                             └─────────┼─────────────────┴────────────────┴──────────┘
                                       │
                     ┌─────────────────┴──────────────────┐
                     │  :8082 (control calls)             │
                     ▼                                    ▼
           ┌──────────────────┐               ┌──────────────────┐
           │   Agent Host A   │               │   Agent Host B   │
           │   (FastAPI)      │               │   (FastAPI)      │
           └────────┬─────────┘               └────────┬─────────┘
                    │                                  │
                    │  :8081 metric batches            │  :8081 metric batches
                    └──────────────────┐  ┌────────────┘
                                       ▼  ▼
                                  Alarm Engine (above)

  Note: all Agent ↔ Manager and Agent ↔ Alarm Engine connections use TLS.
```

---

## How Data Flows

1. **Collection** — On a configurable intervaal, an Agent reads the hardware sensors on its host machine: CPU
   load, RAM usage, GPU temperature and memory, fan speeds, power draw, and network activity. If an
   AI model server is running on that machine, the Agent also reads its current state (which model is
   loaded, how fast it is generating tokens, how much context is in use).

2. **Buffered forwarding to the Alarm Engine** — The Agent queues those readings in memory. Every
   15 seconds it sends the accumulated batch to the Alarm Engine over an encrypted connection. If the
   Alarm Engine is temporarily unreachable, the readings are saved to disk so nothing is lost.

3. **Live state push to the Manager** — Simultaneously, the Agent sends the most recent snapshot
   directly to the Manager every 5 seconds. The Manager holds this in memory (not written to disk) so
   the dashboard always shows the freshest possible numbers.

4. **Storage** — The Alarm Engine writes each incoming batch to InfluxDB. A background task also
   compresses older data down to one-minute averages to keep storage manageable over longer periods.

5. **Alert evaluation** — As each reading arrives, the Alarm Engine checks it against every active
   alarm rule. If a value crosses a threshold (e.g., GPU temperature above 85 °C), the engine opens
   an alert, writes it to its local database, and immediately sends notifications to all configured
   channels (email, Discord, webhook).

6. **Dashboard display** — The web dashboard polls the Manager every 2 to 30 seconds (faster when a
   model is actively running). For live numbers it reads from the Manager's in-memory snapshot; for
   history charts it asks the Manager, which forwards the query to the Alarm Engine, which reads from
   InfluxDB. Alert events are also pushed to the dashboard in real time over a persistent WebSocket
   connection.

---

## Data Flow Diagram

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │  AGENT (runs on each monitored machine)                             │
 │                                                                     │
 │  Hardware sensors → collector_loop (every 5s)                       │
 │         │                                                           │
 │         ├─► BufferedMetricClient ──► POST /api/alarm/metrics/batch  │
 │         │        (flush every 15s)          │                       │
 │         │                                   │                       │
 │         └─► POST /api/remote/provider-state (every 5s, in-memory)   │
 │                        │                    │                       │
 └────────────────────────┼────────────────────┼───────────────────────┘
                          │                    │
                          ▼                    ▼
              ┌───────────────┐   ┌────────────────────────────────────┐
              │    Manager    │   │         Alarm Engine               │
              │  (in-memory   │   │                                    │
              │   snapshot)   │   │  write to InfluxDB                 │
              │               │   │       │                            │
              │  /api/metrics │   │  evaluate alarm rules              │
              │  /api/history─┼───┤       │                            │
              │  (proxy)      │   │  if threshold crossed:             │
              └───────┬───────┘   │    ├─ open alert (SQLite)          │
                      │           │    ├─ send email / webhook /       │
                      │           │    │  Discord notification         │
                      │           │    └─ push WebSocket event         │
                      │           └──────────────────┬─────────────────┘
                      │                              │
                      ▼                              ▼
              ┌───────────────┐           ┌──────────────────┐
              │    Browser    │◄──────────│  Live WS stream  │
              │  (Dashboard)  │  alerts   │  to dashboard    │
              │               │           └──────────────────┘
              │  polls every  │
              │  2–30 seconds │
              └───────────────┘
```

---

## Security Model

All communication between components is encrypted using TLS. 
The Manager generates its own internal Certificate Authority on first startup — essentially
acting as its own trusted signing authority for the private network. It uses this CA to issue
certificates for each approved Agent and for the Alarm Engine, so every connection can be verified
end-to-end without relying on public internet certificate authorities.

Agents must be explicitly approved by an administrator before they can send data or receive commands.
Approval issues the Agent a signed certificate and a bearer token; both must be present for the
Manager and Alarm Engine to accept requests from that Agent. Short-lived tokens are used for live
dashboard streams (SSE) because browser APIs for those connections cannot send custom headers.

The web dashboard requires a username and password. Passwords are stored as one-way hashes (scrypt)
and are never written in plain text anywhere. There are two access levels: **Admin** users can manage
agents, users, alarm rules, and system configuration; **Operator** users can monitor the lab and
control AI models but cannot change security settings or manage other users. The system also enforces
automatic lockout after repeated failed login attempts to resist brute-force attacks.

---

## Storage

| What | Where | Purpose |
|---|---|---|
| **Performance metric history** | InfluxDB — two buckets: raw (5 s resolution) and rollup (1-min averages) | Feeds all dashboard history charts and alarm rule evaluation |
| **Active alerts and alert history** | SQLite — `ae_alarms.db` (owned by Alarm Engine) | Records when alerts fired, were acknowledged, and were resolved |
| **Alarm rules, notification channels, delivery log** | SQLite — `ae_notif_rules.db` (owned by Alarm Engine) | Defines what triggers an alert and where notifications are sent |
| **User accounts and roles** | JSON file — `data/manager_users.json` (access-restricted) | Stores scrypt-hashed passwords and Admin/Operator role assignments |
| **Dashboard layout preferences** | JSON file — `data/layout.json` | Remembers card order, hidden panels, and colour theme per installation |
| **AI model configuration profiles** | JSON file — `data/model_profiles.json` (access-restricted) | Stores named sets of model-server parameters that operators can apply in one click |
| **Benchmark results** | SQLite — `data/metrics.db` (owned by Manager) | Stores average generation and processing speeds per model for comparison |
