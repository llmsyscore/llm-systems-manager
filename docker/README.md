# Docker Compose — control plane

One-command bring-up of the **control plane** — the Flask manager, the alarm
engine, and InfluxDB v2 — for users who don't want to run the installer.

**Agents are not containerized.** They read host sensors, GPUs, systemd units,
and PTYs, so they run natively on each monitored host — install them with the
regular installer (mode 5). See the top-level README.

## Quick start

```bash
git clone https://github.com/llmsyscore/llm-systems-manager.git
cd llm-systems-manager
cp .env.example .env
# fill in LSM_INFLUX_PASSWORD, LSM_INFLUX_TOKEN, LSM_AE_INGEST_TOKEN
# (openssl rand -hex 32 makes good tokens)
docker compose up -d
```

Then open `http://<docker-host>:5000/`. First `up` builds the images from
source; to use the published multi-arch images (amd64 + arm64) instead:

```bash
docker compose pull && docker compose up -d
```

Images are published to ghcr.io on every release tag:

- `ghcr.io/llmsyscore/llm-systems-manager/manager:<vX.Y.Z|latest>`
- `ghcr.io/llmsyscore/llm-systems-manager/alarm-engine:<vX.Y.Z|latest>`

Pin a version with `LSM_IMAGE_TAG=v1.0.0` in `.env`.

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
  `LSM_ALARM_ENGINE_URL`. When native agents on other machines will push
  metrics, set `LSM_ALARM_ENGINE_URL=http://<docker-host-LAN-IP>:8081` so the
  advertised URL is reachable from the agents **and** covered by the cert.
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
