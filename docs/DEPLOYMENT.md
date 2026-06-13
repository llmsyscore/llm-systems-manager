# LLM Systems Manager — Deployment Guide

This guide walks you through installing, configuring, and maintaining LLM Systems Manager on your servers.

---

## Prerequisites

### Manager Server

- **Operating system:** Ubuntu 22.04 or later (other modern Linux distributions should work)
- **Python:** 3.11 or later
- **RAM:** 2 GB minimum; 4 GB recommended
- **Disk:** At least 10 GB free for logs, metrics history, and model benchmark data

### Remote Agent Hosts

Agents can run on:

- **Linux** — Ubuntu 22.04+ or equivalent, Python 3.11+
- **macOS** — macOS 13 (Ventura) or later, Python 3.11+

Each agent host needs network access to ports 8081 (alarm engine) and 5000 or 5443 (manager).

### Required Ports

The following (configurable) ports must be reachable between the components listed.

| Port | What it is | Who needs to reach it |
|------|------------|----------------------|
| 5000 | Manager web interface (HTTP) | Browser |
| 5443 | Manager web interface (HTTPS, optional) | Browser |
| 5444 | Alarm event WebSocket proxy | Browser |
| 8081 | Alarm Engine API — receives metrics from agents | Agents and Manager |
| 8082 | Agent API — manager contacts the agent here | Manager |
| 8086 | InfluxDB time-series database | Alarm Engine and Manager |

---

## Installing the Full Stack

For a quick installation install on one host, choose the full install option:

### Step 1:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
```

### Step 2: Choose an Installation Mode

You will see a menu like this:

```
  Select the deployment option:

    1)  Full system          manager + alarm engine + agent + InfluxDB
    2)  Manager + alarm      manager + alarm engine (existing InfluxDB)
    3)  Manager only         Flask manager + dashboard
    4)  Alarm engine only    standalone FastAPI alarm engine
    5)  Agent only           Linux + macOS host agent
    6)  InfluxDB only        InfluxDB v2 + scoped tokens (DB host)
    7)  Update installed     detect, diff, backup, sync-only-changed, restart
    8)  Uninstall            remove all services and files, with confirmation prompts
    9)  Quit                 exit with no changes

Mode [1-9]:
```

**Choose option 1** for a first-time setup on a single server. This installs everything you need in one step.

### Step 3: Answer Configuration Prompts

The installer will ask a few questions:

- **Manager address** — the IP address or hostname other machines will use to reach this server
- **Admin password** — the password for the initial admin account. It is stored as a secure hash, never in plain text.
- **SMTP settings** (optional) — email address and credentials for alarm notifications
- **InfluxDB details** — the installer can provision InfluxDB automatically in mode 1

If you are unsure about any optional setting, you can accept the default and change it later in the configuration file.

### Step 4: Start the Services

The installer enables the services but does not start them — you decide when to bring them up. Start all three services:

```bash
sudo systemctl start llm-systems-manager
sudo systemctl start llm-systems-alarm-engine
sudo systemctl start llm-systems-agent
```

Verify each one is running:

```bash
sudo systemctl status llm-systems-manager
sudo systemctl status llm-systems-alarm-engine
sudo systemctl status llm-systems-agent
```

Each should show `active (running)`. If a service failed to start, check the logs (see [Viewing Logs](#viewing-logs) below).

### Step 5: Open the Dashboard

Open a browser and go to:

```
http://<your-server-address>:5000
```

Log in with the default credentials:

- **Username:** `llmadmin`
- **Password:** `llmadmin`

**Important:** Change this password immediately after your first login. Go to the account menu in the top navigation bar and choose **Change Password**.

---

## Installing Agents on Remote Computers

If you already have a manager running and want to start monitoring an additional server, install only the agent on that remote machine.

### Step 1: Get the Installer on the Remote Host

Copy just `tools/installer/install.sh` from an existing manager installation using `scp` or another file-transfer method, then run `bash install.sh` from the directory you copied them into.

The agent installer works on both Linux and macOS. It will ask for the manager server address so the agent knows where to register.

Or you can optionally download and run the installer from github

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
```

### Step 2: Start the Agent (if not started during the installation)

On Linux:

```bash
sudo systemctl start llm-systems-agent
```

NOTE: On macOS, the installer registers a launchd service. Start it with:

```bash
launchctl start com.llm-systems-agent
```

### Step 3: Approve the Agent

New agents must be approved before the manager will accept their data. The agent should appear in the dashboard within about 30 seconds of starting.

1. Open the dashboard in your browser
2. Go to the **Admin** tab
3. Click **Agents**
4. Find the new agent in the list and click **Approve**

