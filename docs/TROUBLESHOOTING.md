# Troubleshooting

Find your problem below. Each entry says what you see, why it happens and how to fix it. If nothing here matches, see [Reporting a problem](#reporting-a-problem) at the end.

- [First checks](#first-checks)
- [Install and update](#install-and-update)
- [Signing in and the dashboard](#signing-in-and-the-dashboard)
- [Agents](#agents)
- [Alarm engine, InfluxDB and alerts](#alarm-engine-influxdb-and-alerts)
- [llama.cpp](#llamacpp)
- [LM Studio and vLLM](#lm-studio-and-vllm)
- [Docker](#docker)
- [Homebrew and macOS](#homebrew-and-macos)
- [.deb and .rpm packages](#deb-and-rpm-packages)
- [Fixed in a recent release](#fixed-in-a-recent-release)
- [Reporting a problem](#reporting-a-problem)

## First checks

1. **Admin → System Health** shows which part is unhealthy. The Admin tab button turns red when something needs attention.
2. Check the service on the machine that runs it:

   ```bash
   sudo systemctl status llm-systems-manager
   sudo systemctl status llm-systems-alarm-engine
   sudo systemctl status llm-systems-agent
   ```

3. Read its recent log lines:

   ```bash
   sudo journalctl -u llm-systems-manager -n 100 --no-pager
   ```

   Use `llm-systems-alarm-engine` or `llm-systems-agent` in place of `llm-systems-manager` for the other services.

4. Make sure the ports between the machines are open: [Required ports](DEPLOYMENT.md#required-ports). The Events and alerts stream on an HTTPS dashboard also uses port 5446.

## Install and update

**The install or uninstall pauses with "waiting for another package manager to finish".**
Another apt process, usually automatic updates on a new machine, is using the package system. The installer waits up to 5 minutes and then carries on. Set `LLMSYS_APT_LOCK_TIMEOUT=<seconds>` to wait longer or shorter.

**The install fails with "pins a release tag to download and conflicts with --source=git", or takes an unexpected path.**
An `LLMSYS_*` variable from an earlier attempt is still set in your shell. Run `env | grep LLMSYS_`, `unset` the ones you did not mean to set, and run the installer again. The installer prints `using LLMSYS_SOURCE=… from the environment` or `using LLMSYS_RELEASE_TAG=… from the environment` when it picks one of them up.

**After reinstalling, the install stops at the InfluxDB setup step with a `jq` error.**
An `influx` settings file from the earlier install is still on the machine. Update to the latest release and run the installer again. On an older release, delete the `configs` file in root's `~/.influxdbv2/` folder first.

**After an update, the Admin tab shows an older release than the one you installed, or no "new release" notice.**
Run the update again: `sudo bash /opt/llm-systems-manager/tools/installer/install.sh --update`. It records the installed release for the dashboard. See [Updating](DEPLOYMENT.md#updating).

**After updating a split install, every agent logs "alarm engine certificate has no role yet".**
The alarm engine still serves its old certificate. On the manager host, copy `data/ae-tls.crt` and `data/ae-tls.key` to the alarm engine host's `data/` folder, make them owned by the user the services run as (`llmsys` by default), and restart the alarm engine. Then run **Admin → Agents → Manage ▾ → Update all agents**. The updater prints these steps under **Next steps**. See [Internal certificate roles](DEPLOYMENT.md#internal-certificate-roles).

**An agent stopped connecting after you moved the manager or turned HTTPS off.**
An agent that has checked the manager's certificate keeps using its HTTPS address.

- New manager address: put the new `https://` address in the agent's config and restart the agent.
- HTTPS turned off: delete `data/tls-roles.json` on each agent's host and restart the agent.

## Signing in and the dashboard

**The dashboard does not load, or the browser shows 502.**
Check the manager service and its log ([First checks](#first-checks)). A manager that fails to start names the reason in its last log lines.

**You lost the temporary password, or the login page says the admin password is not set.**
Create a new one on the manager host:

```bash
sudo /opt/llm-systems-manager/llm-systems-manager/venv/bin/python \
  /opt/llm-systems-manager/llm-systems-manager/backend/admin_password.py reset
```

On Homebrew, run `llm-systems-admin-password reset`. Add `--user <name>` for a different account. After several failed attempts the account is locked for a while; wait, or restart the manager.

**System Health warns "SSE stream pool full", and live panels show "Stream error".**
Too many live streams are open at once, for example many dashboard tabs. Close the tabs you do not need. The warning clears up to 10 minutes after the load drops. If it keeps coming back, raise `[manager].http_threads` (default 64) or lower `[manager].stream_reserve_threads` (default 24) in the manager config and restart the manager. See [Key settings](DEPLOYMENT.md#key-settings).

**Alarm engine settings in Admin → Settings are locked on a split install.**
The manager and the alarm engine need the same `[alarm_engine].management_token`, different from the `ingest_token`. Set it on both hosts and restart both. System Health shows `no token` or `token mismatch` until it matches.

**The Events stream shows "disconnected" on an HTTPS dashboard.**
A browser blocks a plain `ws://` connection from an HTTPS page. Set up a certificate browsers trust and keep `[manager].ws_proxy_tls_port` at 5446, with that port open. See [Operator-provided TLS certificate](DEPLOYMENT.md#operator-provided-tls-certificate).

## Agents

**The agent is installed but the machine does not appear in the dashboard.**
Check these in order:

1. **Not approved.** Go to **Admin → Agents** and press **Approve**.
2. **Enrollment is closed.** Enrollment is open for 15 minutes after each manager start. Press **Open enrollment** on **Admin → Agents**. A waiting agent keeps trying and appears once enrollment opens.
3. **Missing enrollment secret.** The agent log says `registration BLOCKED: the manager requires an enrollment secret`. Copy the secret from **Admin → Agents → Manage ▾ → Enrollment** and run the installer again on that machine with `--enrollment-secret <secret>`.
4. **An old record for the same machine.** The agent log says `registration BLOCKED by stale record on the manager`. Delete the old agent under **Admin → Agents** (⋯ → **Delete agent**); the agent registers again and needs approval.
5. **Waiting for a secure connection.** The agent log has a `control channel:` line that mentions `holding the token`. See [Installing agents](DEPLOYMENT.md#step-1-get-the-installer-on-the-remote-host).

**The machine appears but its charts are flat.**
The agent cannot reach the alarm engine. On the agent's machine, run `curl -i http://<manager-host>:8081/health` (`https://` if the alarm engine uses HTTPS). A `401` means the agent has no ingest token yet: wait up to a minute, or restart the agent.

**GPU, temperature, UPS or cooler cards stay empty.**
Open **Admin → Agents → ⋯ → Edit agent config…** and set `COLLECT_GPU_ENABLED`, `COLLECT_SENSORS_ENABLED`, `COLLECT_UPS_ENABLED` and `COLLECT_LIQUIDCTL_ENABLED` to `auto`. Older installs may have them set to `false`. With `auto`, the agent checks for the hardware again every 15 minutes.

**After a power loss on the manager host, every agent is offline.**
The manager could not read its agent list. The manager log says whether it restored the previous copy or started with an empty list.

- **Previous copy restored:** agents connect again by themselves within a few minutes and keep their approval.
- **Empty list:** approved agents no longer have the enrollment secret, so they cannot register again on their own. Restore the agent list from **Admin → Backups** if you have a backup. Otherwise run the installer again on each machine with `--enrollment-secret <secret>`, approve the agents under **Admin → Agents**, and set default agents, pools and pins again.

**Changing an agent's settings.**
Use **Admin → Agents → ⋯ → Edit agent config…**, press **Save**, then **Restart** the agent when it offers. You can also edit `agent_config.yaml` on that machine and restart `llm-systems-agent`.

## Alarm engine, InfluxDB and alerts

**The Admin tab is red and charts have no history.**
InfluxDB is probably down or unreachable. Open **Admin → System Health**: the alarm engine shows as degraded while it cannot reach InfluxDB. On the InfluxDB host, run `sudo systemctl status influxdb`. A package install also needs its `[influxdb.tokens]` filled in; `REPLACE_ME` means it is not finished.

**`llm-systems-influx-setup` says "waiting for the InfluxDB API to finish starting".**
InfluxDB answers its health check before it is ready. The command waits up to 2 minutes (`LSM_INFLUX_WAIT_S=<seconds>` changes that). If it gives up, check `brew services info influxdb@2` and the InfluxDB log it prints.

**The Admin tab shows the alarm engine as unhealthy on a split install.**
Common causes: the alarm engine certificate was not copied from the manager host (see [Install and update](#install-and-update)), the two hosts have different ingest tokens, or InfluxDB is down.

## llama.cpp

These entries are for a `llama-server` you installed yourself, from a distro package or your own build. The full setup is in [Using a llama-server you installed yourself](DEPLOYMENT.md#using-a-llama-server-you-installed-yourself).

**Models you download in the dashboard are not found by `llama-server`.**
The agent downloads into the Hugging Face cache of its own user and adds the model to `llama-server`'s presets file. Check:

- `llama-server` runs in multi-model mode with `--models-preset <presets file>`. If LLM Control says the server serves one fixed model, press **Switch to multi-model mode**. If there is no presets file, press **Add a presets file**.
- `LLAMA_CONFIG_INI` in the agent config names the same presets file.
- `llama-server` runs as the agent's user, with that user's normal home folder, so both use the same cache.

**Models `llama-server` downloaded itself are not listed.**
The dashboard lists llama.cpp's own cache (`~/.cache/llama.cpp`, or the folder in the service's `LLAMA_CACHE` setting) and the server's `--models-dir` folder. If yours is somewhere else, set `LLAMA_CACHE_DIR` or `LLAMA_MODELS_DIR` in the agent config.

**Downloads fail because `hf` is not found.**
If `hf` is installed in a virtual environment, set `HF_CLI_PATH` to its full path, for example `/home/<user>/venv/bin/hf`. Leave it blank to use `hf` on the path or the built-in download tool.

**The dashboard cannot reach `llama-server`, or start and stop do nothing.**
Set `LLAMA_API_URL` to the server's address (for example `http://localhost:9931` when it does not use port 8080) and `LLAMA_SYSTEMD_UNIT` to its service name. If you see settings the agent found but has not saved, press **Save** on the LLM Control page.

**A button says the agent's helper needs updating.**
Run this once on that machine, then try again:

```bash
sudo bash <(curl -fsSL https://get.llmsysmanager.com/install.sh) --mode 5 -- --update
```

For a .deb install run `sudo dpkg-reconfigure llm-systems-agent`; for an .rpm install run `sudo dnf reinstall llm-systems-agent`. See [Updating the agent's helper](DEPLOYMENT.md#updating-the-agents-helper).

**Update llama.cpp refuses and nothing changes.**
The `llama-server` came from a package (`apt`, `dnf`), from Homebrew, or sits in a folder the agent's user cannot write to. Update it the same way you installed it, for example `sudo apt upgrade` or `brew upgrade llama.cpp`.

## LM Studio and vLLM

**The agent does not see LM Studio.**
Set `LMS_ENABLED: true` in the agent config. The agent finds LM Studio's server and the `lms` command by itself, and the dashboard offers a **Save** button for what it found. Set `LMS_API_URL` or `LMS_CMD` only when they are not found.

**A large LM Studio model is reported as failed to load, then appears loaded.**
Large models can take minutes to load. Raise `LMS_LOAD_TIMEOUT_S` (default 180, keep it under 200).

**A vLLM machine is missing or shows only system metrics.**
Set `VLLM_ENABLED: true`, `VLLM_API_URL` (default `http://localhost:8000`) and `VLLM_SYSTEMD_UNIT` (default `vllm.service`) in the agent config, or install the agent with `--enable-vllm`.

## Docker

**Agents on other machines cannot send metrics to a manager in Docker.**
Set `LSM_MANAGER_PUBLIC_HOST` in `.env` to the Docker host's address on your network, then restart the containers. Leave `LSM_ALARM_ENGINE_URL` at its default. See [docker/README.md](../docker/README.md).

**The Docker host's CPU, memory and disk cards are empty.**
Install an agent on the Docker host, approve it, and turn on the **Manager host** toggle on its row in **Admin → Agents**.

**Updating.**
Run `docker compose pull && docker compose up -d`. The update script is not for container installs.

## Homebrew and macOS

**`llm-systems-influx-setup` stops on a new machine.**
Install both `influxdb@2` and `influxdb-cli`. The plain `influxdb` formula is InfluxDB 3, which does not work with this product. Update the formulas and run the command again.

**`brew tap llmsyscore/tap` fails.**
Update Homebrew (`brew update`), run `brew trust llmsyscore/tap`, and tap again. On a Mac, the agent needs Apple Silicon.

**Start order.**
Start the manager before the alarm engine: `brew services start llm-systems-manager`, then `brew services start llm-systems-alarm-engine`.

## .deb and .rpm packages

**The package refuses to install, or the install script refuses a machine that has the package.**
Install methods do not mix on one machine. See [Mixing install methods](DEPLOYMENT.md#mixing-install-methods) for how to switch.

**The alarm engine is enabled but does not start after a package install.**
It waits until `[influxdb.tokens]` is filled in. See [Installing from native packages](DEPLOYMENT.md#installing-from-native-packages-deb--rpm).

**llama.cpp settings after a .deb or .rpm agent install.**
The package uses the settings it finds on the machine. If `llama-server` was not running during the install, check the [llama.cpp settings](DEPLOYMENT.md#settings-to-check) afterwards.

## Fixed in a recent release

If you see one of these, update to the latest release ([Updating](DEPLOYMENT.md#updating)):

| What you see | Fixed in |
|---|---|
| Email alerts never arrive on SMTP port 465 | v3.1.0 |
| A log-watch alert you closed by hand opens again seconds later | v3.1.0 |
| Autotune or Autopilot counts a model twice when the Hugging Face cache holds two copies | v3.0.0 |
| The alarm engine looks healthy while InfluxDB is down | v3.1.0 |
| A "restart pending" chip stays after the alarm engine restarted | v3.1.0 |
| After an upgrade the dashboard starts with empty history | v3.2.0 |
| The InfluxDB setup fails with a `jq` error after reinstalling | v3.3.0 |
| `llm-systems-influx-setup` fails on a new Homebrew machine | v3.3.0 |
| An agent self-update fails with `tarball download failed` | v3.0.0 |
| `llama-perplexity` or another llama.cpp tool crashes after Update llama.cpp | v3.0.0 |

## Reporting a problem

If nothing above fixes it, open a bug report: **Issues → New issue → Bug report**. Include:

- How you installed it: install script, .deb, .rpm, Docker or Homebrew.
- The versions shown in **Admin → System Health**.
- Your inference server and its version, for example the output of `llama-server --version`.
- For llama.cpp problems: the `ExecStart=` line from the `llama-server` service file, and the `LLAMA_*` and `HF_CLI_PATH` lines from `agent_config.yaml`.
- Log lines from the time of the problem ([First checks](#first-checks)).

Remove passwords, tokens and addresses before you post.
