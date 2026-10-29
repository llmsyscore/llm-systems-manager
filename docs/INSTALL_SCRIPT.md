# What the install script does

The install command downloads one script and runs it:

```bash
bash <(curl -fsSL https://get.llmsysmanager.com/install.sh)
```

To read the script before running it, save it first:

```bash
curl -fsSL https://get.llmsysmanager.com/install.sh -o install.sh
less install.sh
bash install.sh
```

## Where it runs

- **The dashboard** (manager and alarm engine) runs on Linux: Debian, Ubuntu, and Rocky Linux, AlmaLinux or RHEL 10. On macOS, install the dashboard with [Homebrew](DEPLOYMENT.md#installing-with-homebrew-control-plane) instead.
- **The agent** runs on Linux and macOS (Apple Silicon).
- Windows is not supported.

## What happens, step by step

1. **Checks the basics.** It looks for Python 3.11 or newer, `git`, `jq`, `curl` and `rsync`. If some are missing it offers to install them with `apt` or `dnf`.
2. **Downloads the latest release** from the project's GitHub Releases page and checks its SHA-256 checksum. If the checksum doesn't match, it stops before installing anything.
3. **Asks what to install.** A menu lets you choose the whole system on one machine, one part of it (dashboard, alarm engine, database or agent), an update, or uninstall.
4. **Creates a service account** called `llmsys` that the services run as. You can choose a different account with `--user <name>`.
5. **Installs the files** into `/opt/llm-systems-manager`, with the Python packages each part needs in its own folder.
6. **Installs InfluxDB** (full install or database-only) from InfluxData's package repository, and creates the database and its access tokens.
7. **Asks a few questions:** the dashboard's admin login, email settings for alerts, and whether to send an anonymous install report. Press Enter to accept a default.
8. **Writes the settings file** at `/opt/llm-systems-manager/config/llm-systems.toml` and creates the certificates the parts use to talk to each other securely.
9. **On agent machines**, adds a rule to `/etc/sudoers.d/` so the agent can start and stop your model servers. Skip it with `--no-sudoers`.
10. **Sets up the system services** and asks before starting them. When it finishes it prints the address of the dashboard, the sign-in details and the next steps.

## Useful options

| Option | What it does |
|---|---|
| `--mode <1-9>` | Skips the menu and picks the install type |
| `--ref v3.2.0` | Installs a specific release |
| `--source local` | Installs from a release you downloaded yourself, for machines without GitHub access |
| `--update` | Updates an installed system |
| `--uninstall` | Removes the services and files, and asks before removing the service account and InfluxDB data |

All options: `bash install.sh --help`. Full guide: [Deployment](DEPLOYMENT.md).

## Other ways to install

- **Docker Compose** runs the dashboard in containers. Fill in `.env` before the last command:

  ```bash
  curl -fsSLO https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/docker-compose.yml
  curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/.env.example -o .env
  docker compose up -d
  ```

- **Packages** (`.deb` and `.rpm`) and **Homebrew**: see [Deployment](DEPLOYMENT.md).
