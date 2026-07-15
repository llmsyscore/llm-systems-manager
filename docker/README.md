# Docker Compose — control plane

One-command bring-up of the **control plane** — the Flask manager, the alarm
engine, and InfluxDB v2 — for users who don't want to run the installer.

**Agents are not containerized.** They read host sensors, GPUs, systemd units,
and PTYs, so they run natively on each monitored host — install them with the
regular installer (mode 5). See the top-level README.

## Quick start

No repo checkout needed — the images carry all the code. Grab the two files
the stack reads and go:

```bash
base=https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main
curl -O "$base/docker-compose.yml"
curl -o .env "$base/.env.example"
# fill in LSM_INFLUX_PASSWORD, LSM_INFLUX_TOKEN, LSM_AE_INGEST_TOKEN
# (openssl rand -hex 32 makes good tokens)
docker compose up -d          # pulls the published multi-arch images
```

Then open `http://<docker-host>:5000/` and log in (default `llmadmin` /
`llmadmin` — change the password from Admin → Authentication).

Images are published to ghcr.io on every release tag (amd64 + arm64):

- `ghcr.io/llmsyscore/llm-systems-manager/manager:<vX.Y.Z|latest>`
- `ghcr.io/llmsyscore/llm-systems-manager/alarm-engine:<vX.Y.Z|latest>`

Pin a version with `LSM_IMAGE_TAG=v1.0.0` in `.env`. To build from source
instead of pulling, clone the repo and run `docker compose up -d --build`.

## Configuration

Both services read `config/llm-systems.toml`. In containers the entrypoint
**renders it from `LSM_*` environment variables** on every start (see
`.env.example` for the full list); anything not covered falls back to the
defaults in `config/unified_config.py`.

Need a key the env vars don't cover? Bind-mount your own TOML and it is left
untouched (bind mounts are detected via `/proc/self/mounts`; if you instead
copy a generated file around, also delete its `# GENERATED …` marker line):

```yaml
    volumes:
      - ./my-llm-systems.toml:/opt/llm-systems-manager/config/llm-systems.toml
```

Tokens rendered into the TOML must not contain `"` or `\` (hex/base64 tokens
are fine).

The dashboard is served by the manager on port 5000; the alarm engine and
InfluxDB are reached *through* the manager, so browse to the manager, not to
`:8081`/`:8086` directly.

## Native agents against a containerized manager

Agents run natively on each host and dial the manager at this docker host's LAN
address. Point each agent's `MANAGER_URL` at `http://<docker-host-LAN-IP>:5000`
— the agent derives its alarm-engine URL from that (port 8081) automatically,
so there is nothing to configure on the manager side for metric push.

One manager-side setting helps:

- `LSM_MANAGER_PUBLIC_HOST` — the address(es) agents reach the manager at,
  added to the manager's TLS cert SAN so the agent's automatic http→https
  control-channel upgrade validates. Without it the manager cert only covers
  the container's internal IP and the upgrade silently stays on http.

Do **not** set `LSM_ALARM_ENGINE_URL` to the host LAN IP — that is the URL the
manager itself uses for its own AE calls and must stay at the compose service
name (`http://alarm-engine:8081`, the default). Setting it to the host IP makes
every manager→AE call fail (the container can't reach the host's own published
port).

## Notifications

Set `LSM_SMTP_*` and/or `LSM_DISCORD_WEBHOOK_URL` to wire up alert delivery;
they are only written to the config when provided. Anything else (Twilio,
per-rule channels) is configured via a bind-mounted TOML.

## Hardening notes

- The quickstart uses the single InfluxDB admin token (`LSM_INFLUX_TOKEN`) for
  all three `[influxdb.tokens]` slots. A bare-metal install mints per-bucket
  scoped tokens instead — to match that posture, create scoped tokens with
  `influx auth create` inside the influxdb container and supply them via a
  bind-mounted TOML.
- Set `LSM_AE_MANAGEMENT_TOKEN` so the alarm engine's rule/alert/channel
  management routes need a different token than the agents' ingest token.

## TLS / internal CA

Works the same as a co-located bare-metal install:

- The manager creates its internal CA in the `manager-data` volume on first
  boot and signs agent leaf certs from it as you approve agents.
- The `ae-data` volume is mounted into **both** the manager and the alarm
  engine. At startup the manager issues `ae-tls.{crt,key}` into it; the alarm
  engine waits up to 90 s (`LSM_AE_TLS_WAIT_S`) for the cert, then serves
  HTTPS. If the cert isn't there yet it starts on plain HTTP (fail-open) and
  picks up TLS on its next restart.
- The AE cert's SAN covers `localhost` and the host part of
  `LSM_ALARM_ENGINE_URL` (the compose service name by default). Native agents
  push metrics to the AE URL they derive from their own `MANAGER_URL`, so no AE
  change is needed for them.
- Manager HTTPS (port 5443) uses an auto-rotated self-issued cert, as on bare
  metal.

## Ports

| Port | Service | What |
|---|---|---|
| 5000 | manager | dashboard + API (HTTP) |
| 5443 | manager | dashboard + API (HTTPS, internal CA) |
| 5444 | manager | `/ws/alarm` WebSocket proxy |
| 5445 | manager | llama-state SSE daemon |
| 8081 | alarm engine | agent metric ingest + alarms API/UI |
| 8086 | influxdb | not published by default (compose-internal) |

## Persistence

Named volumes: `manager-data` (SQLite benchmarks, agent registry, internal CA,
backups), `ae-data` (alert/rule SQLite DBs + AE TLS cert), `influxdb-data` /
`influxdb-config` (metric history). `docker compose down -v` deletes all of it.

## Updating

```bash
docker compose pull && docker compose up -d
```

Data migrations run automatically at service startup, same as a bare-metal
update.
