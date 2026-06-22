#!/usr/bin/env bash
# =============================================================================
# Universal LLM Systems Agent Installer
# install.sh — LLM Systems Agent installer / uninstaller (Linux + macOS)
#
# Install:
#   ./install.sh [--user USER] [--install-dir DIR] [--manager-url URL]
#                [--role auto|llama_host|lms_host|mixed]
#                [--enable-perf | --no-perf]
#                [--enable-llama | --no-llama]
#                [--enable-lms   | --no-lms]
#                [--enable-monitor-manager | --no-monitor-manager]
#                [--enable-monitor-alarm   | --no-monitor-alarm]
#                [--no-sudoers]   (skip dropping /etc/sudoers.d snippet — Linux only)
#                [--no-service]   (skip enabling systemd unit / launchd plist)
#                [-y|--yes]       (auto-confirm uninstall destruction;
#                                  does NOT skip the prereq-install prompt —
#                                  system-package installs always require
#                                  interactive consent)
#                [--no-prereq-install]  (refuse missing-prereq install path,
#                                  exit 1 with the manual command instead)
#                [--install-perf-units]  (drop example performance/powersave
#                                  systemd units to /etc/systemd/system/ —
#                                  Linux only; refuses to overwrite existing
#                                  units unless --force-overwrite-perf-units)
#                [--force-overwrite-perf-units]  (allow clobbering tuned units;
#                                  destructive — backs up to .bak first)
#
# Uninstall:
#   ./install.sh --uninstall [--install-dir DIR] [-y|--yes]
#       Stops + disables the service, removes systemd unit / launchd plist,
#       removes the sudoers file, and deletes the install directory.
#       Does NOT touch the agent's registry entry on the manager — delete
#       that from the Admin tab if you want a clean slate there too.
#
# Defaults:
#   --user        $(id -un)
#   --install-dir /opt/llm-systems-agent
#   --manager-url REQUIRED — installer prompts for it (or refuses to run
#                 non-interactively without --manager-url) and confirms
#                 reachability before touching anything
#   --role        auto
#   --enable-perf is OFF unless flag passed; same for --enable-llama / --enable-lms
# =============================================================================
set -euo pipefail

USER_ARG="llmsys"
INSTALL_DIR="/opt/llm-systems-agent"
INSTALL_DIR_EXPLICIT=false   # set true if --install-dir was given on CLI
HOSTNAME_OVERRIDE=""          # --hostname: agent display name in logs / admin tab
DESCRIPTION_OVERRIDE=""       # --description: free-text label shown in admin tab
MANAGER_URL="http://127.0.0.1:5000"   # placeholder default shown in the prompt;
                                       # the installer always prompts (or requires
                                       # --manager-url) and confirms reachability
                                       # before doing anything destructive.
MANAGER_URL_EXPLICIT=false             # set true if --manager-url given on CLI
ALARM_ENGINE_URL=""                    # optional CLI override; empty → derived
ALARM_ENGINE_URL_EXPLICIT=false        # set true if --alarm-engine-url given
ROLE="auto"
ENABLE_PERF=false
PERF_FLAG_EXPLICIT=false   # set true if --enable-perf/--no-perf given on CLI
ENABLE_LLAMA=false
ENABLE_LMS=false
ENABLE_OPENCLAW=false
ENABLE_IMGGEN=false
# Self-monitor probes — additive to whatever provider role is chosen.
# Default false; auto-detect flips them on when systemd reports the
# matching unit is active on this host.
ENABLE_MONITOR_MANAGER=false
ENABLE_MONITOR_ALARM=false
ENABLE_MONITOR_INFLUXDB_DISK=false

# Per-provider 'was the enable flag set explicitly on CLI?' trackers.
# When true, auto-detect skips that provider's probe so an explicit
# --no-llama / --no-lms / --no-openclaw isn't overridden by what
# happens to be running on the host.
LLAMA_FLAG_EXPLICIT=false
LMS_FLAG_EXPLICIT=false
OPENCLAW_FLAG_EXPLICIT=false
MONITOR_MANAGER_FLAG_EXPLICIT=false
MONITOR_ALARM_FLAG_EXPLICIT=false
MONITOR_INFLUXDB_DISK_FLAG_EXPLICIT=false

# Path/URL overrides — set when the user customizes a value during the
# auto-detect prompts. Empty means "use the YAML default."
LLAMA_API_URL_OVERRIDE=""
LLAMA_LOG_FILE_OVERRIDE=""
LLAMA_SYSTEMD_UNIT_OVERRIDE=""
LLAMA_BIN_OVERRIDE=""
LLAMA_CONFIG_INI_OVERRIDE=""
LLAMA_BUILD_METHOD_OVERRIDE=""
# Setup-time llama.cpp install via install/install_llama.py (run as the agent
# user); wires LLAMA_BIN and the build method to the result.
INSTALL_LLAMA=false
INSTALL_LLAMA_METHOD="release_binary"
INSTALL_LLAMA_BACKEND="cpu"
LMS_API_URL_OVERRIDE=""
LMS_CMD_OVERRIDE=""
OPENCLAW_AGENTS_DIR_OVERRIDE=""
SKIP_SUDOERS=false
SKIP_SERVICE=false
SKIP_START=false        # --no-start: enable unit but don't `--now` it
ASSUME_YES=false           # auto-confirm uninstall destruction prompt
                           # (does NOT skip the prereq-install consent prompt —
                           # touching system packages always requires the user
                           # to type the confirmation themselves)
SKIP_PREREQ_INSTALL=false  # never offer to install prereqs; exit with the
                           # suggested command instead
DO_UNINSTALL=false          # --uninstall mode
INSTALL_PERF_UNITS=false    # drop example performance/powersave systemd units
FORCE_OVERWRITE_PERF=false  # allow clobbering existing perf units (backs up first)

VERSION="v2026.06.11-3"