Once approved, the agent begins sending metrics and the manager can communicate with it.

---

## Configuration

All runtime settings for the manager and alarm engine live in a single configuration file. The installer creates this file for you. Most settings have defaults and do not need to be changed.

### Configuration File Location

The configuration file is located at:

```
config/llm-systems.toml
```

It is readable only by the service user (file permission 0600).

A fully documented template showing every available setting is located at:

```
config/llm-systems.toml.example
```

Refer to that file when you need to understand what a setting does or when adding a new key.

### Key Settings

| Setting path | What it controls | Default |
|---|---|---|
| `[manager].port` | Port the manager web interface listens on | `5000` |
| `[manager].tls_port` | Port for HTTPS access (set to `0` to disable) | `5443` |
| `[manager.auth].mode` | Login requirement: `required`, `trusted_cidr`, or `disabled` | `required` |
| `[manager].alarm_engine_url` | Network address where the Manager can reach the Alarm Engine | `http://localhost:8081` |
| `[alarm_engine].tls_enabled` | Whether the alarm engine uses HTTPS | `true` |
| `[alarm_engine].ingest_token` | Shared token agents use to send metrics; blank means open | *(set by installer)* |
| `[notifications.smtp].server` | SMTP server hostname for email alarm notifications | *(not set)* |
| `[notifications.smtp].user` | Account / sender address used to send alarm emails | *(not set)* |
| `[influxdb].host` | InfluxDB server address | `localhost` |
| `[influxdb].port` | InfluxDB port | `8086` |

### Applying Changes

After editing `config/llm-systems.toml`, restart the affected service for the changes to take effect.

For changes that affect the manager:

```bash
sudo systemctl restart llm-systems-manager
```

For changes that affect the alarm engine:

```bash
sudo systemctl restart llm-systems-alarm-engine
```

If you changed a setting used by both (such as InfluxDB credentials), restart both.

---

## Updating

### Updating All Components

To update the manager, alarm engine, and any locally installed agent to the latest version, run the installer in update mode:

```bash
cd /opt/llm-systems-manager
bash tools/installer/install.sh --update
```

The update process:

- Detects which components are currently installed
- Compares installed files against the latest version
- Backs up any files that will change
- Syncs only the changed files
- Reloads systemd units and restarts affected services
- Runs the smoke test to confirm the update succeeded

You do not need to stop services first — the updater handles restarts.

### Updating a Remote Agent

To update an agent running on a remote machine without logging into that machine:

1. Open the dashboard
2. Go to the **Admin** tab
3. Click **Agents**
4. Select the agent you want to update
5. Click the **Update** button

The agent downloads and applies the latest version of itself, then restarts.

---

## Monitoring Service Health

### Checking Service Status

Check whether each service is running:

```bash
sudo systemctl status llm-systems-manager
sudo systemctl status llm-systems-alarm-engine
sudo systemctl status llm-systems-agent
```

A healthy service shows `active (running)`. A failed service shows `failed` and usually includes the last few log lines explaining why.

### Viewing Logs

**Manager** — log file updated continuously:

```bash
tail -f /var/log/llm-systems-manager/llm-systems-manager.log
```

Or via journald:

```bash
journalctl -u llm-systems-manager -f
```

**Alarm Engine:**

```bash
journalctl -u llm-systems-alarm-engine -f
```

**Agent:**

```bash
journalctl -u llm-systems-agent -f
```

Add `--since "1 hour ago"` to any journalctl command to limit output to recent entries.

### Dashboard Health Page

The **Admin** tab in the dashboard includes a **System Health** card. It shows:

- Status of each connected agent (online, offline, stale)
- InfluxDB connectivity and write health
- Alarm engine connectivity
- TLS certificate status and expiry

The Admin tab button in the navigation bar turns red when any component reports a problem — you do not need to check manually.

---

## Uninstalling

To remove LLM Systems Manager from a server:

```bash
bash tools/installer/install.sh --uninstall
```

The uninstaller:

- Stops and disables the systemd services
- Removes the installed files
- Prompts before deleting the runtime user account and InfluxDB data, so you can preserve your data if needed

---

## Next Steps

After your deployment is up and running, refer to these documents for deeper reference:

- [ARCHITECTURE.md](ARCHITECTURE.md) — How the components fit together, data flow from agent to dashboard, and the multi-agent model
- [COMPONENTS.md](COMPONENTS.md) — Detailed description of each component: manager, alarm engine, and agent
- [API_REFERENCE.md](API_REFERENCE.md) — Full reference for the manager and alarm engine HTTP APIs, including request/response formats