usage() {
  cat <<'EOF'
LLM Systems Agent — installer / uninstaller (Linux + macOS)

USAGE
  install.sh [FLAGS]              # default mode: install
  install.sh --update             # fetch latest agent/ + redeploy + restart
  install.sh --uninstall [FLAGS]  # remove the agent from this host

  See `--help` ADVANCED section for self-update / no-pull / debug flags.

INSTALL — agent identity
  --user USER
      Run-as user for the systemd unit / launchd plist (and sudoers entry
      on Linux). Default: llmsys. On Linux, if the default doesn't exist
      yet the installer offers to create it as a system user with
      home /home/llmsys and shell /bin/bash; on macOS it re-prompts for an
      existing username (auto-create isn't supported there).

  --install-dir DIR
      Where the agent files + venv + token cache live. Default: /opt/llm-systems-agent.
      A custom DIR has /llm-systems-agent appended automatically (unless it
      already ends in that name) so the agent never dumps files into a
      generic top-level directory.

  --hostname NAME
      Display name used everywhere this agent is identified: registry
      (admin tab → Agents), heartbeat logs on the manager, metric samples
      forwarded to the alarm engine, and the agent's own log heartbeat.
      Default: socket.gethostname() resolved at runtime. Use this when the
      OS hostname is generic (vagrant, ubuntu-22.04) or when you want to
      label one of several agents on the same host.

  --description TEXT
      Free-text label shown in the admin tab next to the agent (e.g.
      "Mac Mini M2 Pro — LMS host"). Quote the value if it contains
      spaces. The installer also prompts for this interactively when the
      flag isn't supplied.

  --manager-url URL
      Base URL of the LLM Systems Manager (e.g. http://192.0.2.10:5000).
      The agent registers here on startup and forwards metrics.
      REQUIRED — if not passed on the CLI, the installer prompts and tests
      reachability before doing anything destructive. Non-interactive
      runs (no TTY) without --manager-url are refused.

  --alarm-engine-url URL
      Base URL of the alarm engine the agent should push samples to
      (e.g. http://10.0.0.5:8081). Optional. When omitted, the installer
      reads [manager].alarm_engine_url from the local manager TOML if the
      manager is installed on this host, otherwise falls back to deriving
      <manager-host>:8081. Pass this when the AE lives on a different
      host than the manager AND the manager is not on this host.

  --role auto|llama_host|lms_host|mixed
      Role label sent in the registration. Default: auto.
      With 'auto', the installer probes the host:
        - llama.cpp:  pgrep llama-server, then HTTP probe at 127.0.0.1:8080,
                      then prompt for paths. Verifies --metrics flag is set
                      on the systemd unit.
        - LM Studio:  pgrep lmstudio/lm-studio, then HTTP at 127.0.0.1:1234,
                      then 'lms status' if the binary is on PATH, then prompt.
        - OpenClaw:   ~/.openclaw, then gateway process on :18789, then
                      'which openclaw', then prompt. Optionally writes the
                      8 'openclaw config set' commands to enable OTEL push
                      to the alarm engine.
      Any explicit role disables the probe — use it when you want to
      override what would be auto-detected.

INSTALL — provider toggles (default OFF for all)
  --enable-perf  /  --no-perf
      Set PERF_CONTROLLER_ENABLED in agent_config.yaml. When true, the
      agent tails the llama-server log and triggers 'performance' /
      'powersave' systemd units on sleep / wake transitions. Linux only.
      Implies --install-perf-units (drops the example units so the controller
      has targets; existing tuned units are never overwritten).
      When this is set (or --install-perf-units is set), the installer
      will additionally check for liquidctl (cooler control via USB HID)
      and offer to install it from upstream PyPI if it's missing —
      requires interactive consent; never auto-installs.

  --enable-llama /  --no-llama
      Set LLAMA_ENABLED. The agent reads /tmp/llama-server-last-state and
      (Phase 2) will proxy llama.cpp lifecycle endpoints.

  --install-llama[=METHOD]   (--llama-backend BACKEND)
      With --role auto (the default), install llama.cpp during setup when none
      is detected, reusing the agent's install machinery (install_llama.py).
      METHOD is source|release_binary|conda|homebrew (default release_binary);
      --llama-backend selects cpu|cuda|vulkan|rocm|metal (default cpu). The
      install runs as the agent user; LLAMA_BIN and the build method are wired
      to the result. With a TTY and no flag, the installer prompts to install
      when no running llama-server is found.

  --enable-lms   /  --no-lms
      Set (LM Studio) LMS_ENABLED. The agent collects 'lms ps' / 'lms server status'
      and posts the LM Studio dashboard payload to the manager.

  --enable-openclaw / --no-openclaw
      Set OPENCLAW_ENABLED. The agent reads ~/.openclaw/agents to surface
      OpenClaw analytics. With --role auto, the installer additionally
      offers to push OpenClaw OTEL telemetry to the alarm engine.

  --enable-monitor-manager / --no-monitor-manager
      Set MONITOR_MANAGER_ENABLED. The agent runs lightweight latency
      probes against MANAGER_URL each META_PERF_INTERVAL_S seconds and
      emits results under source=manager_self_monitor for graphing on
      the dashboard's Self-Monitor cards. Additive to --role; works
      alongside any inference-provider role.

  --enable-monitor-alarm / --no-monitor-alarm
      Set MONITOR_ALARM_ENGINE_ENABLED. As above but probes ALARM_ENGINE_URL
      and also probes InfluxDB directly (write + 5m/24h queries + disk
      usage). When --role auto is set on Linux, both monitor flags are
      flipped on automatically if the matching systemd unit
      (llm-systems-manager.service / llm-systems-alarm-engine.service)
      is active on the host.

  Log-file watcher (no CLI flag — rules are per-site)
      LOG_WATCH_ENABLED + LOG_WATCH_RULES in agent_config.yaml. The agent
      tails the listed log files, regex-matches new lines, and POSTs each
      match to ALARM_ENGINE_URL/api/alarm/ingest as a generic alert.
      See the example file for the rule schema. Offsets persist across
      restarts in data/log-watch-state.json so historical lines don't
      re-fire.

INSTALL — system integration
  --no-sudoers
      Skip dropping /etc/sudoers.d/llm-systems-agent. Set this if the agent
      will not run perf controller or llama.cpp systemctl actions. Linux
      only.

  --no-service
      Skip enabling the systemd unit (Linux) / launchd plist (macOS).
      Useful for staged installs where you want to run the agent manually
      first.

  --no-start
      Install + enable the systemd unit / launchd plist as usual, but do
      not start it. The operator (or the universal installer) decides
      when to start. Linux + macOS.

  --install-perf-units
      Drop example performance.service + powersave.service into
      /etc/systemd/system/. Refuses to overwrite existing units (preserves
      tuned hosts). Linux only. The example units only set the CPU
      governor; tune for your hardware via 'sudo systemctl edit performance'.

  --force-overwrite-perf-units
      Allow --install-perf-units to replace existing units. Backs up the
      originals to <unit>.bak.<epoch_seconds> first.

INSTALL — prompts
  --no-prereq-install
      If prerequisites (python3, venv, ensurepip, visudo, systemctl) are
      missing, exit 1 with the suggested apt/dnf/brew command instead of
      offering to install them.

  -y, --yes
      Auto-confirm the uninstall destruction prompt. Does NOT cover the
      prereq-install consent prompt — system-package installs always
      require interactive 'y' to be typed.

UPDATE
  --update
      Update the installed agent to the latest version:
        1. Download the agent/ subdir of the upstream repo as a tarball
           and atomically replace <INSTALL_DIR>/src/agent. Anything else
           in <INSTALL_DIR>/src is scrubbed (no .git/, no toplevel files).
        2. Redeploy llm-systems-agent.py + buffered_metric_client.py
        3. Refresh venv against the latest requirements.txt
        4. Append any NEW top-level keys from agent_config.yaml.example
           to the live agent_config.yaml (commented, for review).
        5. Refresh the systemd unit / launchd plist
        6. Restart the service

      Existing agent_config.yaml values are never overwritten. Token
      cache + registry entry on the manager survive untouched. The
      upstream URL is hardcoded — see source for the constant — to
      prevent install-time flag tampering from establishing a persistent
      code-execution channel.

UNINSTALL
  --uninstall
      Stop + disable the systemd service / launchd plist, remove the unit
      file, sudoers snippet, install directory, and rotated agent log
      files. Does NOT delete the agent's entry from the manager's registry
      — remove that from the Admin tab → Agents if you want a clean slate
      there too.

GENERAL
  -h, --help
      Show this message and exit.

ADVANCED (rarely needed; safe to ignore in normal operation)

  --no-pull
      Skip the upstream fetch in --update — redeploy from whatever is
      already in <INSTALL_DIR>/src/agent. Useful when iterating on
      local changes staged into src/agent by hand. The 99% case is
      to leave this off.

  --from-self-update
      Internal flag used by the agent's /agent/self-update endpoint
      when an operator clicks Update in the admin tab. Implies:
        --update + --skip-service-refresh + --skip-service-restart
      Both skips are because the agent (running as a regular user)
      doesn't have sudoers for `tee /etc/systemd/...` or `systemctl
      restart llm-systems-agent`. Instead the agent SIGTERMs itself
      after the install; systemd Restart=always brings the new code up.
      Operators should use plain --update (or --no-pull); this flag
      is only intended for the agent path.

  --skip-service-refresh
      Skip rewriting /etc/systemd/system/llm-systems-agent.service
      (Linux) or the launchd plist (macOS). Useful when running under
      a restricted shell lacking sudoers for tee, or when you've
      manually customized the unit and want code-only updates.

  --skip-service-restart
      Skip the final `systemctl restart` / `launchctl load -w`.
      Useful when the caller will restart the service some other way.

EXAMPLES
  Install with auto-detected role (probes the host):
      ./install.sh

  Linux llama.cpp host with perf controller, on a fresh box that has no
  performance/powersave units yet:
      ./install.sh --role llama_host --enable-llama --enable-perf \
                   --install-perf-units

  Server running LM Studio:
      ./install.sh --role lms_host --enable-lms

  Uninstall without prompts:
      ./install.sh --uninstall --yes
EOF
}

DO_UPDATE=false       # --update mode: redeploy code + restart; keep config
NO_PULL=false         # --no-pull: skip the upstream tarball fetch in --update.
                       # Default behavior is to download agent/ from upstream
                       # and atomically replace <INSTALL_DIR>/src/agent before
                       # redeploying. Use --no-pull when iterating on local
                       # changes staged into src/agent by hand.
FROM_SELF_UPDATE=false # set when install.sh is invoked by the agent's
                       # /agent/self-update endpoint. Implies --update +
                       # --skip-service-refresh + --skip-service-restart.
                       # The agent SIGTERMs itself after the install; systemd
                       # Restart=always brings the new code up.
SKIP_SERVICE_REFRESH=false   # don't rewrite the systemd unit / launchd plist
SKIP_SERVICE_RESTART=false   # don't issue systemctl restart at end of update

_need_arg() {
  if [[ -z "${2:-}" || "${2:-}" == --* ]]; then
    echo "ERROR: $1 requires a value" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)         _need_arg "$1" "${2:-}"; USER_ARG="$2"; shift 2 ;;
    --install-dir)  _need_arg "$1" "${2:-}"; INSTALL_DIR="$2"; INSTALL_DIR_EXPLICIT=true; shift 2 ;;
    --hostname)     _need_arg "$1" "${2:-}"; HOSTNAME_OVERRIDE="$2"; shift 2 ;;
    --description)  _need_arg "$1" "${2:-}"; DESCRIPTION_OVERRIDE="$2"; shift 2 ;;
    --manager-url)  _need_arg "$1" "${2:-}"; MANAGER_URL="$2"; MANAGER_URL_EXPLICIT=true; shift 2 ;;
    --alarm-engine-url) _need_arg "$1" "${2:-}"; ALARM_ENGINE_URL="$2"; ALARM_ENGINE_URL_EXPLICIT=true; shift 2 ;;
    --role)         _need_arg "$1" "${2:-}"; ROLE="$2"; shift 2 ;;
    --enable-perf)  ENABLE_PERF=true;  PERF_FLAG_EXPLICIT=true; shift ;;
    --no-perf)      ENABLE_PERF=false; PERF_FLAG_EXPLICIT=true; shift ;;
    --enable-llama) ENABLE_LLAMA=true;     LLAMA_FLAG_EXPLICIT=true;    shift ;;
    --no-llama)     ENABLE_LLAMA=false;    LLAMA_FLAG_EXPLICIT=true;    shift ;;
    --install-llama)     INSTALL_LLAMA=true; shift ;;
    --install-llama=*)   INSTALL_LLAMA=true; INSTALL_LLAMA_METHOD="${1#*=}"; shift ;;
    --llama-backend)     _need_arg "$1" "${2:-}"; INSTALL_LLAMA_BACKEND="$2"; shift 2 ;;
    --enable-lms)   ENABLE_LMS=true;       LMS_FLAG_EXPLICIT=true;      shift ;;
    --no-lms)       ENABLE_LMS=false;      LMS_FLAG_EXPLICIT=true;      shift ;;
    --enable-openclaw) ENABLE_OPENCLAW=true; OPENCLAW_FLAG_EXPLICIT=true; shift ;;
    --no-openclaw)  ENABLE_OPENCLAW=false; OPENCLAW_FLAG_EXPLICIT=true; shift ;;
    --enable-imggen)   ENABLE_IMGGEN=true;   IMGGEN_FLAG_EXPLICIT=true;   shift ;;
    --no-imggen)       ENABLE_IMGGEN=false;  IMGGEN_FLAG_EXPLICIT=true;   shift ;;
    --enable-monitor-manager) ENABLE_MONITOR_MANAGER=true;  MONITOR_MANAGER_FLAG_EXPLICIT=true; shift ;;
    --no-monitor-manager)     ENABLE_MONITOR_MANAGER=false; MONITOR_MANAGER_FLAG_EXPLICIT=true; shift ;;
    --enable-monitor-alarm)   ENABLE_MONITOR_ALARM=true;    MONITOR_ALARM_FLAG_EXPLICIT=true;   shift ;;
    --no-monitor-alarm)       ENABLE_MONITOR_ALARM=false;   MONITOR_ALARM_FLAG_EXPLICIT=true;   shift ;;
    --enable-monitor-influxdb-disk) ENABLE_MONITOR_INFLUXDB_DISK=true;  MONITOR_INFLUXDB_DISK_FLAG_EXPLICIT=true; shift ;;
    --no-monitor-influxdb-disk)     ENABLE_MONITOR_INFLUXDB_DISK=false; MONITOR_INFLUXDB_DISK_FLAG_EXPLICIT=true; shift ;;
    --no-sudoers)   SKIP_SUDOERS=true; shift ;;
    --no-service)   SKIP_SERVICE=true; shift ;;
    --no-start)     SKIP_START=true;   shift ;;
    -y|--yes)       ASSUME_YES=true; shift ;;
    --no-prereq-install) SKIP_PREREQ_INSTALL=true; shift ;;
    --install-perf-units) INSTALL_PERF_UNITS=true; shift ;;
    --force-overwrite-perf-units) FORCE_OVERWRITE_PERF=true; shift ;;
    --uninstall)    DO_UNINSTALL=true; shift ;;
    --update)       DO_UPDATE=true; shift ;;
    --no-pull)      NO_PULL=true; shift ;;
    --from-self-update) DO_UPDATE=true; FROM_SELF_UPDATE=true; SKIP_SERVICE_REFRESH=true; SKIP_SERVICE_RESTART=true; shift ;;
    --skip-service-refresh) SKIP_SERVICE_REFRESH=true; shift ;;
    --skip-service-restart) SKIP_SERVICE_RESTART=true; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Validate --role against the set the rest of the system understands.
# `auto` is included because the auto-detect path resolves it to one of
# the explicit roles before registration.
case "$ROLE" in
  auto|llama_host|lms_host|mixed|system_only) ;;
  *)
    echo "ERROR: --role must be one of: auto, llama_host, lms_host, mixed, system_only (got '$ROLE')" >&2
    exit 2
    ;;
esac

# Mutually exclusive top-level modes
if $DO_UNINSTALL && $DO_UPDATE; then
  echo "ERROR: --uninstall and --update are mutually exclusive" >&2
  exit 2
fi

OS_KERNEL="$(uname -s)"
case "$OS_KERNEL" in
  Linux)  AGENT_OS="linux" ;;
  Darwin) AGENT_OS="macos" ;;
  *) echo "Unsupported OS: $OS_KERNEL" >&2; exit 1 ;;
esac

# Colorize OK / WARN / ERR markers when stdout is a TTY. Falls back to
# plain glyphs in pipes / log captures so journalctl stays readable.
if [[ -t 1 ]]; then
  _C_GREEN=$'\033[0;32m'; _C_YELLOW=$'\033[0;33m'; _C_RED=$'\033[0;31m'
  _C_DIM=$'\033[2m';      _C_OFF=$'\033[0m'
else
  _C_GREEN=""; _C_YELLOW=""; _C_RED=""; _C_DIM=""; _C_OFF=""
fi
_ok()      { echo "  ${_C_GREEN}✓${_C_OFF} $*"; }
_warn()    { echo "  ${_C_YELLOW}⚠${_C_OFF} $*"; }
_err()     { echo "  ${_C_RED}✗${_C_OFF} $*" >&2; }
_section() { echo; echo "── $* ──────────────────────────────────────────────────"; }

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMPL_DIR="$SRC_DIR/install"

if $INSTALL_DIR_EXPLICIT; then
  INSTALL_DIR="${INSTALL_DIR%/}"
  if [[ "$(basename "$INSTALL_DIR")" != "llm-systems-agent" ]]; then
    INSTALL_DIR="$INSTALL_DIR/llm-systems-agent"
  fi
elif [[ "$AGENT_OS" == "macos" ]]; then
  # macOS has no sudo escalation here ($SUDO is empty) and the run user can't
  # write /opt, so default the install dir under the run user's home (#160).
  _mac_home="$(eval echo "~$USER_ARG" 2>/dev/null || true)"
  [[ -z "$_mac_home" || "$_mac_home" == "~$USER_ARG" ]] && _mac_home="/Users/$USER_ARG"
  INSTALL_DIR="$_mac_home/llm-systems-agent"
fi

# Populate the global _required_install_files from the CURRENT $SRC_DIR/$TMPL_DIR.
# Called for the initial preflight and again in --update after SRC_DIR is
# re-pointed at the fetched tree, so the post-fetch check sees the right paths.
_compute_required_install_files() {
  _required_install_files=(
    "$SRC_DIR/llm-systems-agent.py"
    "$SRC_DIR/buffered_metric_client.py"
    "$SRC_DIR/_utils.py"
    "$SRC_DIR/_best_effort.py"
    "$SRC_DIR/agent_context.py"
    "$SRC_DIR/stream_pool.py"
    "$SRC_DIR/unified_config_reader.py"
    "$SRC_DIR/agent_config.yaml.example"
    "$TMPL_DIR/requirements.txt"
    "$TMPL_DIR/requirements-monitor.txt"
    "$SRC_DIR/collectors/__init__.py"
    "$SRC_DIR/collectors/_shared.py"
    "$SRC_DIR/collectors/gpu.py"
    "$SRC_DIR/collectors/ups.py"
    "$SRC_DIR/collectors/liquidctl.py"
    "$SRC_DIR/collectors/system.py"
    "$SRC_DIR/providers/__init__.py"
    "$SRC_DIR/providers/lms.py"
    "$SRC_DIR/providers/llama.py"
    "$SRC_DIR/providers/llama_sse.py"
    "$SRC_DIR/providers/terminal.py"
  )
  case "$(uname -s)" in
    Linux)
      _required_install_files+=("$TMPL_DIR/llm-systems-agent.service.tmpl")
      _required_install_files+=("$TMPL_DIR/llm-systems-agent.sudoers.tmpl")
      ;;
    Darwin)
      _required_install_files+=("$TMPL_DIR/com.llm-systems-agent.plist.tmpl")
      ;;
  esac
}
_compute_required_install_files
_missing_install_files=()
for _f in "${_required_install_files[@]}"; do
  # `-s` (file exists AND has non-zero size) so a zero-byte half-written
  # .py from a previous failed cp doesn't slip past the preflight.
  [[ -s "$_f" ]] || _missing_install_files+=("$_f")
done
if (( ${#_missing_install_files[@]} > 0 )); then
  echo "ERROR: required source files are missing or empty — refusing to install:" >&2
  for _f in "${_missing_install_files[@]}"; do
    echo "         - $_f" >&2
  done
  echo "       Re-clone the agent/ directory or extract the full tarball, then re-run." >&2
  exit 1
fi
unset _required_install_files _missing_install_files _f

# Need sudo for /opt + sudoers + systemctl on Linux
SUDO=""
if [[ "$AGENT_OS" == "linux" && $EUID -ne 0 ]]; then
  SUDO="sudo"
fi

# Self-update path runs as the agent user — no TTY, no sudo password
# prompt available, and the operations it needs (cp into INSTALL_DIR,
# chown of files the agent user already owns) don't actually require
# root. The two things that DO need root (systemd unit refresh + service
# restart) are already skipped via --skip-service-refresh /
# --skip-service-restart bundled into --from-self-update. So drop sudo
# entirely in this path; sudoers only grants systemctl + reload, never
# cp/chown, which is why the agent's sudo call was prompting for a
# password and failing.
if $FROM_SELF_UPDATE; then
  SUDO=""
fi

# Privilege preflight: every privileged step (useradd, /opt, systemd unit,
# sudoers) needs root. $SUDO is non-empty only for a Linux non-root normal
# install — verify that user can actually escalate, else fail fast with
# actionable guidance instead of a cryptic mid-flow sudo error.
if [[ -n "$SUDO" ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    _err "Agent install needs root, but 'sudo' is not installed."
    _err "  Re-run as root: open a root shell ('su -') and run this installer again."
    exit 1
  fi
  if ! sudo -v; then
    _err "Agent install needs root, but '$(id -un)' can't escalate via sudo"
    _err "  (not in the sudoers file, sudo auth failed, or no TTY for a password prompt)."
    _err "  Re-run as root, or have an administrator grant '$(id -un)' sudo access."
    exit 1
  fi
fi

# _run_as USER CMD ARGS... — run CMD as USER. Runs directly when already
# that user, else `sudo -u USER`, else `runuser -u USER` when sudo is
# absent (root-without-sudo hosts). Avoids the `$SUDO -u USER` pattern,
# which expands to a bare leading `-u` on macOS where $SUDO is empty.
_run_as() {
  local target="$1"; shift
  if [[ "$(id -un)" == "$target" ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$target" "$@"
  else
    runuser -u "$target" -- "$@"
  fi
}

# Filter pip's harmless `cache entry deserialization failed` warnings from
# stderr while preserving the wrapped command's exit status. Pip emits these
# when an older HTTP-cache entry can't be decoded; they survive --quiet and
# clutter the installer output.
_pip_filter() {
  "$@" 2> >(grep -v 'cache entry deserialization failed' >&2)
}

# _yaml_scalar FILE KEY
#   Naive top-level YAML scalar reader. Handles  KEY: value  with optional
#   single/double quotes. Doesn't try to parse nested structure — agent_config
#   keeps MANAGER_URL + TOKEN at the top level so this is enough.
_yaml_scalar() {
  local file="$1" key="$2"
  grep -E "^${key}:" "$file" 2>/dev/null \
    | head -1 \
    | sed -E "s/^${key}:[[:space:]]*//; s/^['\"]//; s/['\"]$//; s/[[:space:]]*\$//"
}

# _ensure_hf_cli USER HOME
#   Install the HuggingFace 'hf' CLI into the agent venv and symlink it to
#   ~/.local/bin/hf — the path the agent resolves for llama model downloads.
_ensure_hf_cli() {
  local user="$1" home="$2"
  [[ "$AGENT_OS" == "linux" ]] || return 0
  local pip="$INSTALL_DIR/venv/bin/pip"
  local venv_hf="$INSTALL_DIR/venv/bin/hf"
  local link_dir="$home/.local/bin" link="$home/.local/bin/hf"
  [[ -x "$pip" ]] || return 0
  _section "HuggingFace CLI (llama model downloads)"
  if ! _pip_filter _run_as "$user" "$pip" install --quiet --no-cache-dir -U huggingface_hub; then
    _warn "huggingface_hub install failed — 'hf' unavailable; model downloads will error until it's installed"
    return 0
  fi
  if [[ ! -x "$venv_hf" ]]; then
    _warn "huggingface_hub installed but no 'hf' entrypoint at $venv_hf — model downloads unavailable"
    return 0
  fi
  if [[ -e "$link" && ! -L "$link" ]]; then
    _ok "hf installed; existing $link left untouched"
    return 0
  fi
  _run_as "$user" mkdir -p "$link_dir" 2>/dev/null || true
  if _run_as "$user" ln -sfn "$venv_hf" "$link" 2>/dev/null; then
    _ok "hf available at $link"
  else
    _warn "could not symlink $link → $venv_hf; set HF_CLI_PATH=$venv_hf in agent_config.yaml"
  fi
}

# _fetch_agent_into DEST USER
#   Download agent/ as a tarball from the manager (GET /api/agent-tarball,
#   bearer-auth with the agent's registry token), extract only the agent/
#   subdir, and atomically replace $DEST/agent. Wipes everything else in
#   $DEST so the only thing left under it is agent/. No .git, no stale
#   toplevel files. Owner = USER. Returns rc=0 on success.
_fetch_agent_into() {
  local dest="$1" user="$2"
  local group; group="$(id -gn "$user" 2>/dev/null || echo "$user")"
  local cfg="$INSTALL_DIR/agent_config.yaml"

  if [[ ! -f "$cfg" ]]; then
    echo "  ⚠ $cfg missing — cannot determine manager URL/token for fetch" >&2
    return 1
  fi
  local mgr_url tok_file tok
  mgr_url="$(_yaml_scalar "$cfg" MANAGER_URL)"
  if [[ -z "$mgr_url" ]]; then
    echo "  ⚠ MANAGER_URL missing in $cfg" >&2
    return 1
  fi
  # TOKEN_FILE in agent_config.yaml may be blank/commented — default to
  # <INSTALL_DIR>/data/token (matches the agent's own resolution logic).
  tok_file="$(_yaml_scalar "$cfg" TOKEN_FILE)"
  [[ -z "$tok_file" ]] && tok_file="$INSTALL_DIR/data/token"
  if [[ ! -r "$tok_file" ]]; then
    echo "  ⚠ token file not readable: $tok_file" >&2
    echo "    (agent not yet approved by the manager? check Admin tab)" >&2
    return 1
  fi
  tok="$(tr -d '[:space:]' < "$tok_file")"
  if [[ -z "$tok" ]]; then
    echo "  ⚠ token file is empty: $tok_file" >&2
    return 1
  fi

  local tmpdir; tmpdir="$(mktemp -d)"
  echo "  Downloading agent/ from $mgr_url/api/agent-tarball"
  if ! curl -fsSL -H "Authorization: Bearer $tok" \
            "$mgr_url/api/agent-tarball" -o "$tmpdir/agent.tar.gz"; then
    echo "  ⚠ download failed from $mgr_url/api/agent-tarball" >&2
    rm -rf "$tmpdir"
    return 1
  fi

  mkdir -p "$tmpdir/extract"
  if ! tar -xzf "$tmpdir/agent.tar.gz" -C "$tmpdir/extract"; then
    echo "  ⚠ tar extract failed" >&2
    rm -rf "$tmpdir"
    return 1
  fi
  if [[ ! -d "$tmpdir/extract/agent" ]]; then
    echo "  ⚠ tarball did not contain agent/" >&2
    rm -rf "$tmpdir"
    return 1
  fi

  $SUDO mkdir -p "$dest"
  $SUDO rm -rf "$dest/agent.new"
  $SUDO mv "$tmpdir/extract/agent" "$dest/agent.new"
  $SUDO rm -rf "$dest/agent"
  $SUDO mv "$dest/agent.new" "$dest/agent"
  # Scrub anything in $dest that isn't agent/ — clears .git from prior
  # git-based installs, plus any toplevel README/LICENSE leftovers.
  $SUDO find "$dest" -mindepth 1 -maxdepth 1 ! -name agent -exec rm -rf {} +
  $SUDO chown -R "$user:$group" "$dest"

  rm -rf "$tmpdir"
}

# ──────────────────────────────────────────────────────────────────────────
# Uninstall mode — runs and exits before any install logic
# ──────────────────────────────────────────────────────────────────────────
if $DO_UNINSTALL; then
  # Discover the actual install dir from the systemd unit / launchd plist
  # when --install-dir wasn't explicitly passed. Otherwise an uninstall on
  # a host that used a custom --install-dir would silently target the
  # default /opt/llm-systems-agent (which doesn't exist), report success,
  # and leave the real install behind.
  if ! $INSTALL_DIR_EXPLICIT; then
    DISCOVERED=""
    if [[ "$AGENT_OS" == "linux" && -f /etc/systemd/system/llm-systems-agent.service ]]; then
      DISCOVERED="$(awk -F= '/^WorkingDirectory=/{print $2; exit}' \
                         /etc/systemd/system/llm-systems-agent.service 2>/dev/null || true)"
    elif [[ "$AGENT_OS" == "macos" && -f "$HOME/Library/LaunchAgents/com.llm-systems-agent.plist" ]]; then
      # WorkingDirectory key — the value is the line after it. grep -A1.
      DISCOVERED="$(awk '/<key>WorkingDirectory<\/key>/{getline; gsub(/.*<string>|<\/string>.*/,""); print; exit}' \
                         "$HOME/Library/LaunchAgents/com.llm-systems-agent.plist" 2>/dev/null || true)"
    fi
    if [[ -n "$DISCOVERED" && "$DISCOVERED" != "$INSTALL_DIR" ]]; then
      echo "  ⓘ found existing install at $DISCOVERED (from service definition);"
      echo "    using that instead of the default $INSTALL_DIR"
      INSTALL_DIR="$DISCOVERED"
    fi
  fi

  echo "── LLM Systems Agent UNINSTALLER ───────────────────────────────────────"
  echo "  OS:           $AGENT_OS"
  echo "  install-dir:  $INSTALL_DIR"
  echo
  echo "  This will:"
  if [[ "$AGENT_OS" == "linux" ]]; then
    echo "    • systemctl disable --now llm-systems-agent.service"
    echo "    • remove /etc/systemd/system/llm-systems-agent.service"
    echo "    • remove /etc/sudoers.d/llm-systems-agent"
  else
    echo "    • launchctl unload ~/Library/LaunchAgents/com.llm-systems-agent.plist"
    echo "    • remove ~/Library/LaunchAgents/com.llm-systems-agent.plist"
  fi
  echo "    • rm -rf $INSTALL_DIR  (this WIPES the token cache, buffer, logs)"
  echo "    • leave the manager's registry entry alone — delete from Admin tab"
  echo "      if you want a clean slate there too"
  echo "─────────────────────────────────────────────────────────────────────────"
  echo

  if ! $ASSUME_YES; then
    read -rp "  Proceed with uninstall? [y/N] " REPLY
    case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
      y|yes) ;;
      *)
        echo "  Aborted."
        exit 1
        ;;
    esac
  fi

  echo
  echo "  Stopping + removing service…"
  if [[ "$AGENT_OS" == "linux" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      $SUDO systemctl disable --now llm-systems-agent.service 2>/dev/null || true
      $SUDO systemctl reset-failed llm-systems-agent.service 2>/dev/null || true
    fi
    if [[ -f /etc/systemd/system/llm-systems-agent.service ]]; then
      $SUDO rm -f /etc/systemd/system/llm-systems-agent.service
      $SUDO systemctl daemon-reload 2>/dev/null || true
      echo "    ✓ removed /etc/systemd/system/llm-systems-agent.service"
    fi
    if [[ -f /etc/sudoers.d/llm-systems-agent ]]; then
      $SUDO rm -f /etc/sudoers.d/llm-systems-agent
      echo "    ✓ removed /etc/sudoers.d/llm-systems-agent"
    fi
  else
    PLIST="$HOME/Library/LaunchAgents/com.llm-systems-agent.plist"
    if [[ -f "$PLIST" ]]; then
      launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
      echo "    ✓ removed $PLIST"
    fi
  fi

  if [[ -d "$INSTALL_DIR" ]]; then
    echo "  Removing $INSTALL_DIR…"
    $SUDO rm -rf "$INSTALL_DIR"
    echo "    ✓ removed $INSTALL_DIR"
  else
    echo "  $INSTALL_DIR does not exist — skipping"
  fi

  # Optional: remove agent log files. Two locations to sweep:
  #   Linux: /var/log/llm-systems-manager/llm-systems-agent.log*
  #   macOS: ~/Library/Logs/llm-systems-agent/   (whole project subdir)
  if [[ "$AGENT_OS" == "linux" ]]; then
    LOG_GLOB="/var/log/llm-systems-manager/llm-systems-agent.log*"
    # shellcheck disable=SC2086
    if compgen -G $LOG_GLOB >/dev/null 2>&1; then
      $SUDO rm -f $LOG_GLOB
      echo "    ✓ removed agent log files under /var/log/llm-systems-manager/"
    fi
  else
    LOG_DIR="$HOME/Library/Logs/llm-systems-agent"
    if [[ -d "$LOG_DIR" ]]; then
      rm -rf "$LOG_DIR"
      echo "    ✓ removed agent log dir $LOG_DIR"
    fi
    # Also sweep the legacy top-level files from earlier installer
    # versions that put logs at ~/Library/Logs/llm-systems-agent.*.log
    rm -f "$HOME/Library/Logs/llm-systems-agent.log"* \
          "$HOME/Library/Logs/llm-systems-agent.stdout.log"* \
          "$HOME/Library/Logs/llm-systems-agent.stderr.log"* 2>/dev/null || true
  fi

  echo
  echo "── Uninstall complete ───────────────────────────────────────────────────"
  echo "  Reminder: the agent's entry in the manager's registry"
  echo "  ($MANAGER_URL → Admin tab → Agents) is NOT deleted by this script."
  echo "─────────────────────────────────────────────────────────────────────────"
  exit 0
fi

# ──────────────────────────────────────────────────────────────────────────
# Update mode — redeploy files, refresh venv, merge new config keys, restart
# ──────────────────────────────────────────────────────────────────────────
if $DO_UPDATE; then
  echo "── LLM Systems Agent UPDATER ───────────────────────────────────────────"
  echo "  install-dir: $INSTALL_DIR"
  echo "  OS:          $AGENT_OS"
  if $FROM_SELF_UPDATE; then
    echo "  mode:        self-update (invoked by /agent/self-update)"
    echo "  skip:        systemd unit refresh, service restart"
  fi
  echo

  if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "ERROR: $INSTALL_DIR does not exist — nothing to update." >&2
    echo "       Run the installer without --update to do a fresh install." >&2
    exit 1
  fi
  if [[ ! -f "$INSTALL_DIR/agent_config.yaml" ]]; then
    echo "ERROR: $INSTALL_DIR/agent_config.yaml is missing — refusing to update an" >&2
    echo "       incomplete installation. Run the installer without --update." >&2
    exit 1
  fi

  # ── 0. Fetch the latest agent/ from upstream. The self-update source
  # tree lives at $INSTALL_DIR/src — fresh-install creates it (see
  # install-mode block below). The tarball-extract path replaces
  # src/agent atomically and scrubs everything else under src/.
  REPO_DIR_FOR_UPDATE="$INSTALL_DIR/src"

  # Detect run-as user from the existing systemd unit (Linux) or fall back
  # to the dir owner. macOS keeps the plist under the user's LaunchAgents
  # so the launching user is implicit. (Resolved BEFORE the /src/
  # bootstrap and FROM_SELF_UPDATE git-pull below so chown/_run_as have
  # USER_ARG + USER_GROUP available.)
  if [[ "$AGENT_OS" == "linux" && -f /etc/systemd/system/llm-systems-agent.service ]]; then
    EXISTING_USER="$(awk -F= '/^User=/{print $2; exit}' /etc/systemd/system/llm-systems-agent.service)"
    [[ -n "$EXISTING_USER" ]] && USER_ARG="$EXISTING_USER"
  else
    USER_ARG="$(stat -c %U "$INSTALL_DIR" 2>/dev/null || stat -f %Su "$INSTALL_DIR" 2>/dev/null || echo "$USER_ARG")"
  fi
  echo "  run-as user: $USER_ARG (preserved from existing install)"
  echo

  # Resolve user's primary group (linux: usually matches user; macOS:
  # typically 'staff'). `id -gn` is portable across both.
  USER_GROUP="$(id -gn "$USER_ARG" 2>/dev/null || true)"
  if [[ -z "$USER_GROUP" ]]; then
    echo "ERROR: could not resolve primary group for user '$USER_ARG'" >&2
    exit 1
  fi
  # Same home-dir resolution as the install path (needed for the
  # plist refresh on macOS).
  USER_HOME="$(eval echo "~$USER_ARG" 2>/dev/null || true)"
  if [[ -z "$USER_HOME" || "$USER_HOME" == "~$USER_ARG" ]]; then
    if command -v getent >/dev/null 2>&1; then
      USER_HOME="$(getent passwd "$USER_ARG" 2>/dev/null | cut -d: -f6 || true)"
    elif command -v dscl >/dev/null 2>&1; then
      USER_HOME="$(dscl . -read "/Users/$USER_ARG" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    fi
  fi
  if [[ -z "$USER_HOME" ]]; then
    echo "ERROR: could not resolve home directory for user '$USER_ARG'" >&2
    exit 1
  fi

  # Fetch latest agent/ from upstream (default). --no-pull skips this
  # and deploys whatever is already in $REPO_DIR_FOR_UPDATE/agent.
  # The tarball-extract path replaces $REPO_DIR_FOR_UPDATE/agent
  # atomically and scrubs everything else under $REPO_DIR_FOR_UPDATE,
  # so a stale .git/ or toplevel manager files from a prior install
  # get cleaned up here.
  if ! $NO_PULL; then
    echo
    echo "── Fetching latest agent/ from upstream ────────────────────────────────"
    if _fetch_agent_into "$REPO_DIR_FOR_UPDATE" "$USER_ARG"; then
      echo "  ✓ $REPO_DIR_FOR_UPDATE/agent refreshed (only agent/ retained)"
    else
      echo "  ⚠ fetch failed — refusing to deploy stale code." >&2
      echo "    To redeploy WITHOUT fetching, re-run with --no-pull:" >&2
      echo "      bash install.sh --update --no-pull" >&2
      exit 1
    fi
  elif [[ ! -d "$REPO_DIR_FOR_UPDATE/agent" ]]; then
    echo "ERROR: --no-pull set but $REPO_DIR_FOR_UPDATE/agent doesn't exist." >&2
    echo "       Drop --no-pull so the installer can fetch the latest agent/," >&2
    echo "       or stage the agent/ tree manually before retrying." >&2
    exit 1
  else
    echo "ⓘ --no-pull set; deploying from $REPO_DIR_FOR_UPDATE/agent without fetching"
  fi
  # Re-point SRC_DIR + TMPL_DIR at the (possibly newly-fetched) tree so
  # the rest of --update uses what's in src/, not the operator's CWD.
  SRC_DIR="$REPO_DIR_FOR_UPDATE/agent"
  TMPL_DIR="$SRC_DIR/install"

  # 1. Refresh agent code. Packages copied BEFORE top-level .py files so
  # a partial-cp failure leaves the OLD working agent.py in place.
  _section "Updating agent code"
  # Rebuild the manifest against the re-pointed SRC_DIR/TMPL_DIR (it was unset
  # after the initial preflight) and re-check the fetched tree. Referencing the
  # unset array here was an unbound-variable abort on macOS bash 3.2 (#200).
  _compute_required_install_files
  for _f in "${_required_install_files[@]}"; do
    [[ -e "$_f" ]] || { echo "ERROR: post-fetch required file missing: $_f" >&2; exit 1; }
  done
  for _pkg in collectors providers; do
    [[ -d "$SRC_DIR/$_pkg" ]] || { echo "ERROR: $SRC_DIR/$_pkg missing — refusing to wipe $INSTALL_DIR/$_pkg" >&2; exit 1; }
    $SUDO rm -rf "$INSTALL_DIR/$_pkg"
    $SUDO cp -r "$SRC_DIR/$_pkg" "$INSTALL_DIR/$_pkg"
    $SUDO chown -R "$USER_ARG:$USER_GROUP" "$INSTALL_DIR/$_pkg"
  done
  $SUDO cp "$SRC_DIR/llm-systems-agent.py"        "$INSTALL_DIR/llm-systems-agent.py"
  $SUDO cp "$SRC_DIR/buffered_metric_client.py"   "$INSTALL_DIR/buffered_metric_client.py"
  $SUDO cp "$SRC_DIR/_utils.py"                   "$INSTALL_DIR/_utils.py"
  $SUDO cp "$SRC_DIR/_best_effort.py"             "$INSTALL_DIR/_best_effort.py"
  $SUDO cp "$SRC_DIR/agent_context.py"            "$INSTALL_DIR/agent_context.py"
  $SUDO cp "$SRC_DIR/stream_pool.py"              "$INSTALL_DIR/stream_pool.py"
  $SUDO cp "$SRC_DIR/unified_config_reader.py"    "$INSTALL_DIR/unified_config_reader.py"
  $SUDO chown "$USER_ARG:$USER_GROUP" \
    "$INSTALL_DIR/llm-systems-agent.py" \
    "$INSTALL_DIR/buffered_metric_client.py" \
    "$INSTALL_DIR/_utils.py" \
    "$INSTALL_DIR/_best_effort.py" \
    "$INSTALL_DIR/agent_context.py" \
    "$INSTALL_DIR/stream_pool.py" \
    "$INSTALL_DIR/unified_config_reader.py"
  _ok "agent code refreshed"

  # 2. Refresh venv against current requirements (no-op if already satisfied)
  echo
  echo "── Refreshing venv ──────────────────────────────────────────────────────"
  if [[ ! -x "$INSTALL_DIR/venv/bin/pip" ]]; then
    echo "  venv missing or broken — rebuilding"
    $SUDO rm -rf "$INSTALL_DIR/venv"
    # Prefer the python that built the existing venv (its symlink targets
    # are still in $INSTALL_DIR/venv/bin/python3 if even a stale venv
    # exists); else fall back to PATH.
    if [[ -z "${PYTHON3:-}" ]]; then
      if [[ -L "$INSTALL_DIR/venv/bin/python3" ]]; then
        PYTHON3="$(readlink "$INSTALL_DIR/venv/bin/python3" 2>/dev/null || command -v python3)"
      else
        PYTHON3="$(command -v python3)"
      fi
    fi
    _run_as "$USER_ARG" "$PYTHON3" -m venv "$INSTALL_DIR/venv"
  fi
  _pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
  _pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$TMPL_DIR/requirements.txt"
  echo "  ✓ requirements installed"

  _monitor_alarm_on="$(_yaml_scalar "$INSTALL_DIR/agent_config.yaml" MONITOR_ALARM_ENGINE_ENABLED)"
  if [[ "$(printf '%s' "$_monitor_alarm_on" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
    _pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$TMPL_DIR/requirements-monitor.txt"
    echo "  ✓ monitor extras (influxdb-client) installed"
  fi

  _hf_llama_on="$(_yaml_scalar "$INSTALL_DIR/agent_config.yaml" LLAMA_ENABLED)"
  # tr, not ${,,} — macOS bash 3.2 lacks case-modification expansion.
  if [[ "$(printf '%s' "$_hf_llama_on" | tr '[:upper:]' '[:lower:]')" == "true" ]]; then
    _ensure_hf_cli "$USER_ARG" "$USER_HOME"
  fi

  # 3. Reconcile the live config against agent_config.yaml.example.
  #    Goal: end with a live file structurally identical to the example
  #    (same section headers, same key order, same semantic blocks), with
  #    only the *values* reflecting operator overrides. Removed keys land
  #    in a trailing block, commented, with a note explaining why.
  #
  #    Backup of the live config is taken INSIDE the Python heredoc, but
  #    only when the reconcile actually produces different bytes —
  #    otherwise every --update run was leaving a timestamped .bak even
  #    when nothing changed (operators reported piles of identical .bak
  #    files accumulating in INSTALL_DIR).
  echo
  echo "── Reconciling live config with agent_config.yaml.example ──────────────"
  # $SUDO so the reconcile can rewrite the 0600 agent_config.yaml when
  # the agent installer is invoked by a non-root caller (e.g. global
  # update.sh as a regular user that uses sudo per-op). As root,
  # $SUDO is empty and python3 runs directly. The USER_ARG/USER_GROUP
  # tail args let the Python re-chown a newly created .bak (running as
  # root means shutil.copy2 leaves it root-owned).
  $SUDO python3 -u - "$INSTALL_DIR/agent_config.yaml" "$SRC_DIR/agent_config.yaml.example" "$USER_ARG" "$USER_GROUP" <<'PYEOF'
import sys, re

live_path, example_path, user_arg, group_arg = sys.argv[1:5]
KEY_RE = re.compile(r'^(#?)(\s*)([A-Z][A-Z0-9_]*)\s*:(.*)$')

def parse(text):
    """Walk lines; index each top-level key to its block span (start, end).
    A block runs from the key line through any indented continuation lines
    (block style) OR column-0 list items starting with '- ' (compact style)
    — covers both shapes of multi-line YAML values like PROCESS_WATCHLIST.
    Returns (lines, [(key, start, end, commented)] in order)."""
    lines = text.splitlines()
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        m = KEY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        hash_, _ws, key, _rest = m.groups()
        commented = bool(hash_)
        start = i
        i += 1
        while i < n:
            nxt = lines[i]
            if not nxt.strip():
                break
            if KEY_RE.match(nxt):
                break
            if nxt.startswith((' ', '\t', '-')):
                i += 1
                continue
            if re.match(r'^#\s{2,}', nxt) or re.match(r'^#\s*-', nxt):
                i += 1
                continue
            break
        blocks.append((key, start, i, commented))
    return lines, blocks

live_text = open(live_path).read()
live_lines, live_blocks = parse(live_text)
example_lines, example_blocks = parse(open(example_path).read())

live_by_key = {k: (s, e, c) for k, s, e, c in live_blocks}
example_keys = {k for k, *_ in example_blocks}

example_key_at = {start: (key, end, commented)
                  for key, start, end, commented in example_blocks}

# Split a single-line "KEY: value  # inline" into (value, inline). Used to
# transplant the operator's value into the example's line template so inline
# annotations and column alignment survive the merge.
VALUE_RE = re.compile(r'^(?P<lead>#?\s*[A-Z][A-Z0-9_]*\s*:)(?P<val>.*?)(?P<inline>\s+#.*)?$')

def _value_only(line):
    m = VALUE_RE.match(line)
    if not m:
        return None, ''
    return m.group('val').strip(), m.group('inline') or ''

SUBKEY_RE = re.compile(r'^#?\s+([a-z][a-z0-9_]*)\s*:')

def _subkeys(block_lines):
    """Lowercase sub-key names in a block, skipping the KEY: header line."""
    return [m.group(1) for ln in block_lines[1:]
            for m in [SUBKEY_RE.match(ln)] if m]

output = []
added_keys = []
refreshed_subkeys = []
preserved_new_subkeys = []
i, n = 0, len(example_lines)
while i < n:
    if i in example_key_at:
        key, ex_end, ex_commented = example_key_at[i]
        ex_is_multi = (ex_end - i) > 1
        if key in live_by_key:
            ls, le, _ = live_by_key[key]
            live_is_multi = (le - ls) > 1
            if not ex_is_multi and not live_is_multi:
                # Single-line both sides — substitute operator value into the
                # example's line template (keeps inline comments + alignment),
                # and preserve operator's commented/uncommented state.
                live_val, _ = _value_only(live_lines[ls])
                ex_line = example_lines[i]
                ex_val, _ = _value_only(ex_line)
                live_commented = live_lines[ls].lstrip().startswith('#')
                same_commented = live_commented == ex_commented
                if live_val is None or (live_val == ex_val and same_commented):
                    output.append(ex_line)
                else:
                    m = VALUE_RE.match(ex_line)
                    lead = m.group('lead').lstrip('#').lstrip(' ')
                    if live_commented:
                        lead = '# ' + lead
                    inline = m.group('inline') or ''
                    pad_match = re.match(r'^(\s+)', m.group('val') or ' ')
                    pad = pad_match.group(1) if pad_match else ' '
                    output.append(f"{lead}{pad}{live_val}{inline}")
            else:
                live_blk = live_lines[ls:le]
                ex_blk = example_lines[i:ex_end]
                live_untouched = all(
                    (not ln.strip()) or ln.lstrip().startswith('#') for ln in live_blk
                )
                new_subs = [s for s in _subkeys(ex_blk) if s not in _subkeys(live_blk)]
                # Untouched (all-commented) block tracks the example so new
                # sub-keys propagate; an activated block is preserved verbatim.
                if live_untouched:
                    output.extend(ex_blk)
                    if new_subs:
                        refreshed_subkeys.append((key, new_subs))
                else:
                    output.extend(live_blk)
                    if new_subs:
                        # List-style blocks ('- ' items) only get flagged;
                        # mapping blocks get their missing keys appended.
                        is_list = any(ln.lstrip('#').strip().startswith('-')
                                      for ln in live_blk[1:] + ex_blk[1:])
                        if is_list:
                            preserved_new_subkeys.append((key, new_subs))
                        else:
                            output.extend([ln for ln in ex_blk[1:]
                                           for m in [SUBKEY_RE.match(ln)]
                                           if m and m.group(1) in new_subs])
                            refreshed_subkeys.append((key, new_subs))
        else:
            output.extend(example_lines[i:ex_end])
            added_keys.append(key)
        i = ex_end
        continue
    output.append(example_lines[i])
    i += 1

removed_keys = [k for k, *_ in live_blocks if k not in example_keys]
if removed_keys:
    output.append('')
    output.append('# ── Keys not present in agent_config.yaml.example ─────────────────────')
    output.append('# These keys were in your live config but are no longer defined in the')
    output.append('# upstream example. They have been commented out so the agent ignores')
    output.append('# them while preserving your values for reference. If you still need')
    output.append('# one, restore it manually; otherwise it can be deleted.')
    seen = set()
    for k in removed_keys:
        if k in seen:
            continue
        seen.add(k)
        ls, le, _ = live_by_key[k]
        for line in live_lines[ls:le]:
            output.append(line if line.lstrip().startswith('#') else '# ' + line)

# ── LOG_WATCH_RULES re-seeding (idempotent) ───────────────────────────────
# The fresh-install path writes per-host commented stubs into the
# LOG_WATCH_RULES block at first deploy. On --update we re-emit those
# stubs ONLY when the existing block in the live config is still in its
# untouched "every line commented" state — meaning the operator hasn't
# activated or customized it. If they have (any non-comment line in the
# block), leave their value alone.
import os as _os, subprocess as _sp
def _unit_present(unit):
    for d in ('/etc/systemd/system', '/lib/systemd/system', '/usr/lib/systemd/system'):
        if _os.path.isfile(_os.path.join(d, unit)):
            return True
    return False
text_now = '\n'.join(output) + '\n'
# Locate the existing LOG_WATCH_RULES block (commented or live).
lw_re = re.compile(
    r'(?ms)^# LOG_WATCH_RULES:[^\n]*(?:\n#[^\n]*)*'
    r'|^LOG_WATCH_RULES:[^\n]*(?:\n(?:\s+[^\n]*|#\s+[^\n]*))*'
)
mblk = lw_re.search(text_now)
if mblk:
    block = mblk.group(0)
    # "Untouched stub" heuristic: every line in the block starts with '#'
    # (allowing leading whitespace). Any uncommented line → operator has
    # activated/edited it; skip the re-seed.
    is_untouched = all(
        (not ln.strip()) or ln.lstrip().startswith('#')
        for ln in block.splitlines()
    )
    if is_untouched:
        has_mgr    = _unit_present('llm-systems-manager.service')
        has_ae     = _unit_present('llm-systems-alarm-engine.service')
        has_influx = _unit_present('influxdb.service')
        has_llama  = _unit_present('llama_server.service')
        def _rule(name, path, pattern, severity, cooldown, message):
            return (f"#   - name: {name}\n"
                    f"#     path: {path}\n"
                    f"#     pattern: {pattern!r}\n"
                    f"#     severity: {severity}\n"
                    f"#     cooldown_s: {cooldown}\n"
                    f"#     message: {message!r}\n")
        rules = []
        if has_mgr:
            rules.append(_rule("manager-error",
                "/var/log/llm-systems-manager/llm-systems-manager.log",
                r"\[(ERROR|CRITICAL)\]", "critical", 300, "manager log: {line}"))
            rules.append(_rule("manager-traceback",
                "/var/log/llm-systems-manager/llm-systems-manager.log",
                r"^Traceback \(most recent call last\)", "critical", 300,
                "manager traceback: {line}"))
        if has_ae:
            rules.append(_rule("ae-error",
                "/var/log/llm-systems-manager/llm-systems-alarm-engine.log",
                r"\[(ERROR|CRITICAL)\]", "critical", 300, "alarm-engine log: {line}"))
            rules.append(_rule("ae-influx-write-fail",
                "/var/log/llm-systems-manager/llm-systems-alarm-engine.log",
                r"(?i)influx.*(write.*fail|unavailable|circuit.*open)",
                "critical", 600, "alarm-engine: {line}"))
        if has_influx:
            rules.append(_rule("influxdb-error",
                "/var/log/syslog",
                r"(?i)influxd.*?(error|panic|fatal|out of disk)",
                "critical", 300, "influxdb: {line}"))
        if has_llama:
            llama_log_path = "/usr/local/llama-server/llama-server.log"
            rules.append(_rule("llama-oom", llama_log_path,
                r"(?i)cuda out of memory|ggml_cuda.*out of memory|HIP out of memory",
                "critical", 300, "llama-server OOM: {line}"))
            rules.append(_rule("llama-fatal", llama_log_path,
                r"(?i)\bfatal\b|GGML_ASSERT|terminate called",
                "critical", 300, "llama-server fatal: {line}"))
            rules.append(_rule("amdgpu-reset", "/var/log/syslog",
                r"(?i)amdgpu.*(GPU\s+(smu\s+)?mode\d+\s+reset|ring\s+\S+\s+timeout|gpu hang)",
                "critical", 600, "amdgpu reset/hang: {line}"))
        if rules:
            header = (
                "# Installer-seeded log-watch rules for components detected on this\n"
                "# host. To enable: set LOG_WATCH_ENABLED: true above, then uncomment\n"
                "# the LOG_WATCH_RULES: line AND every rule line below it (drop the\n"
                "# leading '# '). Indent is significant — keep the two-space indent\n"
                "# before the leading '-' on each rule, four-space on rule fields.\n"
            )
            new_block = header + "# LOG_WATCH_RULES:\n" + "".join(rules)
            text_now = text_now.replace(block, new_block, 1)
            log_watch_reseeded = True
        else:
            log_watch_reseeded = False
    else:
        log_watch_reseeded = False
else:
    log_watch_reseeded = False

if text_now == live_text:
    # Bytes-equal — no rewrite, no backup. Keeps INSTALL_DIR free of the
    # pile of identical .bak files that --update used to leave behind.
    print(f"  ✓ {live_path} already matches the example — no rewrite, no backup")
else:
    import shutil, pwd, grp, datetime, os
    bak_path = f"{live_path}.{datetime.datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(live_path, bak_path)
    try:
        uid = pwd.getpwnam(user_arg).pw_uid
        gid = grp.getgrnam(group_arg).gr_gid
        os.chown(bak_path, uid, gid)
    except (KeyError, PermissionError) as e:
        print(f"  ⚠ couldn't chown {bak_path} to {user_arg}:{group_arg}: {e}")
    open(live_path, 'w').write(text_now)
    print(f"  ✓ saved backup {bak_path}")
    print(f"  ✓ reconciled {live_path} against {example_path}")

if log_watch_reseeded:
    print( "  ✓ re-seeded LOG_WATCH_RULES stubs (untouched commented block detected)")
if added_keys:
    print("")
    print("  ╔═══ NEW CONFIG KEYS ═══════════════════════════════════════════════╗")
    print(f"  ║ +++ added {len(added_keys)} new key(s) from the upstream example:")
    for k in added_keys:
        print(f"  ║       + {k}")
    print( "  ║ Review them in agent_config.yaml — defaults are off / inert until")
    print( "  ║ you opt in. Edit the file and restart the agent to enable.")
    print( "  ╚═══════════════════════════════════════════════════════════════════╝")
    print("")
if refreshed_subkeys:
    print("")
    print("  ╔═══ NEW CONFIG OPTIONS ════════════════════════════════════════════╗")
    print("  ║ +++ added new option(s) to existing block(s):")
    for key, subs in refreshed_subkeys:
        for s in subs:
            print(f"  ║       + {key}.{s}")
    print( "  ║ Review them in agent_config.yaml — defaults are off / inert until")
    print( "  ║ you opt in. Edit the file and restart the agent to enable.")
    print( "  ╚═══════════════════════════════════════════════════════════════════╝")
    print("")
if preserved_new_subkeys:
    print("")
    print("  ⚠ new option(s) exist in the example for block(s) you've customized —")
    print("    your values were kept; add these manually if you want them:")
    for key, subs in preserved_new_subkeys:
        for s in subs:
            print(f"      + {key}.{s}")
    print("")
if removed_keys:
    print("")
    print(f"  ⚠ commented out {len(removed_keys)} key(s) no longer in the example:")
    for k in removed_keys:
        print(f"      - {k}  (key removed from upstream agent_config.yaml.example)")
    print("")
PYEOF

  # 4. Refresh systemd unit / launchd plist (so unit-level changes —
  #    Restart=always, env tweaks, etc. — take effect)
  if $SKIP_SERVICE_REFRESH; then
    echo
    echo "── Skipping service-definition refresh (--skip-service-refresh) ────────"
    echo "  Self-update path: agent runs as a regular user without sudoers entry"
    echo "  for tee /etc/systemd/system/, so the unit file isn't rewritten. Re-run"
    echo "  install.sh --update interactively to pick up unit-template changes."
  else
    echo
    echo "── Refreshing service definition ───────────────────────────────────────"
    if [[ "$AGENT_OS" == "linux" ]]; then
      UNIT_DEST="/etc/systemd/system/llm-systems-agent.service"
      sed -e "s|\${AGENT_USER}|$USER_ARG|g" \
          -e "s|\${AGENT_GROUP}|$USER_GROUP|g" \
          -e "s|\${AGENT_INSTALL_DIR}|$INSTALL_DIR|g" \
          "$TMPL_DIR/llm-systems-agent.service.tmpl" | $SUDO tee "$UNIT_DEST" >/dev/null
      $SUDO systemctl daemon-reload
      echo "  ✓ unit refreshed: $UNIT_DEST"
    else
      PLIST_DEST="$HOME/Library/LaunchAgents/com.llm-systems-agent.plist"
      sudo -u "$USER_ARG" mkdir -p "$USER_HOME/Library/Logs/llm-systems-agent" 2>/dev/null || \
        mkdir -p "$USER_HOME/Library/Logs/llm-systems-agent"
      sed -e "s|\${AGENT_USER}|$USER_ARG|g" \
          -e "s|\${AGENT_USER_HOME}|$USER_HOME|g" \
          -e "s|\${AGENT_INSTALL_DIR}|$INSTALL_DIR|g" \
          "$TMPL_DIR/com.llm-systems-agent.plist.tmpl" > "$PLIST_DEST"
      echo "  ✓ plist refreshed: $PLIST_DEST"
    fi
  fi
  if $SKIP_SERVICE_RESTART; then
    echo
    echo "── Skipping service restart (--skip-service-restart) ───────────────────"
    echo "  Self-update path: the agent triggers its own SIGTERM after this script"
    echo "  exits; systemd's Restart=always brings the new code up cleanly."
  else
    if [[ "$AGENT_OS" == "linux" ]]; then
      $SUDO systemctl restart llm-systems-agent.service
      echo "  ✓ service restarted"
    else
      launchctl unload "$HOME/Library/LaunchAgents/com.llm-systems-agent.plist" 2>/dev/null || true
      launchctl load -w "$HOME/Library/LaunchAgents/com.llm-systems-agent.plist"
      echo "  ✓ plist reloaded"
    fi
  fi

  # Wipe src/ now that the deploy is done. The next --update re-fetches
  # from the manager; nothing on the agent host needs to persist between
  # updates. --no-pull workflows must re-stage src/agent before each run.
  if [[ -d "$REPO_DIR_FOR_UPDATE" ]]; then
    $SUDO find "$REPO_DIR_FOR_UPDATE" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "  ✓ $REPO_DIR_FOR_UPDATE wiped (re-fetched on next --update)"
  fi

  echo
  echo "── Update complete ─────────────────────────────────────────────────────"
  echo "  agent_config.yaml reconciled against the example — your values were kept"
  echo "  and new keys merged in place. See the notes above; changes apply on restart."
  if [[ "$AGENT_OS" == "linux" ]]; then
    echo "  Tail logs:  journalctl -u llm-systems-agent -f"
  else
    echo "  Tail logs:  tail -f /Users/$USER_ARG/Library/Logs/llm-systems-agent/agent.log"
  fi
  echo "─────────────────────────────────────────────────────────────────────────"
  exit 0
fi

# ──────────────────────────────────────────────────────────────────────────
# Install mode (default)
# ──────────────────────────────────────────────────────────────────────────

# ── Manager URL: prompt + connectivity test ─────────────────────────────────
# The agent is useless without a reachable manager — it would register
# nowhere and buffer metrics into a black hole. Prompt the user (unless
# --manager-url was passed explicitly) and verify reachability before
# touching anything else.

# HTTP probe helper — uses curl if available, falls back to python3.
# Returns the HTTP status code on stdout, or "000" on connection failure.
_probe_http_code() {
  local url="$1" code=""
  if command -v curl >/dev/null 2>&1; then
    # `-w "%{http_code}"` prints "000" on connection failure but curl still
    # exits non-zero — capture into a var so set -e doesn't fire and so we
    # don't accidentally double-emit "000" via a fallback echo.
    code="$(curl -s -m 5 -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)" || code="${code:-000}"
    echo "${code:-000}"
  elif command -v python3 >/dev/null 2>&1; then
    code="$(python3 - "$url" 2>/dev/null <<'PYEOF'
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=5)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")
PYEOF
)" || code="000"
    echo "${code:-000}"
  else
    echo "no-tester"
  fi
}

# Greeting — always shown at install start so the user sees what's about to run.
if ! $DO_UNINSTALL; then
  cat <<EOF
╭───────────────────────────────────────────────────────────────────────╮
│   LLM Systems Agent $VERSION installer                                │
│                                                                       │
│   Universal agent for Linux + macOS hosts. Self-registers with the    │
│   LLM Systems Manager and waits for admin approval before pushing     │
│   metrics. Optionally manages llama.cpp / LM Studio / OpenClaw and    │
│   the perf-controller power-profile transitions.                      │
│                                                                       │
│   This script will:                                                   │
│     1. Verify Python + venv + (Linux) sudoers/systemctl prerequisites │
│     2. Probe the host for llama.cpp / LM Studio / OpenClaw            │
│     3. Install the agent under /opt/llm-systems-agent (configurable)  │
│     4. Drop a systemd unit (Linux) or launchd plist (macOS)           │
│     5. (Linux only, opt-in) drop a sudoers snippet for systemctl      │
│                                                                       │
│   Re-run with --uninstall to remove everything except the registry    │
│   entry on the manager.                                               │
╰───────────────────────────────────────────────────────────────────────╯
EOF
fi

if ! $MANAGER_URL_EXPLICIT; then
  if [[ ! -t 0 ]]; then
    echo "ERROR: --manager-url is required (stdin is not a TTY for prompting)" >&2
    echo "       Pass --manager-url http://host:5000 to set it explicitly." >&2
    exit 1
  fi
  echo
  echo "── Manager URL ──────────────────────────────────────────────────────────"
  echo "  The agent self-registers with the LLM Systems Manager on startup and"
  echo "  forwards metrics there. Provide its base URL (host:port; no trailing"
  echo "  slash needed)."
  echo
  read -rp "  Manager URL [${MANAGER_URL}]: " URL_INPUT
  if [[ -n "$URL_INPUT" ]]; then
    MANAGER_URL="$URL_INPUT"
  fi
fi

# Normalize: trim whitespace + trailing slash, auto-prepend http:// when
# missing a scheme, and append the given default port when missing one. So
# with port 5000, "192.0.2.10" → "http://192.0.2.10:5000" and
# "192.0.2.10:5000" → "http://192.0.2.10:5000". Without a scheme, requests
# fails inside the agent with "No connection adapters were found"; without a
# port, the probe hits :80 and silently times out.
_normalize_url() {
  local raw="${1:-}" default_port="${2:-5000}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  raw="${raw%/}"
  [[ -z "$raw" ]] && { printf ''; return 0; }
  if [[ "$raw" != http://* && "$raw" != https://* ]]; then
    raw="http://$raw"
  fi
  if [[ "$raw" =~ ^https?://[^/]+:[0-9]+(/.*)?$ ]]; then
    printf '%s' "$raw"; return 0
  fi
  local scheme="${raw%%://*}" rest="${raw#*://}" host path=""
  if [[ "$rest" == *"/"* ]]; then
    host="${rest%%/*}"; path="/${rest#*/}"
  else
    host="$rest"
  fi
  printf '%s://%s:%s%s' "$scheme" "$host" "$default_port" "$path"
}
_before="$MANAGER_URL"
MANAGER_URL="$(_normalize_url "$MANAGER_URL" 5000)"
if [[ "$_before" != "$MANAGER_URL" ]]; then
  echo "  ⓘ normalized '$_before' → '$MANAGER_URL'"
fi

_alarm_engine_url_from_manager() {
  # Resolution order:
  #   1. --alarm-engine-url passed on CLI (operator override).
  #   2. [manager].alarm_engine_url in the live manager config — works when
  #      the manager is on this same host (a split install would set a
  #      remote URL there, e.g. http://10.0.0.5:8081). Read via $SUDO
  #      because the file is mode 0600.
  #   3. Fall back to "<manager-host>:8081" (legacy behavior; assumes the
  #      AE is colocated with the manager).
  if $ALARM_ENGINE_URL_EXPLICIT && [[ -n "$ALARM_ENGINE_URL" ]]; then
    echo "$ALARM_ENGINE_URL"; return 0
  fi
  local mgr_toml="/opt/llm-systems-manager/config/llm-systems.toml" found=""
  if $SUDO test -f "$mgr_toml" 2>/dev/null; then
    # Match: alarm_engine_url = "http://..."  (in any section; only the
    # one under [manager] should exist, but grep -m1 is safe regardless).
    found="$($SUDO grep -E '^[[:space:]]*alarm_engine_url[[:space:]]*=' "$mgr_toml" 2>/dev/null \
              | head -n1 | sed -E 's/[^"]*"([^"]+)".*/\1/')"
  fi
  if [[ -n "$found" ]]; then
    echo "$found"; return 0
  fi
  if [[ "$MANAGER_URL" =~ ^(https?://[^:/]+)(:[0-9]+)?(/.*)?$ ]]; then
    echo "${BASH_REMATCH[1]}:8081"
  else
    echo "$MANAGER_URL"
  fi
}

if ! $ALARM_ENGINE_URL_EXPLICIT; then
  _derived_ae_url="$(_alarm_engine_url_from_manager)"
  _mgr_toml_present=false
  $SUDO test -f "/opt/llm-systems-manager/config/llm-systems.toml" 2>/dev/null \
    && _mgr_toml_present=true
  if $_mgr_toml_present; then
    ALARM_ENGINE_URL="$_derived_ae_url"
  elif [[ -t 0 ]]; then
    echo
    echo "  • Alarm engine URL"
    echo "    The agent pushes metrics directly to the alarm engine. On a"
    echo "    split install the AE lives on a different host than the manager;"
    echo "    enter its URL below. Default assumes AE colocated with the manager."
    echo "    (Agents also refresh this from heartbeat acks, so a wrong value"
    echo "    here gets corrected within ~60s of first contact.)"
    read -rp "  Alarm engine URL [${_derived_ae_url}]: " URL_INPUT
    if [[ -n "$URL_INPUT" ]]; then
      ALARM_ENGINE_URL="$URL_INPUT"
    else
      ALARM_ENGINE_URL="$_derived_ae_url"
    fi
  else
    ALARM_ENGINE_URL="$_derived_ae_url"
  fi
fi

# Lock in the resolved AE URL: normalize it (scheme + default :8081) the same
# way as the manager URL, and mark it explicit so the summary, OpenClaw OTEL
# suggestion, and config writer all use exactly this value. Without this they
# re-derive via _alarm_engine_url_from_manager, which on a split/agent-only
# host (no manager TOML) falls back to the manager IP and silently discards
# an operator-entered URL.
if [[ -n "$ALARM_ENGINE_URL" ]]; then
  _ae_before="$ALARM_ENGINE_URL"
  ALARM_ENGINE_URL="$(_normalize_url "$ALARM_ENGINE_URL" 8081)"
  ALARM_ENGINE_URL_EXPLICIT=true
  [[ "$_ae_before" != "$ALARM_ENGINE_URL" ]] \
    && echo "  ⓘ normalized '$_ae_before' → '$ALARM_ENGINE_URL'"
fi

# Reachability test — loops until we get any HTTP response from
# /api/agents that proves the manager is serving. 200 means the
# endpoint is open (legacy auth_mode=disabled / trusted_cidr admitting
# this host); 401/403 means the dashboard auth gate is active and
# declined us — still proof the manager is up and routing. Either
# answer is good enough to proceed: the actual /api/agents/register
# call below carries its own credentials. For --manager-url (explicit)
# we sleep+retry 3× without prompting, since the manager may just be
# warming up. Without --manager-url we prompt for a new URL on each
# failure (same 3-attempt budget).
#
# After a full batch of MAX_CHECK_ATTEMPTS failures we DO NOT exit
# automatically — the universal installer fires the agent install
# right after starting the manager, so a slow first start can hit a
# brief reachability gap that the operator just needs to wait through
# (instead of re-running the whole install). On TTY we drop to a
# Retry/Quit prompt; on non-TTY there's no way to ask so we exit 1.
CHECK_ATTEMPT=0
MAX_CHECK_ATTEMPTS=3
RETRY_SLEEP_S=5
while true; do
  CHECK_ATTEMPT=$((CHECK_ATTEMPT + 1))
  echo
  echo "  Testing reachability of $MANAGER_URL  (attempt $CHECK_ATTEMPT/$MAX_CHECK_ATTEMPTS) ..."
  HTTP_CODE="$(_probe_http_code "$MANAGER_URL/api/agents")"
  case "$HTTP_CODE" in
    200|401|403)
      # 401/403 = manager up but dashboard auth gate declined an
      # unauthenticated probe. Registration itself uses /api/agents/register
      # which is in _AUTH_OPEN_PATHS, so this is safe to proceed past.
      echo "  ✓ manager reachable (HTTP $HTTP_CODE — registry endpoint responding)"
      break
      ;;
    no-tester)
      echo "  ERROR: neither curl nor python3 are available to test the URL." >&2
      echo "         Install one and re-run." >&2
      exit 1
      ;;
    *)
      echo "  ✗ manager not reachable (HTTP $HTTP_CODE for $MANAGER_URL/api/agents)"
      ;;
  esac

  if (( CHECK_ATTEMPT >= MAX_CHECK_ATTEMPTS )); then
    echo
    echo "  $MAX_CHECK_ATTEMPTS attempts failed, ${RETRY_SLEEP_S}s apart."
    if [[ ! -t 0 ]]; then
      echo "  Cannot prompt for a decision (stdin is not a TTY). Verify the"
      echo "  manager is running and reachable from this host (firewall,"
      echo "  port 5000), then re-run the installer."
      exit 1
    fi
    while true; do
      read -rp "  [R]etry another $MAX_CHECK_ATTEMPTS attempts, or [Q]uit? " _ans
      case "$(printf '%s' "$_ans" | tr '[:upper:]' '[:lower:]')" in
        r|retry|"")
          CHECK_ATTEMPT=0
          echo "  Restarting probe batch …"
          break
          ;;
        q|quit|exit)
          echo "  Verify the manager is running and reachable, then re-run the"
          echo "  installer when ready."
          exit 1
          ;;
        *)
          echo "  Please answer R (retry) or Q (quit)."
          ;;
      esac
    done
    continue
  fi

  # Explicit URL: just wait + retry the same one — most likely cause is
  # the manager is still warming up. Don't prompt; the operator chose
  # this URL via the CLI flag.
  if $MANAGER_URL_EXPLICIT; then
    echo "  Retrying in ${RETRY_SLEEP_S}s …"
    sleep "$RETRY_SLEEP_S"
    continue
  fi

  if [[ ! -t 0 ]]; then
    echo "  Cannot prompt for a different URL (stdin is not a TTY)."
    echo "  Retrying same URL in ${RETRY_SLEEP_S}s …"
    sleep "$RETRY_SLEEP_S"
    continue
  fi

  echo
  read -rp "  Enter a different URL (or press ENTER to retry the same one): " URL_INPUT
  if [[ -n "$URL_INPUT" ]]; then
    _before="$URL_INPUT"
    MANAGER_URL="$(_normalize_url "$URL_INPUT" 5000)"
    [[ "$_before" != "$MANAGER_URL" ]] && echo "  ⓘ normalized '$_before' → '$MANAGER_URL'"
  else
    sleep "$RETRY_SLEEP_S"
  fi
done
echo "─────────────────────────────────────────────────────────────────────────"

_HOSTNAME_TO_REGISTER="${HOSTNAME_OVERRIDE:-$(hostname)}"
echo
echo "── Checking hostname uniqueness on the manager ────────────────────────"
echo "  Hostname this agent will register as: $_HOSTNAME_TO_REGISTER"
_AGENTS_JSON="$(curl -fsS -m 5 "$MANAGER_URL/api/agents" 2>/dev/null || true)"
if [[ -z "$_AGENTS_JSON" ]] || ! printf '%s' "$_AGENTS_JSON" | grep -q '"agents"'; then
  echo "  ⓘ couldn't read the registry (HTTP 401/403 or not JSON) — skipping"
  echo "    check. If the hostname collides at runtime the agent's CRITICAL"
  echo "    log will spell out the recovery steps."
else
  if ! command -v python3 >/dev/null 2>&1; then
    echo "  ⓘ python3 not on PATH — skipping uniqueness check"
  else
    _COLLISION="$(printf '%s' "$_AGENTS_JSON" \
      | python3 - "$_HOSTNAME_TO_REGISTER" <<'PYEOF'
import json, sys
name = sys.argv[1]
try:
    data = json.load(sys.stdin) or {}
except Exception:
    sys.exit(0)
for a in (data.get("agents") or []):
    if a.get("hostname") == name:
        print(f'{a.get("agent_id","?")}|{a.get("registered_from","?")}|{a.get("status","?")}|{a.get("last_seen","?")}')
        break
PYEOF
)"
    if [[ -n "$_COLLISION" ]]; then
      IFS='|' read -r _C_ID _C_IP _C_STATUS _C_LAST <<<"$_COLLISION"
      echo "  ⚠ A different agent is already registered with this hostname:"
      echo "        agent_id       $_C_ID"
      echo "        registered_from $_C_IP"
      echo "        status         $_C_STATUS"
      echo "        last_seen      $_C_LAST"
      echo
      echo "  Installing a second agent with the same hostname will be rejected"
      echo "  by the manager with HTTP 403 unless this install can present the"
      echo "  prior agent's token. Two clean paths:"
      echo "    (a) Set a unique hostname for this host BEFORE installing the agent"
      echo "        (sudo hostnamectl set-hostname <name>), then re-run this installer."
      echo "    (b) Delete the existing record from the dashboard's Admin tab,"
      echo "        then re-run this installer."
      if [[ -t 0 ]]; then
        # Auto-suggest a unique name and let the operator accept with ENTER.
        _SUGGEST="${_HOSTNAME_TO_REGISTER}-$(date +%s | tail -c 6)"
        echo
        read -rp "  Override the hostname this agent registers as? Suggested [$_SUGGEST] or type a different name, or ENTER to abort: " _NEW_HN
        if [[ -z "$_NEW_HN" ]]; then
          echo
          echo "  Aborted. Pick option (a) or (b) above and re-run."
          exit 1
        fi
        [[ "$_NEW_HN" == "y" || "$_NEW_HN" == "yes" ]] && _NEW_HN="$_SUGGEST"
        HOSTNAME_OVERRIDE="$_NEW_HN"
        echo "  ✓ this agent will register as: $HOSTNAME_OVERRIDE"
      else
        echo
        echo "  ✗ Non-interactive install + hostname collision — aborting."
        echo "    Pass --hostname <unique-name> on the next run, or pre-resolve."
        exit 1
      fi
    else
      echo "  ✓ hostname is unique"
    fi
  fi
fi
unset _AGENTS_JSON _COLLISION _C_ID _C_IP _C_STATUS _C_LAST _SUGGEST _NEW_HN
echo "─────────────────────────────────────────────────────────────────────────"

# _user_exists USER → 0 if exists, 1 otherwise.
_user_exists() { id -u "$1" >/dev/null 2>&1; }

# _can_write_to PARENT → 0 if we (or sudo) can create files there, 1 otherwise.
# Tests by attempting a no-op `mkdir -p && touch && rm`. The parent of the
# target install dir must already exist; we don't `mkdir` arbitrary chains
# of new directories under root paths because that's surprising.
_can_write_to() {
  local parent="$1"
  if [[ -d "$parent" ]]; then
    # Probe write access — first as the current user, then via sudo if available.
    local tmp="$parent/.lsa-installer-write-probe.$$"
    if (touch "$tmp" 2>/dev/null && rm -f "$tmp"); then
      return 0
    fi
    if [[ -n "$SUDO" ]] && $SUDO test -w "$parent"; then
      return 0
    fi
    # Last-resort sudo touch — covers cases where `test -w` lies due to ACLs.
    if [[ -n "$SUDO" ]] && $SUDO sh -c "touch '$tmp' && rm -f '$tmp'" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

echo
echo "── Validating run-as user + install dir ───────────────────────────────"
# Hostname (when overridden) — reject whitespace / control chars / empty
# strings now rather than ship them into the manager registry where they'd
# look like silent corruption.
if [[ -n "$HOSTNAME_OVERRIDE" ]]; then
  if [[ "$HOSTNAME_OVERRIDE" =~ [[:space:]] ]] || [[ ! "$HOSTNAME_OVERRIDE" =~ ^[[:print:]]+$ ]]; then
    echo "  ✗ --hostname must be a single non-empty token without whitespace (got: '$HOSTNAME_OVERRIDE')" >&2
    exit 2
  fi
  echo "  ✓ hostname override: '$HOSTNAME_OVERRIDE'"
fi

# Free-text description — prompt when not passed via --description and we
# have a TTY. Spaces are fine; we just refuse newlines / nulls so the YAML
# write is well-formed (a stray newline would split the value across two
# lines and break parsing).
if [[ -z "$DESCRIPTION_OVERRIDE" && -t 0 ]]; then
  _default_desc="${HOSTNAME_OVERRIDE:-$(hostname)} ($AGENT_OS)"
  read -rp "  Agent description (free text, shown in admin tab) [$_default_desc]: " _v
  DESCRIPTION_OVERRIDE="${_v:-$_default_desc}"
fi
if [[ -n "$DESCRIPTION_OVERRIDE" ]]; then
  # Reject control chars (newline, tab, NUL) that would corrupt the YAML.
  if [[ "$DESCRIPTION_OVERRIDE" =~ [[:cntrl:]] ]]; then
    echo "  ✗ --description must not contain newlines / tabs / control chars" >&2
    exit 2
  fi
  echo "  ✓ description: '$DESCRIPTION_OVERRIDE'"
fi
while ! _user_exists "$USER_ARG"; do
  echo "  ✗ user '$USER_ARG' does not exist on this system"
  if [[ ! -t 0 ]]; then
    echo "    (no TTY — cannot prompt; pass --user EXISTING_USER or pre-create '$USER_ARG'; aborting)" >&2
    exit 1
  fi
  if [[ "$AGENT_OS" == "linux" ]]; then
    echo "    Options:"
    echo "      [c] create '$USER_ARG' now (system user, home /home/$USER_ARG, shell /bin/bash)"
    echo "      [u] enter a different existing username"
    read -rp "    Choice [c/u]: " _choice
    case "$(printf '%s' "${_choice:-c}" | tr '[:upper:]' '[:lower:]')" in
      c|create|"")
        if $SUDO useradd --system --create-home --home-dir "/home/$USER_ARG" --shell /bin/bash "$USER_ARG"; then
          echo "  ✓ created user '$USER_ARG'"
        else
          echo "  ✗ useradd failed for '$USER_ARG' — pick a different user"
          read -rp "    Enter username: " _v
          [[ -n "$_v" ]] && USER_ARG="$_v"
        fi
        ;;
      *)
        read -rp "    Enter username: " _v
        [[ -n "$_v" ]] && USER_ARG="$_v"
        ;;
    esac
  else
    echo "    (macOS user creation isn't automated — create one with System Settings or dscl, then re-run)"
    read -rp "  Enter a different --user (or Ctrl-C to abort): " _v
    [[ -n "$_v" ]] && USER_ARG="$_v"
  fi
done
echo "  ✓ user '$USER_ARG' exists (uid=$(id -u "$USER_ARG"))"

while true; do
  INSTALL_DIR="${INSTALL_DIR%/}"
  PARENT_DIR="$(dirname "$INSTALL_DIR")"
  if [[ -z "$PARENT_DIR" || "$PARENT_DIR" == "$INSTALL_DIR" ]]; then
    echo "  ✗ install-dir '$INSTALL_DIR' has no usable parent"
  elif [[ ! -d "$PARENT_DIR" ]]; then
    echo "  ✗ parent of install-dir does not exist: $PARENT_DIR"
  elif ! _can_write_to "$PARENT_DIR"; then
    echo "  ✗ cannot write to install-dir parent: $PARENT_DIR (need sudo or ownership)"
  else
    echo "  ✓ install-dir parent OK: $PARENT_DIR (target: $INSTALL_DIR)"
    break
  fi
  if [[ ! -t 0 ]]; then
    echo "    (no TTY — cannot prompt for a different install-dir; aborting)" >&2
    exit 1
  fi
  read -rp "  Enter a different --install-dir (or Ctrl-C to abort): " _v
  if [[ -n "$_v" ]]; then
    INSTALL_DIR="${_v%/}"
    # Re-apply the project-name suffix rule for this prompted value.
    if [[ "$(basename "$INSTALL_DIR")" != "llm-systems-agent" ]]; then
      INSTALL_DIR="$INSTALL_DIR/llm-systems-agent"
      echo "    → appended /llm-systems-agent: $INSTALL_DIR"
    fi
  fi
done
echo "─────────────────────────────────────────────────────────────────────────"

# ── Auto-detect providers (only when --role auto) ───────────────────────────
# Comprehensive checks for llama.cpp / LM Studio / OpenClaw, with prompts to
# fill in gaps. Results are folded into ENABLE_* flags + path overrides
# before YAML rendering.

_proc_running() {
  # Returns 0 if any process matches the regex (pgrep -f), 1 otherwise.
  pgrep -f "$1" >/dev/null 2>&1
}

# _first_existing PATHS... → echoes the first path that exists + is executable
# (or just exists for plain config files — stick with -e). Returns 1 if none.
_first_existing() {
  local p
  for p in "$@"; do
    [[ -n "$p" ]] || continue
    if [[ -e "$p" ]]; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

# _binary_from_pid PID → echoes the resolved binary path of a running pid
# by walking, in order:  /proc/<pid>/exe (Linux) → `lsof -p <pid>` txt entry
# (macOS + Linux fallback) → `ps -p <pid> -o comm=` (last resort, may be a
# truncated basename). Empty echo + rc=1 if nothing yields a usable absolute
# path that exists on disk.
#
# Validates the result before returning: must be absolute (/-prefixed) and
# must exist on disk. Without this, when the caller can't ptrace the
# target pid (running as root, /proc/<pid>/exe unreadable), partial
# lsof error output gets parsed by awk and 'denied)' or similar leaks
# back as if it were a binary path — leaving the prompt with garbage.
_binary_from_pid() {
  local pid="$1" path=""
  [[ -z "$pid" ]] && return 1
  if [[ -r "/proc/$pid/exe" ]]; then
    path="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
  fi
  if [[ -z "$path" ]] && command -v lsof >/dev/null 2>&1; then
    # `lsof -p <pid>` lists open files; the FD column 'txt' is the
    # process's running executable. Format is whitespace-delimited;
    # column 9 is NAME (the path). Pipe stderr to /dev/null AND filter
    # out lines that aren't proper file rows (lsof leaks 'access denied'
    # warnings that include the word 'denied' in odd places).
    path="$(lsof -p "$pid" 2>/dev/null \
            | awk 'NR>1 && $4=="txt" && $9 ~ "^/" {print $9; exit}')"
  fi
  if [[ -z "$path" ]]; then
    # `ps -o comm=` may give a basename (less useful) but at least
    # confirms the binary name; combined with `command -v` we can
    # resolve to an absolute path.
    local comm
    comm="$(ps -p "$pid" -o comm= 2>/dev/null | head -1 | xargs)"
    if [[ -n "$comm" ]]; then
      if [[ "$comm" == /* && -x "$comm" ]]; then
        path="$comm"
      elif command -v "$(basename "$comm")" >/dev/null 2>&1; then
        path="$(command -v "$(basename "$comm")")"
      fi
    fi
  fi
  # Final validation: must be absolute, must exist on disk. Empty
  # otherwise so the caller falls through to the known-locations walk
  # instead of recording garbage.
  if [[ -n "$path" && "$path" == /* && -e "$path" ]]; then
    echo "$path"
    return 0
  fi
  return 1
}

# _install_llama_now METHOD BACKEND — fresh-install llama.cpp as the agent
# user via install/install_llama.py; prints the resolved binary path on success.
_install_llama_now() {
  local method="$1" backend="$2"
  local helper="$SRC_DIR/install/install_llama.py"
  if [[ ! -f "$helper" ]]; then
    _err "install helper not found: $helper"; return 1
  fi
  echo "      → installing llama.cpp (method=$method, backend=$backend) as $USER_ARG …" >&2
  local out
  if ! out="$(_run_as "$USER_ARG" python3 "$helper" \
                --method "$method" --backend "$backend" --agent-user "$USER_ARG")"; then
    _err "llama.cpp install failed"; return 1
  fi
  local bin="${out##*RESOLVED_BIN=}"
  if [[ "$out" != *RESOLVED_BIN=* || -z "$bin" ]]; then
    _err "installer did not report a binary path"; return 1
  fi
  printf '%s\n' "$bin"
}

# _offer_llama_unit UNIT BIN — offer a starter systemd unit pointing at BIN.
# Never overwrites an existing unit; never enables/starts.
_offer_llama_unit() {
  local unit="$1" bin="$2"
  [[ -n "$unit" && -n "$bin" ]] || return 0
  command -v systemctl >/dev/null 2>&1 || return 0
  local target="/etc/systemd/system/$unit"
  if [[ -e "$target" ]]; then
    echo "      ⓘ $unit already exists — leaving it; ensure ExecStart runs '$bin --metrics'"
    return 0
  fi
  local tmpl="$SRC_DIR/install/llama_server.service.tmpl"
  if [[ ! -f "$tmpl" ]]; then
    echo "      ⓘ no unit template; create $unit manually with ExecStart='$bin --metrics …'"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "      ⓘ no TTY — not fabricating $unit; create it with ExecStart='$bin --metrics …'"
    return 0
  fi
  local REPLY
  read -rp "      Create a starter $unit pointing at the new binary? (you must add model flags before enabling) [y/N] " REPLY
  case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
    y|yes) ;;
    *) echo "      → skipped; create $unit manually with ExecStart='$bin --metrics …'"; return 0 ;;
  esac
  sed -e "s|__LLAMA_BIN__|$bin|g" -e "s|__USER__|$USER_ARG|g" "$tmpl" \
    | $SUDO tee "$target" >/dev/null
  $SUDO chmod 644 "$target"
  $SUDO systemctl daemon-reload || true
  _ok "wrote $target (disabled). Add model flags, then: sudo systemctl enable --now $unit"
}

_detect_llama() {
  echo
  echo "  • llama.cpp"

  # Try to derive sensible defaults from the running process: binary
  # path, install dir, listening host:port. Without this the prompt
  # would default to /usr/local/llama-server + :8080 even when llama
  # was clearly running with different values.
  local detected_bin=""
  local detected_dir=""
  local detected_host=""
  local detected_port=""
  local detected_config=""
  local detected_log=""
  local pid=""
  # Match the llama-server binary specifically — `pgrep -x` compares
  # against argv[0]'s basename, so things like `tail -F llama-server.log`
  # (which root often runs alongside) don't false-match.
  if pgrep -x 'llama-server' >/dev/null 2>&1; then
    pid="$(pgrep -x 'llama-server' | head -1)"
    # Resolve the running binary path via /proc/exe → lsof → ps fallback.
    detected_bin="$(_binary_from_pid "$pid" 2>/dev/null || true)"
    # LM Studio ships its llama.cpp backend as a binary literally named
    # 'llama-server' under ~/.lmstudio/extensions/backends/. That's LM Studio's
    # engine, not a standalone llama.cpp server — don't enable the llama
    # provider for it; the host is detected as an LM Studio provider instead. (#160)
    if [[ "$detected_bin" == *"/.lmstudio/"* ]]; then
      echo "      ⓘ ignoring LM Studio's bundled llama backend ($detected_bin)"
      echo "        — not a standalone llama.cpp server"
      detected_bin=""
      pid=""
    fi
    if [[ -n "$detected_bin" ]]; then
      detected_dir="$(dirname "$detected_bin")"
      echo "      ✓ llama-server binary discovered at: $detected_bin"
    fi

    # Parse --port and --host from the process's argv. llama-server
    # accepts both `--port 8080` and `--port=8080`, same for --host.
    # On Linux: read /proc/<pid>/cmdline (NUL-separated). On macOS or
    # if /proc isn't available: fall back to `ps -o args=`.
    local cmdline=""
    if [[ -r "/proc/$pid/cmdline" ]]; then
      cmdline="$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null || true)"
    fi
    if [[ -z "$cmdline" ]]; then
      cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    fi
    if [[ -n "$cmdline" ]]; then
      # `|| true` on each: under `set -euo pipefail` a no-match grep in the
      # pipeline (e.g. argv with no --port/--host) returns 1, and with pipefail
      # that propagates to the assignment and kills the script (#160).
      detected_port="$(echo "$cmdline" | grep -oE -- '--port[ =][0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
      detected_host="$(echo "$cmdline" | grep -oE -- '--host[ =][^ ]+' | sed 's/^--host[ =]//' | head -1 || true)"
      # llama-server's config-file flag has had a few names — try them
      # all. Most modern installs use --models-preset (which the user's
      # llama-server configs do); older builds used --config or -c.
      # Only take the first non-empty match.
      detected_config="$(echo "$cmdline" \
                         | grep -oE -- '(--models-preset|--config|-c)[ =][^ ]+' \
                         | sed -E 's/^(--models-preset|--config|-c)[ =]//' \
                         | head -1 || true)"
      detected_log="$(echo "$cmdline" \
                      | grep -oE -- '--log-file[ =][^ ]+' \
                      | sed 's/^--log-file[ =]//' \
                      | head -1 || true)"
    fi
  fi

  # Last-resort port discovery: HTTP probe of common ports + lsof.
  # Done after the cmdline parse so an explicit --port wins.
  if [[ -n "$pid" && -z "$detected_port" ]]; then
    for _try_port in 8080 8081; do
      if [[ "$(_probe_http_code "http://127.0.0.1:$_try_port/v1/models")" == "200" ]]; then
        detected_port="$_try_port"
        echo "      ✓ port $_try_port discovered via HTTP probe"
        break
      fi
    done
    if [[ -z "$detected_port" ]] && command -v lsof >/dev/null 2>&1; then
      local _lsof_port
      _lsof_port="$(lsof -nP -iTCP -sTCP:LISTEN -p "$pid" 2>/dev/null \
                    | awk '{print $9}' | grep -oE ':[0-9]+$' | tr -d ':' | head -1)"
      if [[ -n "$_lsof_port" ]]; then
        detected_port="$_lsof_port"
        echo "      ✓ port $_lsof_port discovered via lsof"
      fi
    fi
  fi

  local found=false
  if [[ -n "$detected_bin" ]]; then
    echo "      ✓ found running 'llama-server' (binary: $detected_bin)"
    if [[ -n "$detected_port" ]]; then
      echo "        listening on ${detected_host:-127.0.0.1}:$detected_port"
    else
      echo "        ⓘ port not auto-discovered — prompt will default to 8080 (override at the prompt)"
    fi
    found=true
  elif [[ "$(_probe_http_code http://127.0.0.1:8080/v1/models)" == "200" ]]; then
    echo "      ✓ HTTP API responding at http://127.0.0.1:8080"
    detected_port="8080"
    found=true
  fi

  # If we have no binary yet (HTTP-only detection, or pid-extraction
  # failed), walk known install locations. Caller sees the picked path
  # in the binary-prompt default below.
  if $found && [[ -z "$detected_bin" ]]; then
    local _llama_bin=""
    if _llama_bin="$(_first_existing \
                      "$(command -v llama-server 2>/dev/null)" \
                      "/usr/local/llama-server/llama-server" \
                      "/usr/local/bin/llama-server" \
                      "/opt/homebrew/bin/llama-server" \
                      "$HOME/llama.cpp/build/bin/llama-server")"; then
      # Same LM Studio exclusion as the pid path — `command -v` can resolve to
      # LM Studio's bundled backend if it's on PATH (#160).
      if [[ "$_llama_bin" == *"/.lmstudio/"* ]]; then
        echo "      ⓘ ignoring LM Studio's bundled llama backend ($_llama_bin)"
      else
        detected_bin="$_llama_bin"
        detected_dir="$(dirname "$_llama_bin")"
        echo "      ✓ llama-server binary discovered at: $_llama_bin"
      fi
    fi
  fi

  if ! $found; then
    echo "      ✗ no llama-server process; HTTP probe on :8080 failed"
    local _llama_just_installed=false _llama_installed_method=""
    local _want_install=false
    if $INSTALL_LLAMA; then
      _want_install=true
    elif [[ -t 0 ]]; then
      read -rp "      Install llama.cpp now with the agent's installer? [y/N] " REPLY
      case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
        y|yes) _want_install=true ;;
      esac
    fi
    if $_want_install; then
      local _m="$INSTALL_LLAMA_METHOD" _b="$INSTALL_LLAMA_BACKEND" _newbin="" _v
      # Retry loop: on failure, offer another method / quit / skip to manual.
      while true; do
        if [[ -t 0 ]] && ! $INSTALL_LLAMA; then
          read -rp "      Install method (source/release_binary/conda/homebrew) [$_m]: " _v
          _m="${_v:-$_m}"
          read -rp "      Backend (cpu/cuda/vulkan/rocm/metal) [$_b]: " _v
          _b="${_v:-$_b}"
        fi
        if _newbin="$(_install_llama_now "$_m" "$_b")"; then
          detected_bin="$_newbin"
          detected_dir="$(dirname "$_newbin")"
          _llama_just_installed=true
          _llama_installed_method="$_m"
          found=true
          _ok "llama.cpp installed → $_newbin"
          break
        fi
        _err "llama.cpp install failed — see the error above"
        # No TTY or CLI-driven (--install-llama): can't prompt; give up the attempt.
        if [[ ! -t 0 ]] || $INSTALL_LLAMA; then
          break
        fi
        echo "      A different method may avoid this — 'release_binary' needs no"
        echo "      compiler/cmake; 'conda'/'homebrew' need that package manager installed."
        echo "      [r] retry with another method   [q] quit to install prerequisites   [s] skip and configure manually"
        read -rp "      Choose [r/q/s]: " _v
        case "$(printf '%s' "$_v" | tr '[:upper:]' '[:lower:]')" in
          r|retry) continue ;;
          q|quit)  _err "aborting install — re-run the installer after installing the prerequisites"; exit 1 ;;
          *)       break ;;
        esac
      done
    fi
    if ! $found; then
      if [[ ! -t 0 ]]; then
        echo "      → skipped (stdin is not a TTY for prompting)"
        return
      fi
      read -rp "      Manually configure llama.cpp now? [y/N] " REPLY
      case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
        y|yes) ;;
        *) echo "      → skipped"; return ;;
      esac
    fi
  fi

  # When found OR the user opted into manual configuration, gather paths.
  # Detect existing install method from the binary path.
  detected_build_method="custom_script"
  case "$detected_bin" in
    */homebrew/*|/opt/homebrew/*)            detected_build_method="homebrew" ;;
    */envs/*|*/miniconda*/*|*/anaconda*/*)   detected_build_method="conda" ;;
  esac
  if [[ "$detected_build_method" == "custom_script" \
        && ! -f /usr/local/llama-server/build-llama-cpp.sh ]] \
     && command -v brew >/dev/null 2>&1 && [[ -x /opt/homebrew/bin/llama-server ]]; then
    detected_build_method="homebrew"
  fi
  # Prefer the method we installed with over path-based detection.
  if [[ "${_llama_just_installed:-false}" == true && -n "${_llama_installed_method:-}" ]]; then
    detected_build_method="$_llama_installed_method"
  fi

  # Skip prompts only if there's no TTY (auto-detect with no terminal).
  ENABLE_LLAMA=true

  if [[ ! -t 0 ]]; then
    # No TTY: keep blank overrides so the agent's class defaults apply.
    # The user will need to edit agent_config.yaml after install if they
    # want non-default paths. We log the caveat clearly.
    echo "      ⓘ no TTY; using agent built-in path defaults — edit agent_config.yaml to override"
    LLAMA_BIN_OVERRIDE="${detected_bin}"
    LLAMA_LOG_FILE_OVERRIDE="${detected_log:-${detected_dir:+$detected_dir/llama-server.log}}"
    LLAMA_CONFIG_INI_OVERRIDE="${detected_config}"
    LLAMA_BUILD_METHOD_OVERRIDE="${detected_build_method:-custom_script}"
    return
  fi

  local _v _default_dir _default_bin _default_log _default_ini
  _default_dir="${detected_dir:-/usr/local/llama-server}"
  _default_bin="${detected_bin:-$_default_dir/llama-server}"

  # Config file: prefer what was parsed from the running argv (most
  # authoritative — that's the actual file llama-server has open), then
  # check next-to-binary, then walk known locations. If nothing exists,
  # leave the prompt default empty so the user knows to fill it in.
  if [[ -n "$detected_config" && -f "$detected_config" ]]; then
    _default_ini="$detected_config"
    echo "      ✓ config file discovered from running argv: $_default_ini"
  elif _default_ini="$(_first_existing \
                        "$_default_dir/config.ini" \
                        "/usr/local/llama-server/config.ini" \
                        "/etc/llama-server/config.ini" \
                        "/etc/llama-cpp/config.ini" \
                        "$HOME/.config/llama-server/config.ini" \
                        "$HOME/.llama-server/config.ini")"; then
    echo "      ✓ config file found at: $_default_ini"
  else
    _default_ini=""
    echo "      ⓘ config file not auto-detected — set the path at the prompt below"
  fi

  # Log file: same approach — argv > known locations > empty default.
  if [[ -n "$detected_log" && -f "$detected_log" ]]; then
    _default_log="$detected_log"
    echo "      ✓ log file discovered from running argv: $_default_log"
  elif _default_log="$(_first_existing \
                        "$_default_dir/llama-server.log" \
                        "/usr/local/llama-server/llama-server.log" \
                        "/var/log/llama-server.log")"; then
    echo "      ✓ log file found at: $_default_log"
  else
    _default_log=""
    echo "      ⓘ log file not auto-detected — set the path at the prompt below"
  fi

  read -rp "      Binary path [$_default_bin]: " _v
  LLAMA_BIN_OVERRIDE="${_v:-$_default_bin}"

  # Config-file prompt allows blank when nothing was found; emit a
  # warning if the path the user supplies (or accepts) doesn't exist
  # so they don't ship a config that points at a non-existent file.
  if [[ -n "$_default_ini" ]]; then
    read -rp "      Config file [$_default_ini]: " _v
    LLAMA_CONFIG_INI_OVERRIDE="${_v:-$_default_ini}"
  else
    read -rp "      Config file (full path): " _v
    LLAMA_CONFIG_INI_OVERRIDE="$_v"
  fi
  if [[ -n "$LLAMA_CONFIG_INI_OVERRIDE" && ! -f "$LLAMA_CONFIG_INI_OVERRIDE" ]]; then
    echo "      ⚠ '$LLAMA_CONFIG_INI_OVERRIDE' not found at that path — agent will retry at runtime"
  fi

  if [[ -n "$_default_log" ]]; then
    read -rp "      Log file [$_default_log]: " _v
    LLAMA_LOG_FILE_OVERRIDE="${_v:-$_default_log}"
  else
    read -rp "      Log file (full path): " _v
    LLAMA_LOG_FILE_OVERRIDE="$_v"
  fi
  if [[ -n "$LLAMA_LOG_FILE_OVERRIDE" && ! -f "$LLAMA_LOG_FILE_OVERRIDE" ]]; then
    echo "      ⚠ '$LLAMA_LOG_FILE_OVERRIDE' not found at that path — perf controller will wait for it to appear"
  fi

  local _api_host="${detected_host:-127.0.0.1}"
  if [[ "$_api_host" == "0.0.0.0" || "$_api_host" == "::" ]]; then
    _api_host="127.0.0.1"
  fi
  local _default_api_url="http://${_api_host}:${detected_port:-8080}"
  read -rp "      API URL [$_default_api_url]: " _v
  LLAMA_API_URL_OVERRIDE="${_v:-$_default_api_url}"
  if [[ -n "$LLAMA_API_URL_OVERRIDE" \
        && "$LLAMA_API_URL_OVERRIDE" != http://* \
        && "$LLAMA_API_URL_OVERRIDE" != https://* ]]; then
    LLAMA_API_URL_OVERRIDE="http://$LLAMA_API_URL_OVERRIDE"
  fi

  read -rp "      systemd unit [llama_server.service]: " _v
  LLAMA_SYSTEMD_UNIT_OVERRIDE="${_v:-llama_server.service}"

  read -rp "      Build method [$detected_build_method]: " _v
  LLAMA_BUILD_METHOD_OVERRIDE="${_v:-$detected_build_method}"

  if [[ "${_llama_just_installed:-false}" == true ]]; then
    _offer_llama_unit "$LLAMA_SYSTEMD_UNIT_OVERRIDE" "$LLAMA_BIN_OVERRIDE"
  fi

  echo "      → llama.cpp configured"

  # Once configured, check whether --metrics flag is on the systemd unit
  # or on the running process.
  _check_llama_metrics_flag
}

_check_llama_metrics_flag() {
  local unit="${LLAMA_SYSTEMD_UNIT_OVERRIDE:-llama_server.service}"
  local catout
  catout="$(systemctl cat "$unit" 2>/dev/null || true)"

  # Check the unit file first; if not present (or doesn't have the flag),
  # fall back to inspecting the running process's command line. Either
  # source confirming --metrics is enough.
  if [[ -n "$catout" ]] && echo "$catout" | grep -q -- '--metrics\b'; then
    echo "      ✓ --metrics flag present in $unit"
    return
  fi

  # Process check — `ps -o args` shows the full command line for any
  # llama-server process. `pgrep -af` is also fine but ps is more portable.
  local procargs
  procargs="$(ps -C llama-server -o args= 2>/dev/null || true)"
  if [[ -z "$procargs" ]]; then
    procargs="$(pgrep -af 'llama-server' 2>/dev/null || true)"
  fi
  if echo "$procargs" | grep -q -- '--metrics\b'; then
    echo "      ✓ --metrics flag present on running llama-server process"
    return
  fi

  if [[ -z "$catout" && -z "$procargs" ]]; then
    echo "      ⓘ '$unit' not installed and no running llama-server found;"
    echo "        cannot verify --metrics flag. Add --metrics to llama-server's"
    echo "        args before enabling the unit."
    return
  fi

  echo "      ⚠ --metrics flag NOT found in $unit ExecStart or on the running process"
  echo "        Without it, llama-server's /metrics endpoint stays empty"
  echo "        and tps / KV cache stats won't be collected."
  if [[ -n "$catout" ]]; then
    echo
    echo "        To add it, edit the unit:"
    echo "            sudo systemctl edit --full $unit"
    echo "        Append --metrics to the ExecStart= line, then:"
    echo "            sudo systemctl daemon-reload"
    echo "            sudo systemctl restart $unit"
  fi
}

_detect_lms() {
  echo
  echo "  • LM Studio"

  # Tracks what auto-detect already populated, so the post-detect prompt
  # block knows what's still missing.
  local detected_via=""
  local detected_port=""

  # a) process check (matches both 'lmstudio' and 'lm-studio' variants)
  if _proc_running 'lmstudio|lm-studio'; then
    local pid
    pid="$(pgrep -f 'lmstudio|lm-studio' | head -1)"
    echo "      ✓ found running LM Studio process (pid $pid)"
    ENABLE_LMS=true
    detected_via="process"

     local _lms_bin=""
    if _lms_bin="$(_first_existing \
                    "$(command -v lms 2>/dev/null)" \
                    "$HOME/.lmstudio/bin/lms" \
                    "/usr/local/bin/lms" \
                    "/opt/homebrew/bin/lms")"; then
      LMS_CMD_OVERRIDE="$_lms_bin"
      echo "      ✓ lms CLI discovered at: $_lms_bin"
    else
      echo "      ⓘ lms CLI not in any known location — prompt will let you set the path"
    fi

    if command -v lms >/dev/null 2>&1; then
      local _lms_status
      _lms_status="$(lms status 2>/dev/null || true)"
      if echo "$_lms_status" | grep -qE "Server: ON|running"; then
        local _p
        _p="$(echo "$_lms_status" | grep -oE 'port:\s*[0-9]+' | grep -oE '[0-9]+' | head -1)"
        if [[ -n "$_p" ]]; then
          detected_port="$_p"
          LMS_API_URL_OVERRIDE="http://127.0.0.1:$_p"
          LMS_CMD_OVERRIDE="$(command -v lms)"
          echo "      ✓ port $_p discovered via 'lms status'"
        fi
      fi
    fi
    if [[ -z "$detected_port" ]]; then
      for _try_port in 1234 1235; do
        if [[ "$(_probe_http_code "http://127.0.0.1:$_try_port/v1/models")" == "200" ]]; then
          detected_port="$_try_port"
          LMS_API_URL_OVERRIDE="http://127.0.0.1:$_try_port"
          echo "      ✓ port $_try_port discovered via HTTP probe"
          break
        fi
      done
    fi
    if [[ -z "$detected_port" ]] && command -v lsof >/dev/null 2>&1; then
      # lsof can list listening TCP ports for a pid. Format varies by
      # platform; the simplest portable approach is grep for ":<port> (LISTEN)".
      local _lsof_port
      _lsof_port="$(lsof -nP -iTCP -sTCP:LISTEN -p "$pid" 2>/dev/null \
                    | awk '{print $9}' | grep -oE ':[0-9]+$' | tr -d ':' | head -1)"
      if [[ -n "$_lsof_port" ]]; then
        detected_port="$_lsof_port"
        LMS_API_URL_OVERRIDE="http://127.0.0.1:$_lsof_port"
        echo "      ✓ port $_lsof_port discovered via lsof"
      fi
    fi
    if [[ -z "$detected_port" ]]; then
      echo "      ⓘ port not auto-discovered — prompt will default to 1234 (override at the prompt)"
    fi
  # b) HTTP API on default port 1234
  elif [[ "$(_probe_http_code http://127.0.0.1:1234/v1/models)" == "200" ]]; then
    echo "      ✓ HTTP API responding at http://127.0.0.1:1234"
    LMS_API_URL_OVERRIDE="http://127.0.0.1:1234"
    detected_port="1234"
    ENABLE_LMS=true
    detected_via="http"
    # HTTP says LMS is up; find the lms CLI to drive it.
    local _lms_bin=""
    if _lms_bin="$(_first_existing \
                    "$(command -v lms 2>/dev/null)" \
                    "$HOME/.lmstudio/bin/lms" \
                    "/usr/local/bin/lms" \
                    "/opt/homebrew/bin/lms")"; then
      LMS_CMD_OVERRIDE="$_lms_bin"
      echo "      ✓ lms CLI discovered at: $_lms_bin"
    fi
  # c) lms binary + 'lms status' check
  elif command -v lms >/dev/null 2>&1; then
    local lms_path
    lms_path="$(command -v lms)"
    local lms_status
    lms_status="$(lms status 2>/dev/null || true)"
    if echo "$lms_status" | grep -qE "Server: ON|running"; then
      local _port
      _port="$(echo "$lms_status" | grep -oE 'port:\s*[0-9]+' | grep -oE '[0-9]+' | head -1)"
      _port="${_port:-1235}"
      echo "      ✓ 'lms status' reports server ON at port $_port (binary: $lms_path)"
      LMS_CMD_OVERRIDE="$lms_path"
      LMS_API_URL_OVERRIDE="http://127.0.0.1:$_port"
      detected_port="$_port"
      ENABLE_LMS=true
      detected_via="lms-status"
    else
      echo "      ⓘ lms binary present at $lms_path but server is not running"
      LMS_CMD_OVERRIDE="$lms_path"
      # Don't auto-enable; treat as not-found and prompt below
    fi
  fi

  # d) Not detected at all → ask the user whether to configure manually.
  if ! $ENABLE_LMS; then
    echo "      ✗ no LM Studio process; default port 1234 not reachable; lms not running"
    if [[ ! -t 0 ]]; then
      echo "      → skipped (stdin is not a TTY for prompting)"
      return
    fi
    read -rp "      Manually configure LM Studio now? [y/N] " REPLY
    case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
      y|yes) ENABLE_LMS=true ;;
      *) echo "      → skipped"; return ;;
    esac
  fi

  if [[ ! -t 0 ]]; then
    # No TTY: do best-effort defaults so the config has *something* useful.
    if [[ -z "$LMS_CMD_OVERRIDE" ]]; then
      if command -v lms >/dev/null 2>&1; then
        LMS_CMD_OVERRIDE="$(command -v lms)"
      elif [[ "$AGENT_OS" == "macos" ]]; then
        LMS_CMD_OVERRIDE="$HOME/.lmstudio/bin/lms"
      else
        LMS_CMD_OVERRIDE="/usr/local/bin/lms"
      fi
    fi
    if [[ -z "$LMS_API_URL_OVERRIDE" ]]; then
      LMS_API_URL_OVERRIDE="http://127.0.0.1:${detected_port:-1234}"
    fi
    echo "      ⓘ no TTY; using best-guess: cmd=$LMS_CMD_OVERRIDE url=$LMS_API_URL_OVERRIDE"
    return
  fi

  # Confirm/override binary path. Try `which lms`, then platform conventions.
  local _v _default_cmd
  _default_cmd="$LMS_CMD_OVERRIDE"
  if [[ -z "$_default_cmd" ]]; then
    if command -v lms >/dev/null 2>&1; then
      _default_cmd="$(command -v lms)"
    elif [[ "$AGENT_OS" == "macos" ]]; then
      _default_cmd="$HOME/.lmstudio/bin/lms"
    else
      _default_cmd="/usr/local/bin/lms"
    fi
  fi
  read -rp "      lms binary [$_default_cmd]: " _v
  LMS_CMD_OVERRIDE="${_v:-$_default_cmd}"
  if [[ ! -x "$LMS_CMD_OVERRIDE" && ! -f "$LMS_CMD_OVERRIDE" ]]; then
    echo "      ⚠ '$LMS_CMD_OVERRIDE' not found at that path — agent will retry at runtime"
  fi

  # Confirm/override API URL. The default LM Studio CLI server runs on
  # 1234 (UI-attached) or 1235 (headless). Prefer whatever auto-detect
  # found; otherwise 1234.
  local _default_url
  _default_url="${LMS_API_URL_OVERRIDE:-http://127.0.0.1:${detected_port:-1234}}"
  read -rp "      API URL [$_default_url]: " _v
  LMS_API_URL_OVERRIDE="${_v:-$_default_url}"
  # Auto-prepend http:// if the user typed "127.0.0.1:1234"
  if [[ -n "$LMS_API_URL_OVERRIDE" && "$LMS_API_URL_OVERRIDE" != http://* && "$LMS_API_URL_OVERRIDE" != https://* ]]; then
    LMS_API_URL_OVERRIDE="http://$LMS_API_URL_OVERRIDE"
  fi

  echo "      → LM Studio configured (cmd=$LMS_CMD_OVERRIDE  url=$LMS_API_URL_OVERRIDE)"
}

_detect_openclaw() {
  echo
  echo "  • OpenClaw"
  local detected=""

  # a) default install dir ~/.openclaw
  local home_oc="$HOME/.openclaw"
  local home_agents="$home_oc/agents"
  if [[ -d "$home_oc" ]]; then
    detected="installed at $home_oc"
    [[ -d "$home_agents" ]] && OPENCLAW_AGENTS_DIR_OVERRIDE="$home_agents"
  # b) running gateway process
  elif _proc_running 'gateway --port 18789|openclaw.*gateway'; then
    detected="gateway running on port 18789"
  # c) `which openclaw`
  elif command -v openclaw >/dev/null 2>&1; then
    detected="openclaw binary at $(command -v openclaw)"
  fi

  if [[ -n "$detected" ]]; then
    echo "      ✓ $detected"
    ENABLE_OPENCLAW=true
  else
    echo "      ✗ no ~/.openclaw, no gateway process on :18789, no openclaw binary"
    if [[ -t 0 ]]; then
      read -rp "      Manually configure OpenClaw now? [y/N/skip] " REPLY
      case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
        y|yes)
          local _v
          read -rp "      Install dir [$HOME/.openclaw]: " _v
          local _dir="${_v:-$HOME/.openclaw}"
          OPENCLAW_AGENTS_DIR_OVERRIDE="$_dir/agents"
          ENABLE_OPENCLAW=true
          echo "      → OpenClaw manually configured (agents dir: $OPENCLAW_AGENTS_DIR_OVERRIDE)"
          ;;
        *)
          echo "      → skipped"
          ;;
      esac
    fi
  fi

  if $ENABLE_OPENCLAW && [[ -t 0 ]]; then
    echo
    local ae_url
    ae_url="$(_alarm_engine_url_from_manager)"
    echo "      OpenClaw can push OTLP metrics/traces/logs to the alarm engine."
    echo "      Suggested OTEL endpoint: $ae_url"
    if ! command -v openclaw >/dev/null 2>&1; then
      echo "      ⓘ 'openclaw' is not on PATH — if you say yes, the installer"
      echo "        will try anyway and tell you which commands failed so you"
      echo "        can run them manually with the correct binary path."
    fi
    read -rp "      Configure OpenClaw OTEL now (writes via 'openclaw config set')? [y/N] " REPLY
    case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
      y|yes)
        _configure_openclaw_otel "$ae_url"
        ;;
      *)
        echo "      → skipped (you can run the openclaw config set commands manually later)"
        ;;
    esac
  fi
}

_detect_imggen() {
  echo
  echo "  • Image Generation (stable-diffusion.cpp)"
  local detected=""
  # a) running sd-server process
  if _proc_running 'sd-server'; then
    detected="sd-server process running"
  # b) HTTP probe on the well-known sd.cpp port
  elif [[ "$(_probe_http_code http://127.0.0.1:1234/)" =~ ^(200|404)$ ]]; then
    detected="HTTP responder on 127.0.0.1:1234"
  fi

  if [[ -n "$detected" ]]; then
    echo "      ✓ $detected"
    ENABLE_IMGGEN=true
  else
    echo "      ✗ no sd-server process, nothing on :1234"
    if [[ -t 0 ]]; then
      read -rp "      Enable image generation capability anyway? [y/N] " REPLY
      case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
        y|yes)
          ENABLE_IMGGEN=true
          echo "      → image_gen capability advertised (operator confirmed)"
          ;;
        *)
          echo "      → skipped"
          ;;
      esac
    fi
  fi
}

_configure_openclaw_otel() {
  local endpoint="$1"
  echo "      Running openclaw config set commands…"
  local cmds=(
    "diagnostics.enabled=true"
    "diagnostics.otel.enabled=true"
    "diagnostics.otel.traces=true"
    "diagnostics.otel.metrics=true"
    "diagnostics.otel.logs=true"
    "diagnostics.otel.protocol=http/protobuf"
    "diagnostics.otel.endpoint=$endpoint"
    "diagnostics.otel.serviceName=openclaw-gateway"
  )
  local fail=0
  for kv in "${cmds[@]}"; do
    local k="${kv%%=*}" v="${kv#*=}"
    if openclaw config set "$k" "$v" >/dev/null 2>&1; then
      echo "        ✓ $k = $v"
    else
      echo "        ✗ failed: openclaw config set $k \"$v\""
      fail=1
    fi
  done
  if [[ $fail -eq 0 ]]; then
    echo "      ✓ OpenClaw OTEL configured. Restart the gateway for changes to take effect."
  else
    echo "      ⚠ Some openclaw config commands failed — review above output."
  fi
}

if $INSTALL_LLAMA && [[ "$ROLE" != "auto" ]]; then
  _warn "--install-llama only applies with --role auto; ignoring for role=$ROLE"
fi

if [[ "$ROLE" == "auto" ]]; then
  echo
  echo "── Auto-detecting providers (role=auto) ────────────────────────────────"
  if $LLAMA_FLAG_EXPLICIT && ! $INSTALL_LLAMA; then
    echo
    echo "  • llama.cpp — skipping probe (--$([ "$ENABLE_LLAMA" = "true" ] && echo "enable" || echo "no")-llama set on CLI)"
  else
    _detect_llama
  fi
  if $LMS_FLAG_EXPLICIT; then
    echo
    echo "  • LM Studio — skipping probe (--$([ "$ENABLE_LMS" = "true" ] && echo "enable" || echo "no")-lms set on CLI)"
  else
    _detect_lms
  fi
  if $OPENCLAW_FLAG_EXPLICIT; then
    echo
    echo "  • OpenClaw — skipping probe (--$([ "$ENABLE_OPENCLAW" = "true" ] && echo "enable" || echo "no")-openclaw set on CLI)"
  else
    _detect_openclaw
  fi
  if ${IMGGEN_FLAG_EXPLICIT:-false}; then
    echo
    echo "  • Image Generation — skipping probe (--$([ "$ENABLE_IMGGEN" = "true" ] && echo "enable" || echo "no")-imggen set on CLI)"
  else
    _detect_imggen
  fi

  # Resolve role from what was found. The role label only encodes the
  # two inference providers (llama / lms); OpenClaw is observability and
  # doesn't shift the role.
  if $ENABLE_LLAMA && $ENABLE_LMS; then
    ROLE="mixed"
  elif $ENABLE_LLAMA; then
    ROLE="llama_host"
  elif $ENABLE_LMS; then
    ROLE="lms_host"
  else
    ROLE="system_only"
  fi
  echo
  echo "  → resolved role: $ROLE"

    if [[ "$AGENT_OS" == "linux" ]]; then
    echo
    echo "  • Self-monitor probes (manager / alarm engine)"
    if $MONITOR_MANAGER_FLAG_EXPLICIT; then
      echo "      manager:       skipping probe (--$([ "$ENABLE_MONITOR_MANAGER" = "true" ] && echo "enable" || echo "no")-monitor-manager set on CLI)"
    elif systemctl is-active --quiet llm-systems-manager.service 2>/dev/null; then
      ENABLE_MONITOR_MANAGER=true
      echo "      manager:       llm-systems-manager.service is active → enabling MONITOR_MANAGER_ENABLED"
    else
      echo "      manager:       llm-systems-manager.service not active → leaving MONITOR_MANAGER_ENABLED=false"
    fi
    if $MONITOR_ALARM_FLAG_EXPLICIT; then
      echo "      alarm engine:  skipping probe (--$([ "$ENABLE_MONITOR_ALARM" = "true" ] && echo "enable" || echo "no")-monitor-alarm set on CLI)"
    elif systemctl is-active --quiet llm-systems-alarm-engine.service 2>/dev/null; then
      ENABLE_MONITOR_ALARM=true
      echo "      alarm engine:  llm-systems-alarm-engine.service is active → enabling MONITOR_ALARM_ENGINE_ENABLED"
    else
      echo "      alarm engine:  llm-systems-alarm-engine.service not active → leaving MONITOR_ALARM_ENGINE_ENABLED=false"
    fi
    if $MONITOR_INFLUXDB_DISK_FLAG_EXPLICIT; then
      echo "      influxdb disk: skipping probe (--$([ "$ENABLE_MONITOR_INFLUXDB_DISK" = "true" ] && echo "enable" || echo "no")-monitor-influxdb-disk set on CLI)"
    elif systemctl is-active --quiet influxdb.service 2>/dev/null; then
      if systemctl is-active --quiet llm-systems-alarm-engine.service 2>/dev/null; then
        ENABLE_MONITOR_INFLUXDB_DISK=false
        echo "      influxdb disk: alarm engine is colocated → leaving MONITOR_INFLUXDB_DISK_ENABLED=false (AE's influx_monitor owns the probe here)"
      else
        ENABLE_MONITOR_INFLUXDB_DISK=true
        echo "      influxdb disk: influxdb.service is active without AE → enabling MONITOR_INFLUXDB_DISK_ENABLED (agent is the only probe source)"
      fi
    else
      ENABLE_MONITOR_INFLUXDB_DISK=false
      echo "      influxdb disk: influxdb.service not active → leaving MONITOR_INFLUXDB_DISK_ENABLED=false"
    fi
  fi
  echo "─────────────────────────────────────────────────────────────────────────"
fi

COLLECT_GPU=false
COLLECT_SENSORS=false
COLLECT_LIQUIDCTL=false
COLLECT_UPS=false
COLLECT_ISCSI=false

# _offer_apt_install LABEL PKGS BINARY DESCRIPTION
#   Returns 0 if BINARY ends up on PATH (already there OR installed now).
#   Returns 1 if missing AND operator declined / non-interactive / apt fails.
_offer_apt_install() {
  local label="$1" pkgs="$2" binary="$3" description="$4"
  if command -v "$binary" >/dev/null 2>&1; then
    return 0
  fi
  if $SKIP_PREREQ_INSTALL; then
    echo "    $label: '$binary' missing, --no-prereq-install set → skipping"
    return 1
  fi
  if [[ ! -t 0 ]]; then
    echo "    $label: '$binary' missing, no TTY → skipping (apt-get install $pkgs to enable)"
    return 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "    $label: '$binary' missing and apt-get not available → skipping"
    return 1
  fi
  echo "    $label: '$binary' not installed — $description"
  read -rp "      Install $pkgs now? [Y/n] " _ans
  case "$(printf '%s' "$_ans" | tr '[:upper:]' '[:lower:]')" in
    ""|y|yes) ;;
    *) echo "      declined — leaving disabled"; return 1 ;;
  esac
  # shellcheck disable=SC2086
  if $SUDO apt-get install -y $pkgs >/dev/null 2>&1; then
    hash -r 2>/dev/null || true
    if command -v "$binary" >/dev/null 2>&1; then
      echo "      ✓ installed"
      return 0
    fi
    echo "      ✗ apt reported success but '$binary' still not on PATH"
    return 1
  fi
  echo "      ✗ apt-get install $pkgs failed — run manually and re-run installer"
  return 1
}

if [[ "$AGENT_OS" == "linux" ]] && [[ "$ROLE" != "system_only" ]]; then
  echo
  echo "── Host hardware collectors ────────────────────────────────────────────"
  # GPU: PCI vendor 0x1002 (AMD) or 0x10de (NVIDIA) on any DRM card.
  # AMD reads pure sysfs (no binary). NVIDIA needs nvidia-smi from
  # nvidia-utils. We offer to install nvidia-utils; installing the
  # kernel driver itself is out of scope (needs reboot, version matching).
  _gpu_vendor=""
  for _v in /sys/class/drm/card[0-9]*/device/vendor; do
    [[ -f "$_v" ]] || continue
    case "$(cat "$_v" 2>/dev/null)" in
      0x1002) _gpu_vendor=AMD;    break ;;
      0x10de) _gpu_vendor=NVIDIA; break ;;
    esac
  done
  case "$_gpu_vendor" in
    AMD)
      COLLECT_GPU=true
      echo "    GPU:         AMD detected → COLLECT_GPU_ENABLED=true (sysfs, no binary needed)"
      ;;
    NVIDIA)
      if _offer_apt_install "GPU" "nvidia-utils-535" "nvidia-smi" \
           "needed to read NVIDIA GPU metrics (temperature, VRAM, power, util)"; then
        COLLECT_GPU=true
        echo "    GPU:         NVIDIA detected + nvidia-smi available → COLLECT_GPU_ENABLED=true"
      else
        echo "    GPU:         NVIDIA detected but nvidia-smi missing → COLLECT_GPU_ENABLED=false"
      fi
      ;;
    *)
      echo "    GPU:         no AMD/NVIDIA card → COLLECT_GPU_ENABLED=false"
      ;;
  esac

  if _offer_apt_install "lm-sensors" "lm-sensors" "sensors" \
       "reads CPU / motherboard temperature, voltage, and fan sensors"; then
    if sensors -j 2>/dev/null | grep -q '[^[:space:]]'; then
      COLLECT_SENSORS=true
      echo "    lm-sensors:  installed and reporting → COLLECT_SENSORS_ENABLED=true"
    else
      echo "    lm-sensors:  installed but no sensors detected — run 'sudo sensors-detect' first → COLLECT_SENSORS_ENABLED=false"
    fi
  fi

  if _offer_apt_install "liquidctl" "liquidctl" "liquidctl" \
       "controls NZXT Kraken AIOs, Corsair PSUs, NZXT Smart Device V2, etc."; then
    # Binary present — only enable if a supported device is actually wired up.
    # The collector reads Kraken / HX1000i / Smart Device, so gate on those.
    if $SUDO liquidctl list 2>/dev/null | grep -qiE 'kraken|hx1000i|smart device'; then
      COLLECT_LIQUIDCTL=true
      echo "    liquidctl:   supported device found → COLLECT_LIQUIDCTL_ENABLED=true"
    else
      echo "    liquidctl:   installed, no Kraken/HX1000i/Smart Device → COLLECT_LIQUIDCTL_ENABLED=false"
    fi
  fi

  if command -v upower >/dev/null 2>&1 && upower -e 2>/dev/null | grep -qi ups; then
    COLLECT_UPS=true
    echo "    UPS:         upower reports a UPS → COLLECT_UPS_ENABLED=true"
  else
    echo "    UPS:         no UPS via upower → COLLECT_UPS_ENABLED=false"
  fi
  if [[ -d /sys/class/iscsi_session ]] && [[ -n "$(ls -A /sys/class/iscsi_session 2>/dev/null)" ]]; then
    COLLECT_ISCSI=true
    echo "    iSCSI:       active session(s) → COLLECT_ISCSI_ENABLED=true"
  else
    echo "    iSCSI:       no active sessions → COLLECT_ISCSI_ENABLED=false"
  fi
  echo "─────────────────────────────────────────────────────────────────────────"
fi

_hw_monitoring=false
if $COLLECT_GPU || $COLLECT_SENSORS || $COLLECT_LIQUIDCTL; then
  _hw_monitoring=true
fi
if [[ "$AGENT_OS" == "linux" ]] \
   && ! $PERF_FLAG_EXPLICIT \
   && $_hw_monitoring \
   && { [[ "$ENABLE_LLAMA" == "true" ]] || [[ "$ENABLE_LMS" == "true" ]]; }; then
  if [[ "$ENABLE_LLAMA" == "true" && "$ENABLE_LMS" == "true" ]]; then
    _providers="llama + lms"
  elif [[ "$ENABLE_LLAMA" == "true" ]]; then
    _providers="llama"
  else
    _providers="lms"
  fi
  echo
  echo "── Perf controller (optional) ──────────────────────────────────────────"
  echo "  This host has hardware monitoring enabled and runs $_providers."
  echo "  The perf controller switches CPU governor + GPU power profile when"
  echo "  llama-server transitions between awake/sleeping, saving power when"
  echo "  idle and ramping for inference. Requires llama-server (LLM Studio"
  echo "  hosts won't see transitions and the controller will idle)."
  echo "  Caveat: runs 'sudo systemctl reload-or-restart performance|powersave'"
  echo "  on each transition — modifies host state."
  if [[ -t 0 ]]; then
    read -rp "  Enable PERF_CONTROLLER_ENABLED? [y/N] " _ans
    case "$(printf '%s' "$_ans" | tr '[:upper:]' '[:lower:]')" in
      y|yes) ENABLE_PERF=true;  echo "  ✓ enabled" ;;
      *)     ENABLE_PERF=false; echo "  ✗ left disabled" ;;
    esac
  else
    echo "  (no TTY — leaving disabled; pass --enable-perf to opt in non-interactively)"
  fi
  echo "─────────────────────────────────────────────────────────────────────────"
fi

# Auto-install the example perf units when the controller is enabled so it has
# targets to trigger; refuse-to-overwrite (section 7) keeps tuned hosts safe (#138).
if $ENABLE_PERF && [[ "$AGENT_OS" == "linux" ]] && ! $INSTALL_PERF_UNITS; then
  INSTALL_PERF_UNITS=true
  echo "  ⓘ perf controller enabled → will install example performance/powersave units"
fi

echo
echo "── LLM Systems Agent install Summary ─────────────────────────────────────────"
echo "  OS:           $AGENT_OS"
echo "  user:         $USER_ARG"
echo "  install-dir:  $INSTALL_DIR"
echo "  hostname:     ${HOSTNAME_OVERRIDE:-$(hostname) (auto)}"
echo "  description:  ${DESCRIPTION_OVERRIDE:-(none)}"
echo "  manager-url:  $MANAGER_URL"
echo "  alarm-url:    $(_alarm_engine_url_from_manager)"
echo "  role:         $ROLE"
echo "  enable-perf:  $ENABLE_PERF"
echo "  perf-units:   $INSTALL_PERF_UNITS"
echo "  enable-llama: $ENABLE_LLAMA"
echo "  enable-lms:   $ENABLE_LMS"
echo "  enable-openclaw: $ENABLE_OPENCLAW"
echo "  enable-imggen:   $ENABLE_IMGGEN"
echo "  monitor-manager: $ENABLE_MONITOR_MANAGER"
echo "  monitor-alarm:   $ENABLE_MONITOR_ALARM"
echo "  collect-gpu:     $COLLECT_GPU"
echo "  collect-sensors: $COLLECT_SENSORS"
echo "  collect-liquidctl: $COLLECT_LIQUIDCTL"
echo "  collect-ups:     $COLLECT_UPS"
echo "  collect-iscsi:   $COLLECT_ISCSI"
echo "  skip-sudoers: $SKIP_SUDOERS"
echo "  skip-service: $SKIP_SERVICE"
echo "─────────────────────────────────────────────────────────────────────────"
echo
echo "── Checking prerequisites ───────────────────────────────────────────────"

PREREQ_MISSING=()             # human-friendly list for display
APT_PKGS=()                   # what to install via apt
DNF_PKGS=()                   # what to install via dnf
BREW_PKGS=()                  # what to install via brew

PYTHON3=""
_is_modern_python() {
  local cand="$1" v=""
  [[ -n "$cand" && -x "$cand" ]] || return 1
  v="$("$cand" -c 'import sys
if sys.version_info >= (3, 10):
    print(f"{sys.version_info.major}.{sys.version_info.minor}")
' 2>/dev/null)" || return 1
  [[ -n "$v" ]] || return 1
  echo "$v"
}

_python3_candidates=(
  "${PYTHON3:-}"
  "$(command -v python3 2>/dev/null || true)"
  "$(command -v python3.13 2>/dev/null || true)"
  "$(command -v python3.12 2>/dev/null || true)"
  "$(command -v python3.11 2>/dev/null || true)"
  "$(command -v python3.10 2>/dev/null || true)"
  /opt/homebrew/bin/python3
  /opt/homebrew/bin/python3.13
  /opt/homebrew/bin/python3.12
  /opt/homebrew/bin/python3.11
  /opt/homebrew/bin/python3.10
  /usr/local/bin/python3
  /usr/local/bin/python3.13
  /usr/local/bin/python3.12
  /usr/local/bin/python3.11
  /usr/local/bin/python3.10
  /usr/bin/python3
)
PY_FOUND_VER=""
for _cand in "${_python3_candidates[@]}"; do
  [[ -n "$_cand" ]] || continue
  if PY_FOUND_VER="$(_is_modern_python "$_cand")"; then
    PYTHON3="$_cand"
    break
  fi
done
unset _python3_candidates _cand

if [[ -z "$PYTHON3" ]]; then
  # Nothing modern enough was found. Decide what to install.
  PY_VER_DETECTED="?"
  if command -v python3 >/dev/null 2>&1; then
    PY_VER_DETECTED="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "?")"
  fi
  PREREQ_MISSING+=("python3 >= 3.10 (system python3 = $PY_VER_DETECTED)")
  APT_PKGS+=("python3" "python3-venv" "python3-pip")
  DNF_PKGS+=("python3" "python3-pip")
  BREW_PKGS+=("python@3.12")
else
  echo "  ✓ python3 $PY_FOUND_VER at: $PYTHON3"
  _probe_dir="$(mktemp -d -t lsa-venv-probe.XXXXXX)"
  if ! "$PYTHON3" -m venv "$_probe_dir/v" >/dev/null 2>&1; then
    PREREQ_MISSING+=("python3 venv (build a venv with bundled pip)")
    APT_PKGS+=("python3-venv" "python3-pip")
    DNF_PKGS+=("python3-pip")
  fi
  rm -rf "$_probe_dir"
  unset _probe_dir
fi

if [[ "$AGENT_OS" == "linux" && "$SKIP_SUDOERS" == "false" \
      && "$ENABLE_LLAMA" != "true" && "$ENABLE_PERF" != "true" ]]; then
  echo "  ⓘ skipping sudoers (no llama, no perf controller — nothing for the agent to sudo)"
  SKIP_SUDOERS=true
fi

# Linux-specific: visudo (for sudoers validation), systemctl
if [[ "$AGENT_OS" == "linux" ]]; then
  if ! $SKIP_SUDOERS && ! command -v visudo >/dev/null 2>&1; then
    PREREQ_MISSING+=("visudo (sudo package)")
    APT_PKGS+=("sudo")
    DNF_PKGS+=("sudo")
  fi
  if ! $SKIP_SERVICE && ! command -v systemctl >/dev/null 2>&1; then
    PREREQ_MISSING+=("systemctl (systemd)")
    # Both apt + dnf already include systemd in base; if it's missing, the
    # VM is unusual enough that we punt.
    SKIP_PREREQ_INSTALL=true
  fi
fi

# Dedupe pkg lists
if (( ${#APT_PKGS[@]} )); then
  IFS=$'\n' read -r -d '' -a APT_PKGS < <(printf "%s\n" "${APT_PKGS[@]}" | sort -u && printf '\0')
fi
if (( ${#DNF_PKGS[@]} )); then
  IFS=$'\n' read -r -d '' -a DNF_PKGS < <(printf "%s\n" "${DNF_PKGS[@]}" | sort -u && printf '\0')
fi
if (( ${#BREW_PKGS[@]} )); then
  IFS=$'\n' read -r -d '' -a BREW_PKGS < <(printf "%s\n" "${BREW_PKGS[@]}" | sort -u && printf '\0')
fi

if (( ${#PREREQ_MISSING[@]} == 0 )); then
  echo "  ✓ all prerequisites present"
else
  echo "  ✗ missing prerequisites:"
  printf "      - %s\n" "${PREREQ_MISSING[@]}"

  # Pick the install command for this distro. Prefix with $SUDO (empty when
  # already root) so installing the sudo package itself works on a root box
  # that has no sudo — `sudo apt-get install sudo` would be a chicken-and-egg.
  _S="${SUDO:+$SUDO }"
  INSTALL_CMD=""
  if [[ "$AGENT_OS" == "linux" ]]; then
    if [[ -f /etc/debian_version ]] && command -v apt-get >/dev/null 2>&1 && (( ${#APT_PKGS[@]} )); then
      INSTALL_CMD="${_S}apt-get update && ${_S}apt-get install -y ${APT_PKGS[*]}"
    elif [[ -f /etc/redhat-release ]] && command -v dnf >/dev/null 2>&1 && (( ${#DNF_PKGS[@]} )); then
      INSTALL_CMD="${_S}dnf install -y ${DNF_PKGS[*]}"
    elif command -v yum >/dev/null 2>&1 && (( ${#DNF_PKGS[@]} )); then
      INSTALL_CMD="${_S}yum install -y ${DNF_PKGS[*]}"
    fi
  elif [[ "$AGENT_OS" == "macos" ]]; then
    if command -v brew >/dev/null 2>&1 && (( ${#BREW_PKGS[@]} )); then
      INSTALL_CMD="brew install ${BREW_PKGS[*]}"
    fi
  fi

  if $SKIP_PREREQ_INSTALL || [[ -z "$INSTALL_CMD" ]]; then
    echo
    echo "  Cannot auto-install on this system. Please install manually and re-run."
    if [[ -n "$INSTALL_CMD" ]]; then
      echo "  Suggested:  $INSTALL_CMD"
    fi
    exit 1
  fi

  echo
  echo "  The installer can run this for you:"
  echo "      $INSTALL_CMD"
  echo
  echo "  System packages will only be installed with your explicit consent."
  echo "  -y/--yes does NOT cover this — touching system packages always asks."

  # Refuse to auto-install in any non-interactive context. We require the
  # user to type the confirmation themselves so the installer never silently
  # touches system-package state.
  if [[ ! -t 0 ]]; then
    echo
    echo "ERROR: stdin is not a TTY — cannot prompt for prereq-install consent." >&2
    echo "       Run interactively, or install the packages manually:" >&2
    echo "         $INSTALL_CMD" >&2
    exit 1
  fi
  read -rp "  Install missing prerequisites now? [Y/n] " REPLY
  case "$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')" in
    ""|y|yes)
      echo "  Running prereq install…"
      eval "$INSTALL_CMD"
      hash -r 2>/dev/null || true

      PYTHON3=""
      PY_FOUND_VER=""
      _post_candidates=(
        "$(command -v python3 2>/dev/null || true)"
        "$(command -v python3.13 2>/dev/null || true)"
        "$(command -v python3.12 2>/dev/null || true)"
        "$(command -v python3.11 2>/dev/null || true)"
        "$(command -v python3.10 2>/dev/null || true)"
        /opt/homebrew/bin/python3
        /opt/homebrew/bin/python3.13
        /opt/homebrew/bin/python3.12
        /opt/homebrew/bin/python3.11
        /opt/homebrew/bin/python3.10
        /usr/local/bin/python3
        /usr/local/bin/python3.13
        /usr/local/bin/python3.12
        /usr/local/bin/python3.11
        /usr/local/bin/python3.10
        /usr/bin/python3
      )
      _probe_dir="$(mktemp -d -t lsa-venv-recheck.XXXXXX)"
      _last_probe_err=""
      for _candidate in "${_post_candidates[@]}"; do
        [[ -n "$_candidate" ]] || continue
        # Must be modern enough first; cheap test.
        if ! PY_FOUND_VER="$(_is_modern_python "$_candidate")"; then
          continue
        fi
        # Then must build a venv successfully.
        if _last_probe_err="$("$_candidate" -m venv "$_probe_dir/v" 2>&1 >/dev/null)"; then
          PYTHON3="$_candidate"
          rm -rf "$_probe_dir/v"
          break
        fi
        rm -rf "$_probe_dir/v" 2>/dev/null || true
      done
      rm -rf "$_probe_dir"
      unset _probe_dir _post_candidates
      if [[ -z "$PYTHON3" ]]; then
        echo "ERROR: prereq install completed but no python3 ≥ 3.10 with a working venv was found." >&2
        echo "       Searched PATH + /opt/homebrew/bin/* + /usr/local/bin/* + /usr/bin/python3" >&2
        if [[ -n "$_last_probe_err" ]]; then
          echo "       Last error from venv probe:" >&2
          printf '         %s\n' "$_last_probe_err" >&2
        fi
        if [[ "$AGENT_OS" == "macos" ]]; then
          echo "       On macOS, install Homebrew + python@3.12 then re-run:" >&2
          echo "         brew install python@3.12" >&2
          echo "       (you may also need to add /opt/homebrew/bin to PATH)" >&2
        else
          echo "       On Debian/Ubuntu, install python3-venv + python3-pip + a 3.10+ python:" >&2
          echo "         sudo apt-get install -y python3 python3-venv python3-pip" >&2
        fi
        exit 1
      fi
      PY_FOUND="$PYTHON3"  # legacy var name retained for the success-message line below
      _ok "prerequisites installed (python3 at: $PY_FOUND)"
      ;;
    *)
      echo "  Aborted by user. Install the prerequisites manually and re-run."
      exit 1
      ;;
  esac
fi
echo "──────────────────────────────────────────────────────────────────"

if [[ "$AGENT_OS" == "linux" ]] && [[ "$ROLE" == "llama_host" || "$ROLE" == "mixed" ]]; then
  _section "Optional host-metric tooling"
  _missing_hw=()
  command -v sensors    >/dev/null 2>&1 || _missing_hw+=("lm-sensors")
  command -v liquidctl  >/dev/null 2>&1 || _missing_hw+=("liquidctl")
  command -v upower     >/dev/null 2>&1 || _missing_hw+=("upower")
  if [[ ${#_missing_hw[@]} -eq 0 ]]; then
    _ok "all optional host-metric tools present"
  else
    _warn "missing: ${_missing_hw[*]} — corresponding fields will be null"
    echo "    Install later with: sudo apt-get install -y ${_missing_hw[*]}"
  fi
  unset _missing_hw
fi

USER_GROUP="$(id -gn "$USER_ARG" 2>/dev/null || true)"
if [[ -z "$USER_GROUP" ]]; then
  echo "ERROR: could not resolve primary group for user '$USER_ARG'" >&2
  exit 1
fi

USER_HOME="$(eval echo "~$USER_ARG" 2>/dev/null || true)"
if [[ -z "$USER_HOME" || "$USER_HOME" == "~$USER_ARG" ]]; then
  if command -v getent >/dev/null 2>&1; then
    USER_HOME="$(getent passwd "$USER_ARG" 2>/dev/null | cut -d: -f6 || true)"
  elif command -v dscl >/dev/null 2>&1; then
    USER_HOME="$(dscl . -read "/Users/$USER_ARG" NFSHomeDirectory 2>/dev/null \
                 | awk '{print $2}')"
  fi
fi
if [[ -z "$USER_HOME" ]]; then
  echo "ERROR: could not resolve home directory for user '$USER_ARG'" >&2
  exit 1
fi

# 1. Create install dir
$SUDO mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
$SUDO chown -R "$USER_ARG:$USER_GROUP" "$INSTALL_DIR"

if [[ "$AGENT_OS" == "linux" ]]; then
  $SUDO mkdir -p /var/log/llm-systems-manager
  $SUDO chown "$USER_ARG:$USER_GROUP" /var/log/llm-systems-manager
fi

# 2. Copy agent + buffered_metric_client + shared helpers + collectors/ + providers/.
# Packages first so partial-cp leaves the OLD agent.py in place (fresh-install:
# nothing in place, but same code shape as the --update path above).
_section "Deploying agent code"
for _pkg in collectors providers; do
  [[ -d "$SRC_DIR/$_pkg" ]] || { echo "ERROR: $SRC_DIR/$_pkg missing — refusing to wipe $INSTALL_DIR/$_pkg" >&2; exit 1; }
  $SUDO rm -rf "$INSTALL_DIR/$_pkg"
  $SUDO cp -r "$SRC_DIR/$_pkg" "$INSTALL_DIR/$_pkg"
  $SUDO chown -R "$USER_ARG:$USER_GROUP" "$INSTALL_DIR/$_pkg"
done
$SUDO cp "$SRC_DIR/llm-systems-agent.py"      "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/buffered_metric_client.py" "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/_utils.py"                 "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/_best_effort.py"           "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/agent_context.py"          "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/stream_pool.py"            "$INSTALL_DIR/"
$SUDO cp "$SRC_DIR/unified_config_reader.py"  "$INSTALL_DIR/"
$SUDO chown "$USER_ARG:$USER_GROUP" \
  "$INSTALL_DIR/llm-systems-agent.py" \
  "$INSTALL_DIR/buffered_metric_client.py" \
  "$INSTALL_DIR/_utils.py" \
  "$INSTALL_DIR/_best_effort.py" \
  "$INSTALL_DIR/stream_pool.py" \
  "$INSTALL_DIR/agent_context.py" \
  "$INSTALL_DIR/unified_config_reader.py"
_ok "agent code installed to $INSTALL_DIR"

# 3. Drop config from example if missing
if [[ ! -f "$INSTALL_DIR/agent_config.yaml" ]]; then
  $SUDO cp "$SRC_DIR/agent_config.yaml.example" "$INSTALL_DIR/agent_config.yaml"
  $SUDO chown "$USER_ARG:$USER_GROUP" "$INSTALL_DIR/agent_config.yaml"
  _ok "agent_config.yaml rendered from example"
  # Substitute identity + provider toggles + any per-provider overrides
  # collected during the auto-detect prompts.
  ALARM_ENGINE_URL="$(_alarm_engine_url_from_manager)"
  # Hardware-collector detection results passed via env so the python
  # writer doesn't need yet another positional arg per flag.
  $SUDO env \
    COLLECT_GPU_DET="$COLLECT_GPU" \
    COLLECT_SENSORS_DET="$COLLECT_SENSORS" \
    COLLECT_LIQUIDCTL_DET="$COLLECT_LIQUIDCTL" \
    COLLECT_UPS_DET="$COLLECT_UPS" \
    COLLECT_ISCSI_DET="$COLLECT_ISCSI" \
    python3 - "$INSTALL_DIR/agent_config.yaml" \
    "$AGENT_OS" "$ROLE" "$MANAGER_URL" "$ALARM_ENGINE_URL" \
    "$USER_ARG" "$INSTALL_DIR" "$HOSTNAME_OVERRIDE" "$DESCRIPTION_OVERRIDE" \
    "$ENABLE_PERF" "$ENABLE_LLAMA" "$ENABLE_LMS" "$ENABLE_OPENCLAW" "$ENABLE_IMGGEN" \
    "$LLAMA_API_URL_OVERRIDE" "$LLAMA_LOG_FILE_OVERRIDE" "$LLAMA_SYSTEMD_UNIT_OVERRIDE" \
    "$LLAMA_BIN_OVERRIDE" "$LLAMA_CONFIG_INI_OVERRIDE" "$LLAMA_BUILD_METHOD_OVERRIDE" \
    "$LMS_API_URL_OVERRIDE" "$LMS_CMD_OVERRIDE" \
    "$OPENCLAW_AGENTS_DIR_OVERRIDE" \
    "$ENABLE_MONITOR_MANAGER" "$ENABLE_MONITOR_ALARM" \
    "${ENABLE_MONITOR_INFLUXDB_DISK:-false}" <<'PYEOF'
import sys, re

(path, agent_os, role, mgr_url, alarm_url,
 agent_user, install_dir, hostname, description,
 perf, llama, lms, openclaw, imggen,
 llama_api, llama_log, llama_unit,
 llama_bin, llama_ini, llama_build_method,
 lms_api, lms_cmd,
 oc_dir,
 monitor_manager, monitor_alarm,
 monitor_influxdb_disk) = sys.argv[1:]

text = open(path).read()

def _set(key, value):
    """Replace existing 'KEY: <anything>' line with the new value, unquoted.

    Also matches a commented '# KEY: ...' line and uncomments + sets it.
    Without this, keys that the example file ships commented (so YAML
    defaults to the agent's class defaults) would never get picked up
    by the installer's prompt overrides.
    """
    global text
    pattern = rf'^[#\s]*{re.escape(key)}:[^\n]*'
    new_line = f'{key}: {value}'
    if re.search(pattern, text, flags=re.M):
        text = re.sub(pattern, new_line, text, count=1, flags=re.M)
    else:
        # Key not in file at all — append.
        text = text.rstrip() + f"\n{new_line}\n"

def _set_quoted(key, value):
    """Quoted variant — same semantics as _set but wraps value in double quotes."""
    _set(key, f'"{value}"')

# Identity
_set('AGENT_OS',   agent_os)
_set('AGENT_ROLE', role)
_set_quoted('AGENT_USER',         agent_user)
_set_quoted('AGENT_INSTALL_DIR',  install_dir)
if hostname:
    _set_quoted('AGENT_HOSTNAME', hostname)
if description:
    _set_quoted('AGENT_DESCRIPTION', description)
_set_quoted('MANAGER_URL',      mgr_url)
_set_quoted('ALARM_ENGINE_URL', alarm_url)

# Provider toggles
_set('PERF_CONTROLLER_ENABLED', perf.lower())
_set('LLAMA_ENABLED',           llama.lower())
_set('LMS_ENABLED',             lms.lower())
_set('OPENCLAW_ENABLED',        openclaw.lower())
_set('IMGGEN_ENABLED',          imggen.lower())

# Self-monitor probe toggles — independent of role. Auto-detect in the
# install script flips these on when the matching systemd unit is active
# on the install target.
_set('MONITOR_MANAGER_ENABLED',      monitor_manager.lower())
_set('MONITOR_ALARM_ENGINE_ENABLED', monitor_alarm.lower())
_set('MONITOR_INFLUXDB_DISK_ENABLED', monitor_influxdb_disk.lower())

# powermetrics is macOS-only — the Linux helper would just respawn-and-die.
_set('COLLECT_POWERMETRICS_ENABLED', 'true' if agent_os == 'macos' else 'false')

# Host hardware collectors — values come from install-time probes (see
# the "Host hardware collectors" banner above). Default false when env
# vars aren't set, which is the right thing on macOS (no Linux helpers).
import os
_set('COLLECT_GPU_ENABLED',       os.environ.get('COLLECT_GPU_DET',       'false'))
_set('COLLECT_SENSORS_ENABLED',   os.environ.get('COLLECT_SENSORS_DET',   'false'))
_set('COLLECT_LIQUIDCTL_ENABLED', os.environ.get('COLLECT_LIQUIDCTL_DET', 'false'))
_set('COLLECT_UPS_ENABLED',       os.environ.get('COLLECT_UPS_DET',       'false'))
_set('COLLECT_ISCSI_ENABLED',     os.environ.get('COLLECT_ISCSI_DET',     'false'))

# Discover which monitorable services are actually installed on THIS
# host. Used to (a) customize PROCESS_WATCHLIST so we only check what
# exists, and (b) seed LOG_WATCH_RULES with commented stubs for the
# matching log files. Probe systemd unit-file paths — covers
# /etc/systemd/system (operator/install), /lib/systemd/system (distro),
# and /usr/lib/systemd/system (modern Debian/Ubuntu).
import os
def _unit_present(unit):
    for d in ('/etc/systemd/system', '/lib/systemd/system', '/usr/lib/systemd/system'):
        if os.path.isfile(os.path.join(d, unit)):
            return True
    return False
has_mgr     = _unit_present('llm-systems-manager.service')
has_ae      = _unit_present('llm-systems-alarm-engine.service')
has_influx  = _unit_present('influxdb.service')
has_llama   = _unit_present(llama_unit or 'llama_server.service')

# PROCESS_WATCHLIST — only watch units that exist locally. Alarm rules
# like "<name>_running == 0" then fire only when the local copy crashes,
# avoiding spurious alerts about services that don't live here.
watchlist = []
if has_mgr:     watchlist.append(('manager',      'llm-systems-manager.service'))
if has_ae:      watchlist.append(('alarm-engine', 'llm-systems-alarm-engine.service'))
if has_influx:  watchlist.append(('influxdb',     'influxdb.service'))
if has_llama:   watchlist.append(('llama-server', llama_unit or 'llama_server.service'))
if watchlist:
    yaml_block = "PROCESS_WATCHLIST:\n" + "\n".join(
        f"  - name: {name}\n    match: systemd\n    unit: {unit}"
        for name, unit in watchlist
    )
    # Match the key line + any commented/indented continuation lines so we
    # replace the whole template block — not just the header.
    wl_pattern = r'^[#\s]*PROCESS_WATCHLIST:[^\n]*(?:\n(?:#\s{2,}.*|\s{2,}.*))*'
    if re.search(wl_pattern, text, flags=re.M):
        text = re.sub(wl_pattern, yaml_block, text, count=1, flags=re.M)
    else:
        text = text.rstrip() + '\n\n' + yaml_block + '\n'

# LOG_WATCH_RULES — emit commented-out rules for each locally-installed
# component. Operators flip LOG_WATCH_ENABLED to true and uncomment the
# rules they want active. Skip entirely when no monitored component is
# installed (DB-only host with no influxd log routing → nothing useful).
def _log_rule(name, path, pattern, severity, cooldown, message):
    return (f"#   - name: {name}\n"
            f"#     path: {path}\n"
            f"#     pattern: {pattern!r}\n"
            f"#     severity: {severity}\n"
            f"#     cooldown_s: {cooldown}\n"
            f"#     message: {message!r}\n")
rules_for_host = []
if has_mgr:
    rules_for_host.append(_log_rule(
        "manager-error",
        "/var/log/llm-systems-manager/llm-systems-manager.log",
        r"\[(ERROR|CRITICAL)\]",
        "critical", 300, "manager log: {line}",
    ))
    rules_for_host.append(_log_rule(
        "manager-traceback",
        "/var/log/llm-systems-manager/llm-systems-manager.log",
        r"^Traceback \(most recent call last\)",
        "critical", 300, "manager traceback: {line}",
    ))
if has_ae:
    rules_for_host.append(_log_rule(
        "ae-error",
        "/var/log/llm-systems-manager/llm-systems-alarm-engine.log",
        r"\[(ERROR|CRITICAL)\]",
        "critical", 300, "alarm-engine log: {line}",
    ))
    rules_for_host.append(_log_rule(
        "ae-influx-write-fail",
        "/var/log/llm-systems-manager/llm-systems-alarm-engine.log",
        r"(?i)influx.*(write.*fail|unavailable|circuit.*open)",
        "critical", 600, "alarm-engine: {line}",
    ))
if has_influx:
    # influxd usually routes through journald → /var/log/syslog on Ubuntu.
    rules_for_host.append(_log_rule(
        "influxdb-error",
        "/var/log/syslog",
        r"(?i)influxd.*?(error|panic|fatal|out of disk)",
        "critical", 300, "influxdb: {line}",
    ))
if has_llama:
    # Operator-supplied path wins; otherwise the agent's default.
    llama_log_path = llama_log or "/usr/local/llama-server/llama-server.log"
    rules_for_host.append(_log_rule(
        "llama-oom",
        llama_log_path,
        r"(?i)cuda out of memory|ggml_cuda.*out of memory|HIP out of memory",
        "critical", 300, "llama-server OOM: {line}",
    ))
    rules_for_host.append(_log_rule(
        "llama-fatal",
        llama_log_path,
        r"(?i)\bfatal\b|GGML_ASSERT|terminate called",
        "critical", 300, "llama-server fatal: {line}",
    ))
    # AMD GPU reset/lockup events land in dmesg → syslog, not llama-server's
    # log. Only useful on AMD hosts; the rule is harmless on others (just
    # won't match anything).
    rules_for_host.append(_log_rule(
        "amdgpu-reset",
        "/var/log/syslog",
        r"(?i)amdgpu.*(GPU\s+(smu\s+)?mode\d+\s+reset|ring\s+\S+\s+timeout|gpu hang)",
        "critical", 600, "amdgpu reset/hang: {line}",
    ))
if rules_for_host:
    # Header comment explains what this block is + how to activate; the
    # `# LOG_WATCH_RULES:` line below it is the actual YAML scaffold
    # the operator uncomments.
    header = ("# Installer-seeded log-watch rules for components detected on this\n"
              "# host. To enable: set LOG_WATCH_ENABLED: true above, then uncomment\n"
              "# the LOG_WATCH_RULES: line AND every rule line below it (drop the\n"
              "# leading '# '). Indent is significant — keep the two-space indent\n"
              "# before the leading '-' on each rule, four-space on rule fields.\n")
    new_block = header + "# LOG_WATCH_RULES:\n" + "".join(rules_for_host)
    # Replace the example's commented LOG_WATCH_RULES block (KEY line +
    # any following commented continuation lines).
    lw_pattern = r'^# LOG_WATCH_RULES:[^\n]*(?:\n#[^\n]*)*'
    if re.search(lw_pattern, text, flags=re.M):
        text = re.sub(lw_pattern, new_block, text, count=1, flags=re.M)

# Per-provider overrides (only if non-empty)
if llama_bin:   _set_quoted('LLAMA_BIN',          llama_bin)
if llama_ini:           _set_quoted('LLAMA_CONFIG_INI',   llama_ini)
if llama_build_method:  _set_quoted('LLAMA_BUILD_METHOD', llama_build_method)
if llama_log:   _set_quoted('LLAMA_LOG_FILE',     llama_log)
if llama_api:   _set_quoted('LLAMA_API_URL',      llama_api)
if llama_unit:  _set_quoted('LLAMA_SYSTEMD_UNIT', llama_unit)
if lms_api:     _set_quoted('LMS_API_URL', lms_api)
if lms_cmd:     _set_quoted('LMS_CMD',     lms_cmd)
if oc_dir:      _set_quoted('OPENCLAW_AGENTS_DIR', oc_dir)

open(path, 'w').write(text)
PYEOF
else
  _warn "existing $INSTALL_DIR/agent_config.yaml — leaving untouched"
  echo "    Prompts from this install were NOT applied. Pass --update to merge new keys,"
  echo "    or edit the file then: systemctl restart llm-systems-agent"
fi

$SUDO mkdir -p "$INSTALL_DIR/src"
$SUDO chown "$USER_ARG:$USER_GROUP" "$INSTALL_DIR/src"

: "${PYTHON3:=$(command -v python3)}"
_run_as "$USER_ARG" "$PYTHON3" -m venv "$INSTALL_DIR/venv"
_pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
_pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$TMPL_DIR/requirements.txt"

if $ENABLE_MONITOR_ALARM; then
  _pip_filter _run_as "$USER_ARG" "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$TMPL_DIR/requirements-monitor.txt"
fi

if $ENABLE_LLAMA; then
  _ensure_hf_cli "$USER_ARG" "$USER_HOME"
fi

# 5. systemd unit (Linux) / launchd plist (macOS)
if ! $SKIP_SERVICE; then
  if [[ "$AGENT_OS" == "linux" ]]; then
    _section "Installing systemd service"
    UNIT_DEST="/etc/systemd/system/llm-systems-agent.service"
    sed -e "s|\${AGENT_USER}|$USER_ARG|g" \
        -e "s|\${AGENT_GROUP}|$USER_GROUP|g" \
        -e "s|\${AGENT_INSTALL_DIR}|$INSTALL_DIR|g" \
        "$TMPL_DIR/llm-systems-agent.service.tmpl" | $SUDO tee "$UNIT_DEST" >/dev/null
    $SUDO systemctl daemon-reload
    if $SKIP_START; then
      $SUDO systemctl enable llm-systems-agent.service >/dev/null 2>&1
      _ok "$UNIT_DEST installed + enabled (not started — --no-start)"
    else
      $SUDO systemctl enable --now llm-systems-agent.service >/dev/null 2>&1
      _ok "$UNIT_DEST installed + started"
    fi
  else
    PLIST_DEST="$HOME/Library/LaunchAgents/com.llm-systems-agent.plist"
    mkdir -p "$(dirname "$PLIST_DEST")"
    # Pre-create ~/Library/Logs for the run-as user — launchd won't
    # create the parent dir for StandardOutPath/StandardErrorPath and
    # silently swallows the streams if it can't open them. Always
    # exists for the current user, but explicitly mkdir is cheap.
    sudo -u "$USER_ARG" mkdir -p "$USER_HOME/Library/Logs/llm-systems-agent" 2>/dev/null || \
      mkdir -p "$USER_HOME/Library/Logs/llm-systems-agent"
    sed -e "s|\${AGENT_USER}|$USER_ARG|g" \
        -e "s|\${AGENT_USER_HOME}|$USER_HOME|g" \
        -e "s|\${AGENT_INSTALL_DIR}|$INSTALL_DIR|g" \
        "$TMPL_DIR/com.llm-systems-agent.plist.tmpl" > "$PLIST_DEST"
    cat <<EOF

   ── macOS Local Network permission ─────────────────────────────────
   When the agent starts in a moment, macOS will pop up a dialog:

       "llm-systems-agent" would like to find and connect to devices
       on your local network.

   Click ALLOW. Without this permission, the agent will start but
   fail to reach the manager with 'No route to host' errors.

   If you missed the dialog, you can re-grant via:
       System Settings → Privacy & Security → Local Network →
       enable the row for python3 (or 'llm-systems-agent')
   Or reset the prompt with:
       tccutil reset Network && launchctl kickstart -k gui/$UID/com.llm-systems-agent

EOF

    if $SKIP_START; then
      _ok "$PLIST_DEST installed (not loaded — --no-start)"
      echo "    Start manually with: launchctl load -w $PLIST_DEST"
    else
      launchctl unload "$PLIST_DEST" 2>/dev/null || true
      launchctl load -w "$PLIST_DEST"
      _ok "$PLIST_DEST installed + loaded"
    fi

    # Post-load verification: wait for the agent to come up locally,
    # then verify it can reach the manager. Local /health doesn't need
    # Local Network permission (loopback is exempt); the manager probe
    # does. Distinguishing the two tells the user *which* problem they
    # have if something fails.
    if $SKIP_START; then
      echo
      echo "  Skipping post-install health probe — agent not started yet (--no-start)."
    else
    echo
    echo "  Waiting up to 30s for the agent to start + register..."
    # Top-level script scope (not a function) — `local` is illegal here and
    # aborts under set -e on macOS, the only OS that reaches this block.
    _started=false
    _registered=false
    for _i in $(seq 1 30); do
      sleep 1
      if [[ "$(_probe_http_code http://127.0.0.1:8082/health)" == "200" ]]; then
        _started=true
        # Once started, give it a moment to register, then check.
        if [[ "$(_probe_http_code "$MANAGER_URL/api/agents")" == "200" ]]; then
          # Look up our hostname in the registry.
          _expected_host="${HOSTNAME_OVERRIDE:-$(hostname)}"
          if curl -fsS "$MANAGER_URL/api/agents" 2>/dev/null \
               | grep -q "\"hostname\":[[:space:]]*\"$_expected_host\""; then
            _registered=true
            break
          fi
        fi
      fi
    done

    if $_started && $_registered; then
      echo "  ✓ agent up at :8082 and registered with manager"
    elif $_started && ! $_registered; then
      echo
      echo "  ⚠ agent started locally but is NOT registered with the manager." >&2
      echo "    Most likely cause: macOS Local Network permission was not granted." >&2
      echo "    Look for the dialog in your menu bar / Notification Center, or" >&2
      echo "    open System Settings → Privacy & Security → Local Network and" >&2
      echo "    enable the entry for python3 / llm-systems-agent." >&2
      echo "    Then restart the agent:" >&2
      echo "        launchctl kickstart -k gui/$UID/com.llm-systems-agent" >&2
      echo "    Tail the log to confirm registration:" >&2
      echo "        tail -f /Users/$USER_ARG/Library/Logs/llm-systems-agent/agent.log" >&2
    elif ! $_started; then
      echo
      echo "  ✗ agent did not respond on http://127.0.0.1:8082/health within 30s." >&2
      echo "    Check the launchd log:" >&2
      echo "        tail -f /Users/$USER_ARG/Library/Logs/llm-systems-agent/agent.log" >&2
    fi
    fi   # end: ! $SKIP_START
  fi
fi

if [[ "$AGENT_OS" == "linux" && "$SKIP_SUDOERS" == "false" ]]; then
  if $FROM_SELF_UPDATE; then
    # Self-update runs as the agent user (no root, no sudo). Skip the
    # rewrite — the existing sudoers stays. If sudoers content has
    # changed (e.g. a new perf command was added in this version) the
    # operator needs to re-run install.sh manually with sudo to pick
    # up the new template. Diff against template:
    TMP_SUDOERS="$(mktemp)"
    sed "s|\${AGENT_USER}|$USER_ARG|g" "$TMPL_DIR/llm-systems-agent.sudoers.tmpl" > "$TMP_SUDOERS"
    if [[ -r /etc/sudoers.d/llm-systems-agent ]] \
       && cmp -s "$TMP_SUDOERS" /etc/sudoers.d/llm-systems-agent; then
      _ok "sudoers up-to-date (skipped — self-update can't rewrite)"
    else
      echo "  ⚠ sudoers template differs from installed file (or unreadable as agent user)."
      echo "    Self-update can't rewrite /etc/sudoers.d/ — run manually."
      echo "    Note: single-quote the sed pattern so bash doesn't expand \${AGENT_USER}."
      echo "      TMP=\$(mktemp)"
      echo "      sed 's|\${AGENT_USER}|$USER_ARG|g' \\"
      echo "        $TMPL_DIR/llm-systems-agent.sudoers.tmpl > \"\$TMP\""
      echo "      sudo visudo -c -f \"\$TMP\" && \\"
      echo "        sudo install -m 0440 -o root -g root \"\$TMP\" /etc/sudoers.d/llm-systems-agent"
      echo "      rm -f \"\$TMP\""
      echo "      sudo systemctl restart llm-systems-agent"
    fi
    rm -f "$TMP_SUDOERS"
  else
    TMP_SUDOERS="$(mktemp)"
    sed "s|\${AGENT_USER}|$USER_ARG|g" "$TMPL_DIR/llm-systems-agent.sudoers.tmpl" > "$TMP_SUDOERS"
    if $SUDO visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
      $SUDO install -m 0440 -o root -g root "$TMP_SUDOERS" /etc/sudoers.d/llm-systems-agent
      _ok "sudoers rules installed: /etc/sudoers.d/llm-systems-agent"
    else
      echo "ERROR: visudo validation failed for $TMP_SUDOERS — NOT installing sudoers." >&2
      rm -f "$TMP_SUDOERS"
      exit 1
    fi
    rm -f "$TMP_SUDOERS"
  fi
fi

# 6b. Migrate /tmp/llama-server-last-state ownership.
# The legacy bash perf-controller daemon ran as root and created this
# file owned by root. The agent (running as a regular user) can't
# os.replace() over a root-owned file under /tmp because of the
# sticky bit, so every transition would fail with EPERM. One-time
# fixup: chown to the agent user so the agent can write atomically.
if [[ "$AGENT_OS" == "linux" && "$ENABLE_PERF" == "true" ]]; then
  STATE_FILE="/tmp/llama-server-last-state"
  if [[ -e "$STATE_FILE" ]]; then
    CURRENT_OWNER="$(stat -c %U "$STATE_FILE" 2>/dev/null || echo unknown)"
    if [[ "$CURRENT_OWNER" != "$USER_ARG" ]]; then
      echo
      echo "── Migrating $STATE_FILE ownership ────────────────────────────────────"
      echo "  current owner: $CURRENT_OWNER → target: $USER_ARG"
      $SUDO chown "$USER_ARG:$USER_GROUP" "$STATE_FILE"
      echo "  ✓ chowned to $USER_ARG:$USER_GROUP"
      echo "  (legacy artifact from the bash perf-controller daemon — agent"
      echo "   needs to own it to do atomic rename writes under /tmp)"
    fi
  fi
fi

# 7. Example perf-controller systemd units (opt-in via --install-perf-units)
if $INSTALL_PERF_UNITS; then
  if [[ "$AGENT_OS" != "linux" ]]; then
    echo "WARNING: --install-perf-units is Linux-only; ignoring on $AGENT_OS." >&2
  else
    EX_DIR="$TMPL_DIR/examples"
    if [[ ! -d "$EX_DIR" ]]; then
      echo "ERROR: example unit dir not found: $EX_DIR" >&2
      exit 1
    fi
    echo
    echo "── Installing example perf units ────────────────────────────────────────"
    PERF_INSTALLED=()
    PERF_SKIPPED=()
    for unit in performance.service powersave.service; do
      src="$EX_DIR/$unit"
      dest="/etc/systemd/system/$unit"
      if [[ ! -f "$src" ]]; then
        echo "  WARNING: $src missing in source — skipping" >&2
        continue
      fi
      if [[ -f "$dest" ]] && ! $FORCE_OVERWRITE_PERF; then
        PERF_SKIPPED+=("$unit")
        echo "  ⓘ $dest already exists — leaving alone (use --force-overwrite-perf-units to clobber, .bak first)"
        continue
      fi
      if [[ -f "$dest" ]] && $FORCE_OVERWRITE_PERF; then
        BAK="$dest.bak.$(date +%s)"
        $SUDO cp -p "$dest" "$BAK"
        echo "  ⚠ overwriting $dest (backup at $BAK)"
      fi
      $SUDO install -m 0644 -o root -g root "$src" "$dest"
      PERF_INSTALLED+=("$unit")
      echo "  ✓ installed $dest"
    done
    if (( ${#PERF_INSTALLED[@]} > 0 )); then
      $SUDO systemctl daemon-reload
      echo "  daemon-reload complete"
      echo "  trigger them with:  sudo systemctl reload-or-restart performance"
      echo "                      sudo systemctl reload-or-restart powersave"
      echo "  (the agent will trigger them automatically on llama-server transitions)"
      echo
      echo "  These units only set the CPU governor by default. Tune for your"
      echo "  hardware via:"
      echo "      sudo systemctl edit performance     # add ExecStart= overrides"
      echo "      sudo systemctl edit powersave"
      echo "  See $EX_DIR/README.md for examples."
    fi
    if (( ${#PERF_SKIPPED[@]} > 0 )); then
      echo
      echo "  Skipped (already present): ${PERF_SKIPPED[*]}"
      echo "  Re-run with --force-overwrite-perf-units if you want to replace"
      echo "  them with the examples (originals preserved as .bak)."
    fi
    echo "─────────────────────────────────────────────────────────────────────────"
  fi
fi

# 8. liquidctl (optional cooler control — prompted only when perf controller
#    or example perf units are being installed). Install from upstream PyPI
#    via pipx, plus the udev rules from the liquidctl GitHub repo.
if { $ENABLE_PERF || $INSTALL_PERF_UNITS; } && [[ "$AGENT_OS" == "linux" ]]; then
  if command -v liquidctl >/dev/null 2>&1; then
    LQ_VER="$(liquidctl --version 2>/dev/null | head -1 || echo unknown)"
    echo
    echo "  ✓ liquidctl already installed: $LQ_VER"
  else
    echo
    echo "── liquidctl (cooler control for perf controller) ───────────────────────"
    echo "  liquidctl drives AIO pumps + case fans for the 'performance' /"
    echo "  'powersave' systemd units. The example units have liquidctl lines"
    echo "  commented out, so it's optional — but you'll want it if you have an"
    echo "  NZXT/Corsair/Asetek/etc. cooler and want fan curves to follow llama"
    echo "  load."
    echo
    echo "  Source: https://github.com/liquidctl/liquidctl"
    echo
    echo "  If you say yes, the installer will:"
    echo "    1. pipx install liquidctl    (latest stable release from PyPI)"
    echo "    2. Drop /etc/udev/rules.d/71-liquidctl.rules from the upstream repo"
    echo "       (lets liquidctl access HID devices without root in the future)"
    echo "    3. udevadm control --reload-rules && udevadm trigger"
    echo

    if [[ ! -t 0 ]]; then
      echo "  Skipping liquidctl install (stdin is not a TTY)."
      echo "  Install manually later with:"
      echo "      pipx install liquidctl"
      echo "─────────────────────────────────────────────────────────────────────────"
    else
      read -rp "  Install liquidctl from upstream now? [Y/n] " LQ_REPLY
      case "$(printf '%s' "$LQ_REPLY" | tr '[:upper:]' '[:lower:]')" in
        ""|y|yes)
          # pipx is required (modern way to install Python CLI tools system-wide
          # without polluting system Python). Refuse to fall back to
          # `pip install --break-system-packages` — that's an audit nightmare.
          if ! command -v pipx >/dev/null 2>&1; then
            echo
            echo "  ERROR: pipx is required for the upstream install path." >&2
            echo "         Install pipx first, then re-run this installer:" >&2
            if [[ -f /etc/debian_version ]]; then
              echo "             sudo apt-get install -y pipx && pipx ensurepath" >&2
            elif [[ -f /etc/redhat-release ]]; then
              echo "             sudo dnf install -y pipx && pipx ensurepath" >&2
            else
              echo "             (install pipx via your distro package manager)" >&2
            fi
            echo "         Or skip this step — liquidctl is optional." >&2
            echo "─────────────────────────────────────────────────────────────────────────"
          else
            echo
            echo "  Installing liquidctl via pipx (running as root so the symlink"
            echo "  lands in a system PATH systemd can use)…"
            # `pipx install` as root puts symlinks in /usr/local/bin by default
            # (or whatever PIPX_BIN_DIR points to). Confirm by checking after.
            if $SUDO env PIPX_BIN_DIR=/usr/local/bin pipx install liquidctl; then
              if command -v liquidctl >/dev/null 2>&1; then
                echo "  ✓ liquidctl installed: $(liquidctl --version 2>/dev/null | head -1)"
              else
                echo "  ⚠ pipx reported success but 'liquidctl' isn't on PATH yet."
                echo "    You may need to log out + log back in, or run 'pipx ensurepath'."
              fi

              # Drop udev rules from upstream main branch so non-root users
              # (and the perf systemd units running as root, but better safe)
              # can address USB HID devices without errors.
              UDEV_URL="https://raw.githubusercontent.com/liquidctl/liquidctl/main/extra/linux/71-liquidctl.rules"
              UDEV_DEST="/etc/udev/rules.d/71-liquidctl.rules"
              echo "  Fetching udev rules from $UDEV_URL …"
              TMP_UDEV="$(mktemp)"
              if command -v curl >/dev/null 2>&1; then
                curl -fsSL -m 10 -o "$TMP_UDEV" "$UDEV_URL" || true
              elif command -v wget >/dev/null 2>&1; then
                wget -q -T 10 -O "$TMP_UDEV" "$UDEV_URL" || true
              fi
              if [[ -s "$TMP_UDEV" ]]; then
                $SUDO install -m 0644 -o root -g root "$TMP_UDEV" "$UDEV_DEST"
                rm -f "$TMP_UDEV"
                echo "  ✓ installed $UDEV_DEST"
                $SUDO udevadm control --reload-rules || true
                $SUDO udevadm trigger || true
                echo "  ✓ udev rules reloaded — unplug/replug USB coolers if they're connected"
              else
                echo "  ⚠ failed to fetch udev rules; install them manually:"
                echo "      sudo curl -fsSL -o $UDEV_DEST $UDEV_URL"
                echo "      sudo udevadm control --reload-rules && sudo udevadm trigger"
                rm -f "$TMP_UDEV"
              fi
            else
              echo "  ✗ pipx install liquidctl failed — see the error above."
              echo "    The agent will work without liquidctl; you can install it later."
            fi
            echo "─────────────────────────────────────────────────────────────────────────"
          fi
          ;;
        *)
          echo "  Skipped. Install manually later with:  pipx install liquidctl"
          echo "─────────────────────────────────────────────────────────────────────────"
          ;;
      esac
    fi
  fi
fi

# Recommend an inference runtime when the host has a GPU but no provider
# was enabled and no llama-server / lms / vllm binary is on disk. Pure
# information — never auto-installs (toolchain/version/license decisions
# belong to the operator). Only fires on fresh installs (the --update
# path exits earlier).
#
# `_gpu_vendor` is set by the Host hardware collectors block, which is
# skipped for ROLE=system_only. Default to empty here so set -u is happy
# and the guard correctly evaluates to "no GPU detected, skip".
: "${_gpu_vendor:=}"
if [[ "$AGENT_OS" == "linux" ]] \
   && [[ "$_gpu_vendor" == "AMD" || "$_gpu_vendor" == "NVIDIA" ]] \
   && [[ "$ENABLE_LLAMA" != "true" && "$ENABLE_LMS" != "true" ]]; then
  _llama_found=""
  _llama_found="$(_first_existing \
      "/usr/local/llama-server/llama-server" \
      "/usr/local/llama.cpp/llama-server" \
      "/usr/local/bin/llama-server" \
      "$HOME/llama.cpp/build/bin/llama-server" 2>/dev/null || true)"
  _lms_found=""
  command -v lms >/dev/null 2>&1 && _lms_found="$(command -v lms)"
  [[ -z "$_lms_found" && -x "$HOME/.lmstudio/bin/lms" ]] && _lms_found="$HOME/.lmstudio/bin/lms"
  _vllm_found=""
  python3 -c "import vllm" >/dev/null 2>&1 && _vllm_found="python3 -c 'import vllm'"

  if [[ -z "$_llama_found" && -z "$_lms_found" && -z "$_vllm_found" ]]; then
    echo
    echo "── Recommended next step: install an inference runtime ────────────────"
    echo "  This host has a $_gpu_vendor GPU but no inference runtime was found"
    echo "  (llama.cpp / LM Studio / vLLM). The agent registered fine — it just"
    echo "  won't have a provider to monitor until one is installed."
    echo
    echo "  The installer does NOT auto-install these (GPU toolchain + version"
    echo "  pinning + license choices are operator decisions). Pick ONE:"
    echo
    if [[ "$_gpu_vendor" == "AMD" ]]; then
      echo "  • llama.cpp (HIP/ROCm) — small, fast, GGUF models:"
      echo "      git clone https://github.com/ggerganov/llama.cpp /usr/local/llama.cpp"
      echo "      cd /usr/local/llama.cpp && cmake -B build -DGGML_HIP=ON \\"
      echo "          -DAMDGPU_TARGETS=gfx1100 -DCMAKE_BUILD_TYPE=Release"
      echo "      cmake --build build --config Release -j"
      echo "      # adjust AMDGPU_TARGETS for your card (gfx1100 = RX 7900 XTX)"
    else
      echo "  • llama.cpp (CUDA) — small, fast, GGUF models:"
      echo "      git clone https://github.com/ggerganov/llama.cpp /usr/local/llama.cpp"
      echo "      cd /usr/local/llama.cpp && cmake -B build -DGGML_CUDA=ON \\"
      echo "          -DCMAKE_BUILD_TYPE=Release"
      echo "      cmake --build build --config Release -j"
    fi
    echo
    echo "  • LM Studio — GUI-driven catalog + headless 'lms' CLI:"
    echo "      https://lmstudio.ai/download    (download, install, run once,"
    echo "                                       then 'lms server start' for the API)"
    echo
    if [[ "$_gpu_vendor" == "NVIDIA" ]]; then
      echo "  • vLLM — high-throughput batched serving (NVIDIA-only via PyPI):"
      echo "      python3 -m venv /opt/vllm && source /opt/vllm/bin/activate"
      echo "      pip install vllm   # ~3 GB of PyTorch + CUDA wheels"
    else
      echo "  • vLLM — limited AMD support; check vllm.ai for ROCm build status"
      echo "      before committing to it on this host."
    fi
    echo
    echo "  After installing one, re-run the installer with --update so the"
    echo "  agent re-detects the runtime and enables the matching provider:"
    echo "      sudo bash $INSTALL_DIR/src/agent/install/install.sh --update"
    echo "  …then edit $INSTALL_DIR/agent_config.yaml to set LLAMA_ENABLED=true"
    echo "  (or LMS_ENABLED=true), and restart the agent."
    echo "─────────────────────────────────────────────────────────────────────────"
  fi
  unset _llama_found _lms_found _vllm_found
fi

echo
echo "── Agent Installation Complete ─────────────────────────────────────────────"
echo "  Edit config:    $INSTALL_DIR/agent_config.yaml"
echo "  View logs:      ${AGENT_OS:+(linux)} journalctl -u llm-systems-agent -f"
echo "                  (macOS) tail -f /Users/$USER_ARG/Library/Logs/llm-systems-agent/agent.log"
echo "  Health check:   curl http://localhost:8082/health"
echo
# Show a browser-reachable URL — when the manager runs on the same host
# (MANAGER_URL=127.0.0.1) the operator still needs a LAN IP they can hit
# from their workstation. Fall back to the configured URL if no LAN IP.
APPROVE_URL="$MANAGER_URL"
if [[ "$MANAGER_URL" =~ ^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?/?$ ]]; then
  _lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "$_lan_ip" ]]; then
    _port="$(printf '%s' "$MANAGER_URL" | sed -nE 's|.*:([0-9]+)/?$|\1|p')"
    APPROVE_URL="http://${_lan_ip}:${_port:-5000}"
  fi
fi
echo "  Approve at:     $APPROVE_URL/  → Admin tab → Agents"
echo "─────────────────────────────────────────────────────────────────────────"
