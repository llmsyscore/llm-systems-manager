#!/usr/bin/env python3
"""Universal LLM Systems Agent — runs on Linux or macOS.

Collects host metrics + provider state, forwards to the alarm engine, posts
dashboard payloads to the manager, exposes a FastAPI control surface, and
self-registers/heartbeats with the manager. Optionally runs the performance
controller. Config via agent_config.yaml or env vars.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import platform
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

import psutil
import requests
import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unified_config_reader  # type: ignore
import buffered_metric_client as bmc  # type: ignore
import stream_pool  # type: ignore
from _best_effort import best_effort  # type: ignore
# collectors.configure_all(CONFIG) + providers.configure_all(ctx) called from main().
import collectors  # type: ignore
import providers  # type: ignore
from collectors.system import collect_system_metrics  # type: ignore
from providers.lms import lms_get_models, lms_get_ps, lms_get_status  # type: ignore
from providers.llama import collect_llama_for_metrics, llama_get_state  # type: ignore
from agent_context import AgentContext  # type: ignore
try:
    from _utils import atomic_write_text  # type: ignore
except ImportError:
    # Older deployments may not ship _utils.py; inline to keep agent bootable.
    def atomic_write_text(path, content, mode=None, encoding="utf-8"):  # type: ignore[no-redef]
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding=encoding)
        if mode is not None:
            os.chmod(tmp, mode)
        tmp.replace(p)

VERSION = "v2026.06.25-1"


def _detect_install_dir() -> str:
    """Resolved parent dir of this script — runtime source of truth for paths."""
    try:
        return str(Path(__file__).resolve().parent)
    except Exception:
        return "/opt/llm-systems-agent"


DEFAULT_INSTALL_DIR = _detect_install_dir()


def _detect_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        try:
            return pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _detect_default_lms_cmd() -> str:
    found = shutil.which("lms")
    if found:
        return found
    if sys.platform == "darwin":
        return os.path.expanduser("~/.lmstudio/bin/lms")
    return "/usr/local/bin/lms"


def _detect_default_hf_cmd() -> str:
    found = shutil.which("hf")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/hf")


def _default_log_file(install_dir: str) -> str:
    """Return the first writable log path: platform-conventional → install_dir → /tmp."""
    candidates = []
    if sys.platform == "darwin":
        candidates.append(os.path.expanduser("~/Library/Logs/llm-systems-agent/agent.log"))
    else:
        candidates.append("/var/log/llm-systems-manager/llm-systems-agent.log")
    candidates.append(os.path.join(install_dir, "logs", "agent.log"))
    candidates.append(f"/tmp/llm-systems-agent-{os.getuid()}.log")

    for target in candidates:
        try:
            d = os.path.dirname(target)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(target, "a"):
                pass
            return target
        except (PermissionError, OSError):
            continue
    return candidates[-1]


def _probe_http(url: str, timeout: float = 1.5) -> "tuple[bool, str]":
    """Best-effort HTTP probe. Returns (ok, summary)."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.ok:
            return True, f"HTTP {r.status_code} ({len(r.content)} bytes)"
        return False, f"HTTP {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "connection refused"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _probe_systemd_unit(name: str) -> "tuple[bool, str]":
    """Returns (exists, current_state). Tries .service / .target if name is unsuffixed."""
    if sys.platform != "linux":
        return False, "not-linux"
    candidates = [name] if "." in name else [f"{name}.service", f"{name}.target", name]
    for candidate in candidates:
        try:
            rc = subprocess.run(
                ["systemctl", "list-unit-files", candidate, "--no-legend"],
                capture_output=True, text=True, timeout=3,
            )
            if rc.returncode == 0 and rc.stdout.strip():
                active = subprocess.run(
                    ["systemctl", "is-active", candidate],
                    capture_output=True, text=True, timeout=3,
                )
                return True, active.stdout.strip() or "unknown"
        except FileNotFoundError:
            return False, "no systemctl"
        except Exception:
            continue
    return False, "not installed"


def _probe_and_autoconfigure(cfg: "AgentConfig") -> None:
    """Probe the host for known LLM subsystems and set cfg flags + AGENT_ROLE.

    Runs only when AGENT_ROLE == 'auto'. Does NOT auto-enable the perf
    controller — it modifies system state.
    """
    print("── Auto-detect (role=auto) ─────────────────────────────────────────────")

    found = {"llama": False, "lms": False, "openclaw": False}

    llama_http_ok, llama_http_msg = _probe_http(f"{cfg.LLAMA_API_URL}/v1/models")
    llama_log_exists = os.path.isfile(cfg.LLAMA_LOG_FILE)
    llama_unit_ok, llama_unit_state = _probe_systemd_unit(cfg.LLAMA_SYSTEMD_UNIT)
    if llama_http_ok or llama_log_exists or llama_unit_ok:
        found["llama"] = True
        cfg.LLAMA_ENABLED = True
        print("  ✓ llama.cpp detected")
        print(f"      API   {cfg.LLAMA_API_URL:<32} {('reachable — ' + llama_http_msg) if llama_http_ok else 'unreachable'}")
        print(f"      log   {cfg.LLAMA_LOG_FILE:<32} {'exists' if llama_log_exists else 'missing'}")
        print(f"      unit  {cfg.LLAMA_SYSTEMD_UNIT:<32} {('present — ' + llama_unit_state) if llama_unit_ok else 'not installed'}")
    else:
        print("  ✗ llama.cpp not detected")
        print(f"      tried API={cfg.LLAMA_API_URL} log={cfg.LLAMA_LOG_FILE} unit={cfg.LLAMA_SYSTEMD_UNIT}")

    # ── LM Studio ────
    lms_http_ok, lms_http_msg = _probe_http(f"{cfg.LMS_API_URL}/v1/models")
    lms_cli_exists = bool(cfg.LMS_CMD) and os.path.isfile(cfg.LMS_CMD)
    if lms_http_ok or lms_cli_exists:
        found["lms"] = True
        cfg.LMS_ENABLED = True
        print("  ✓ LM Studio detected")
        print(f"      API   {cfg.LMS_API_URL:<32} {('reachable — ' + lms_http_msg) if lms_http_ok else 'unreachable'}")
        print(f"      cli   {cfg.LMS_CMD:<32} {'exists' if lms_cli_exists else 'missing'}")
    else:
        print("  ✗ LM Studio not detected")
        print(f"      tried API={cfg.LMS_API_URL} cli={cfg.LMS_CMD or '<none>'}")

    # Resolve the run-as user's home via getpwnam — /home vs /Users vs LDAP.
    user_home = ""
    if cfg.AGENT_USER:
        try:
            user_home = pwd.getpwnam(cfg.AGENT_USER).pw_dir
        except KeyError:
            user_home = ""
    oc_candidates = [
        cfg.OPENCLAW_AGENTS_DIR,
        os.path.join(user_home, ".openclaw/agents") if user_home else "",
        os.path.expanduser("~/.openclaw/agents"),
        "/mnt/openclaw/agents",
    ]
    oc_found = next((p for p in oc_candidates if p and os.path.isdir(p)), None)
    if oc_found:
        try:
            n_agents = len([p for p in os.listdir(oc_found)
                            if os.path.isdir(os.path.join(oc_found, p))])
        except OSError:
            n_agents = 0
        found["openclaw"] = True
        cfg.OPENCLAW_ENABLED = True
        cfg.OPENCLAW_AGENTS_DIR = oc_found
        print(f"  ✓ OpenClaw agents directory found at {oc_found} ({n_agents} agents)")
    else:
        print("  ✗ OpenClaw agents directory not found")
        for p in oc_candidates:
            if p:
                print(f"      tried {p}")

    sd_proc_ok = False
    try:
        sd_proc_ok = any(
            (p.info.get("name") or "").lower() == "sd-server"
            for p in psutil.process_iter(["name"])
        )
    except Exception:
        sd_proc_ok = False
    sd_http_ok, sd_http_msg = _probe_http("http://127.0.0.1:1234/")
    if sd_proc_ok or sd_http_ok:
        cfg.IMGGEN_ENABLED = True
        print("  ✓ image generation (sd.cpp) detected")
        print(f"      proc  sd-server                        {'running' if sd_proc_ok else 'not found'}")
        print(f"      http  http://127.0.0.1:1234/           {('reachable — ' + sd_http_msg) if sd_http_ok else 'unreachable'}")
    else:
        print("  ✗ image generation (sd.cpp) not detected")
        print("      tried proc=sd-server, http=127.0.0.1:1234")

    if found["llama"] and cfg.AGENT_OS == "linux":
        perf_ok, perf_state = _probe_systemd_unit(cfg.PERF_TARGET_AWAKE)
        sleep_ok, sleep_state = _probe_systemd_unit(cfg.PERF_TARGET_SLEEP)
        if perf_ok and sleep_ok:
            print(f"  ⓘ perf controller eligible — '{cfg.PERF_TARGET_AWAKE}'/'{cfg.PERF_TARGET_SLEEP}' systemd units present")
            print("     enable explicitly with PERF_CONTROLLER_ENABLED: true (won't auto-enable; modifies system state)")
        else:
            print(f"  ⓘ perf controller prereqs missing (units '{cfg.PERF_TARGET_AWAKE}'={perf_state}, '{cfg.PERF_TARGET_SLEEP}'={sleep_state})")

    if found["llama"] and found["lms"]:
        cfg.AGENT_ROLE = "mixed"
    elif found["llama"]:
        cfg.AGENT_ROLE = "llama_host"
    elif found["lms"]:
        cfg.AGENT_ROLE = "lms_host"
    else:
        cfg.AGENT_ROLE = "system_only"

    print(f"  → resolved role: {cfg.AGENT_ROLE}")
    print("─────────────────────────────────────────────────────────────────────────")


class AgentConfig:
    """Effective configuration. Resolution: defaults < YAML < env vars."""

    AGENT_OS: str = "linux"
    AGENT_HOSTNAME: str = ""
    AGENT_ROLE: str = "auto"  # auto | llama_host | lms_host | mixed
    AGENT_BIND_HOST: str = "0.0.0.0"
    AGENT_BIND_PORT: int = 8082
    # When all three files exist+readable, agent serves TLS on AGENT_BIND_PORT (HTTP dropped).
    TLS_CERT_FILE: str = "data/tls-cert.pem"
    TLS_KEY_FILE:  str = "data/tls-key.pem"
    TLS_CA_FILE:   str = "data/tls-ca.pem"
    AGENT_DESCRIPTION: str = ""
    AGENT_USER: str = ""
    AGENT_INSTALL_DIR: str = DEFAULT_INSTALL_DIR
    AGENT_REPO_DIR: str = ""

    # Installer fills these in; agent refuses to register if MANAGER_URL is blank.
    # ALARM_ENGINE_URL is derived from MANAGER_URL (port → 8081) when blank.
    MANAGER_URL: str = ""
    ALARM_ENGINE_URL: str = ""

    LMS_ENABLED: bool = False
    LMS_CMD: str = ""
    LMS_API_URL: str = "http://localhost:1235"

    LLAMA_ENABLED: bool = False
    LLAMA_BIN: str = ""
    LLAMA_CONFIG_INI: str = ""
    LLAMA_LOG_FILE: str = ""
    LLAMA_STATE_FILE: str = "/tmp/llama-server-last-state"
    LLAMA_SYSTEMD_UNIT: str = "llama_server.service"
    LLAMA_API_URL: str = "http://localhost:8080"
    LLAMA_BUILD_METHOD: str = ""          # custom_script|source|release_binary|conda|homebrew
    LLAMA_BUILD_DIR: str = ""             # managed install root; blank => ~/.local/share/llama.cpp
    LLAMA_BUILD_OPTS: dict = {}           # YAML-only; per-method knobs
    # /models/sse push-state consumer: auto (router mode only) | on | off.
    LLAMA_SSE_ENABLED: str = "auto"

    PERF_CONTROLLER_ENABLED: bool = False
    PERF_TARGET_AWAKE: str = "performance"
    PERF_TARGET_SLEEP: str = "powersave"
    # Substring markers copied verbatim from the bash performance controller.
    PERF_SLEEP_MARKERS: list[str] = [
        "server is entering sleeping state",
        "cmd_child_to_router:sleep",
    ]
    PERF_WAKE_MARKERS: list[str] = [
        "server is exiting sleeping state",
        "main: loading model",
        "model loaded",
        "load_model: loading model",
    ]

    # --- OpenClaw ---
    OPENCLAW_ENABLED: bool = False
    # Default resolves at runtime via getpwnam(AGENT_USER).pw_dir; blank here
    # means "fall back to ~/.openclaw/agents" in the auto-detect probe.
    OPENCLAW_AGENTS_DIR: str = ""

    # --- Image generation (stable-diffusion.cpp sd-server) ---
    # No host-specific URLs needed — the manager's /proxy/imggen/ resolver
    # discovers an agent with this capability and uses its registered IP.
    # IMGGEN_PORT lets a host run sd-server on a non-default port; the
    # agent advertises it at registration so the manager can dial the
    # right port without operator-side TOML edits.
    IMGGEN_ENABLED: bool = False
    IMGGEN_PORT:    int  = 1234

    # --- HF CLI ---
    HF_CLI_PATH: str = ""

    # --- HTTP worker pool / SSE stream cap ---
    # anyio pool size; SSE streams capped at WORKER_THREADS - STREAM_RESERVE_THREADS.
    WORKER_THREADS: int = 64
    STREAM_RESERVE_THREADS: int = 24

    # --- collection / forwarding ---
    POLL_INTERVAL_S: int = 5
    HEARTBEAT_INTERVAL_S: int = 60
    COLLECTION_ENABLED: bool = True

    # --- BufferedMetricClient → alarm engine push tuning ---
    # METRIC_FLUSH_INTERVAL_S controls how often the agent POSTs the
    # accumulated batch to the alarm engine. Larger = fewer-but-fatter
    # writes (lower CPU on the Influx side). The collector still samples
    # every POLL_INTERVAL_S; only the network push cadence changes.
    METRIC_FLUSH_INTERVAL_S: float = 30.0
    METRIC_MAX_MEMORY_SAMPLES: int = 400
    METRIC_BATCH_LIMIT: int = 1000
    METRIC_HTTP_TIMEOUT_S: float = 10.0

    # --- Manager / alarm-engine / Influx self-monitor probes ---
    # When MONITOR_MANAGER_ENABLED, a background thread probes the
    # configured MANAGER_URL endpoints periodically. When
    # MONITOR_ALARM_ENGINE_ENABLED, the same thread probes ALARM_ENGINE_URL
    # and (using the alerts token from the unified config) writes/reads
    # a synthetic point against InfluxDB. Probe results land under the
    # `manager_self_monitor` source — see metric_flatten.py.
    #
    # Both default false. The installer flips them on when it detects the
    # corresponding systemd unit (llm-systems-manager.service /
    # llm-systems-alarm-engine.service) on the host being installed.
    # Operators can override either side independently in YAML to support
    # a future split of manager + alarm engine onto separate hosts.
    MONITOR_MANAGER_ENABLED: bool = False
    MONITOR_ALARM_ENGINE_ENABLED: bool = False
    META_PERF_INTERVAL_S: int = 60        # seconds between probe passes
    META_PERF_TIMEOUT_S: int = 5          # per-probe HTTP timeout

    # InfluxDB on-disk-bytes probe. Off by default; the agent's installer
    # turns it on automatically when InfluxDB is detected on this host
    # (process watchlist match for `influxdb.service`). Walks every TSM
    # file under INFLUXDB_DATA_PATH via `du -sb`, so the cached value is
    # Refreshed every INFLUXDB_DISK_PROBE_INTERVAL_S seconds, not per tick.
    MONITOR_INFLUXDB_DISK_ENABLED: bool = False
    INFLUXDB_DATA_PATH: str = ""
    INFLUXDB_DISK_PROBE_INTERVAL_S: int = 300
    # Path to the unified-config TOML the influx self-monitor probe reads
    # connection details from. Blank => $LLM_SYSTEMS_CONFIG or the /opt default.
    UNIFIED_CONFIG_TOML_PATH: str = ""

    COLLECT_GPU_ENABLED: bool = True
    COLLECT_SENSORS_ENABLED: bool = True
    COLLECT_LIQUIDCTL_ENABLED: bool = True
    COLLECT_UPS_ENABLED: bool = True
    COLLECT_ISCSI_ENABLED: bool = True
    LIQUIDCTL_BIN: str = ""

    PUSH_HOST_METRICS_ENABLED: bool = True

    # Each entry: {name, match: "process"|"systemd", pattern, unit}. Populated per-OS in load() if empty.
    PROCESS_WATCHLIST: list = []
    PROCESS_WATCH_ENABLED: bool = True

    # Per-rule offsets persist under data/log-watch-state.json to survive restarts.
    LOG_WATCH_ENABLED: bool = False
    LOG_WATCH_INTERVAL_S: int = 10
    LOG_WATCH_MAX_BYTES_PER_TICK: int = 1_048_576
    LOG_WATCH_RULES: list = []

    # Requires passwordless `sudo -n /usr/bin/powermetrics` via /etc/sudoers.d.
    COLLECT_POWERMETRICS_ENABLED: bool = True
    POWERMETRICS_INTERVAL_MS: int = 5000

    LOG_FILE: str = ""
    LOG_LEVEL: str = "INFO"          # DEBUG | INFO | WARNING | ERROR
    TOKEN_FILE: str = ""

    @classmethod
    def load(cls) -> "AgentConfig":
        cfg = cls()

        cfg.AGENT_OS = "macos" if sys.platform == "darwin" else "linux"
        cfg.AGENT_HOSTNAME = socket.gethostname()
        cfg.AGENT_USER = _detect_user()
        cfg.LMS_CMD = _detect_default_lms_cmd()
        cfg.HF_CLI_PATH = _detect_default_hf_cmd()

        yaml_paths = [
            Path.cwd() / "agent_config.yaml",
            Path("/etc/llm-systems-agent/agent_config.yaml"),
            Path(cfg.AGENT_INSTALL_DIR) / "agent_config.yaml",
            Path(__file__).resolve().parent / "agent_config.yaml",
        ]
        loaded_from = None
        for p in yaml_paths:
            if not p.is_file():
                continue
            try:
                data = yaml.safe_load(p.read_text()) or {}
            except Exception as e:
                # Malformed YAML: refuse to start with defaults — operator wouldn't notice.
                msg = (
                    f"FATAL: agent_config.yaml at {p} exists but failed to parse: {e}\n"
                    f"       Refusing to start with default config — fix the YAML "
                    f"(see backups at {p}.*.bak) and retry."
                )
                print(msg, file=sys.stderr)
                raise SystemExit(2)
            for k, v in data.items():
                if not hasattr(cfg, k):
                    continue
                # Skip blank strings so YAML doesn't clobber runtime defaults.
                if isinstance(v, str) and v == "":
                    continue
                setattr(cfg, k, v)
            loaded_from = str(p)
            break

        # Each config field can be overridden as $LSA_<NAME>.
        for k in vars(cls):
            if k.startswith("_") or not k.isupper():
                continue
            env_key = f"LSA_{k}"
            if env_key in os.environ:
                raw = os.environ[env_key]
                cur = getattr(cfg, k)
                if isinstance(cur, bool):
                    setattr(cfg, k, raw.lower() in ("1", "true", "yes", "on"))
                elif isinstance(cur, int):
                    try:
                        setattr(cfg, k, int(raw))
                    except ValueError:
                        pass
                elif isinstance(cur, list):
                    setattr(cfg, k, [s.strip() for s in raw.split("|") if s.strip()])
                else:
                    setattr(cfg, k, raw)

        if not cfg.PROCESS_WATCHLIST:
            if cfg.AGENT_OS == "macos":
                cfg.PROCESS_WATCHLIST = [
                    {"name": "lm-studio",  "match": "process", "pattern": "LM Studio"},
                    {"name": "lms-server", "match": "process", "pattern": "lms-server"},
                    {"name": "sd-server",  "match": "process", "pattern": "sd-server"},
                ]
            else:
                cfg.PROCESS_WATCHLIST = [
                    {"name": "llama-server",   "match": "systemd", "unit": "llama_server.service"},
                    {"name": "influxdb",       "match": "systemd", "unit": "influxdb.service"},
                    {"name": "manager",        "match": "systemd", "unit": "llm-systems-manager.service"},
                    {"name": "alarm-engine",   "match": "systemd", "unit": "llm-systems-alarm-engine.service"},
                ]

        # Probe AFTER YAML/env so explicit user choices win over auto-detection.
        if (cfg.AGENT_ROLE or "").lower() == "auto":
            try:
                _probe_and_autoconfigure(cfg)
            except Exception as e:
                print(f"WARNING: auto-detect failed: {e}", file=sys.stderr)
                cfg.AGENT_ROLE = "system_only"

        if not cfg.LOG_FILE:
            cfg.LOG_FILE = _default_log_file(cfg.AGENT_INSTALL_DIR)
        if not cfg.TOKEN_FILE:
            cfg.TOKEN_FILE = os.path.join(cfg.AGENT_INSTALL_DIR, "data", "token")
        if not cfg.AGENT_REPO_DIR:
            cfg.AGENT_REPO_DIR = os.path.join(cfg.AGENT_INSTALL_DIR, "src")
        # Scheme is required; bare "host:port" yields opaque `requests` errors.
        for _attr in ("MANAGER_URL", "ALARM_ENGINE_URL"):
            v = getattr(cfg, _attr, "")
            if v and not (v.startswith("http://") or v.startswith("https://")):
                setattr(cfg, _attr, "http://" + v)
        if not cfg.ALARM_ENGINE_URL and cfg.MANAGER_URL:
            with best_effort("config: derive alarm-engine url from manager url"):
                from urllib.parse import urlparse, urlunparse
                p = urlparse(cfg.MANAGER_URL)
                if p.hostname:
                    netloc = f"{p.hostname}:8081"
                    cfg.ALARM_ENGINE_URL = urlunparse((p.scheme or "http", netloc, "", "", "", ""))

        cfg._loaded_from = loaded_from
        return cfg

    def to_redacted_dict(self) -> dict[str, Any]:
        """Effective config for /config endpoint (no secrets)."""
        out = {}
        for k in vars(type(self)):
            if k.startswith("_") or not k.isupper():
                continue
            out[k] = getattr(self, k)
        out["_loaded_from"] = getattr(self, "_loaded_from", None)
        return out


logger = logging.getLogger("llm-systems-agent")


def setup_logging(log_file: str, level: str = "INFO") -> None:
    log_dir = os.path.dirname(log_file)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s() - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(logger.handlers):
        logger.removeHandler(h)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except (OSError, PermissionError) as e:
        logger.warning("file handler unavailable for %s: %s", log_file, e)

    # Mirror handlers onto buffered_metric_client so its messages reach the agent log.
    bmc_logger = logging.getLogger("buffered_metric_client")
    bmc_logger.setLevel(logging.INFO)
    for h in list(bmc_logger.handlers):
        bmc_logger.removeHandler(h)
    for h in logger.handlers:
        bmc_logger.addHandler(h)
    bmc_logger.propagate = False

    for noisy in ("urllib3", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


CONFIG: AgentConfig
_runtime_lock = threading.RLock()
_state: dict[str, Any] = {
    "approved": False,
    "agent_id": None,
    "token": None,
    "last_heartbeat_at": None,
    "last_heartbeat_ack": None,
    "last_manager_error": None,
    "last_metric_sample": None,
    "samples_posted": 0,
    "auth_disabled_global": False,
    # Applied AE URL (string, not bool) — _maybe_sync_ae_url re-probes on diff.
    "ae_url_applied": "",
    "llama_state": "unknown",
    "perf_last_transition": None,
    "perf_sleep_count": 0,
    "perf_wake_count": 0,
    "restart_pending": False,
}

_metric_client: Optional[bmc.BufferedMetricClient] = None
_post_session = requests.Session()
# _lms_session moved to agent/providers/lms.py (Tier 3 A2).


def _configure_manager_tls_verify() -> None:
    """Point the outbound requests session at TLS_CA_FILE when MANAGER_URL is https://."""
    if not CONFIG.MANAGER_URL.lower().startswith("https://"):
        return
    ca_path = Path(CONFIG.TLS_CA_FILE)
    if not ca_path.is_absolute():
        ca_path = Path(CONFIG.AGENT_INSTALL_DIR) / ca_path
    if ca_path.is_file():
        _post_session.verify = str(ca_path)
        logger.info("MANAGER_URL is https — outbound calls verify against %s", ca_path)
    else:
        logger.warning(
            "MANAGER_URL=https but CA bundle %s is missing; "
            "outbound calls will fail until the CA arrives. "
            "Run one heartbeat over http:// first (set MANAGER_URL=http... once) "
            "to receive the CA, then switch back.",
            ca_path,
        )


_UPGRADE_DIAG_INTERVAL_S = 300.0


def _diag_throttle(key: str, msg: str, *args, level: int = logging.INFO) -> None:
    """Emit the same diagnostic at most once per _UPGRADE_DIAG_INTERVAL_S."""
    import time as _time
    with _runtime_lock:
        last = _state.setdefault("_diag_throttle", {}).get(key, 0.0)
        now = _time.time()
        if now - last < _UPGRADE_DIAG_INTERVAL_S:
            return
        _state["_diag_throttle"][key] = now
    logger.log(level, msg, *args)


def _maybe_upgrade_manager_https(ack: dict) -> None:
    """Switch control channel to manager's advertised HTTPS URL after probing /health.

    Probe-before-switch: a wrong/unreachable TLS endpoint never strands the heartbeat.
    Value-driven so URL changes propagate within one heartbeat without restart.
    """
    https_url = (ack.get("manager_https_url") or "").strip().rstrip("/")
    with _runtime_lock:
        last_applied = _state.get("mgr_url_applied") or ""
    if not https_url.lower().startswith("https://"):
        _diag_throttle(
            "mgr_https_no_url",
            "control channel: manager advertised no HTTPS URL (manager_https_url=%r); "
            "staying on %s — check [manager].tls_port in the manager TOML",
            https_url, CONFIG.MANAGER_URL or "(unset)",
        )
        return
    if https_url == last_applied and (CONFIG.MANAGER_URL or "").lower() == https_url.lower():
        return
    if (CONFIG.MANAGER_URL or "").lower().startswith("https://") and CONFIG.MANAGER_URL == https_url:
        with _runtime_lock:
            _state["mgr_url_applied"] = https_url
        return
    ca_path = Path(CONFIG.TLS_CA_FILE)
    if not ca_path.is_absolute():
        ca_path = Path(CONFIG.AGENT_INSTALL_DIR) / ca_path
    if not ca_path.is_file():
        _diag_throttle(
            "mgr_https_no_ca",
            "control channel: HTTPS URL %s advertised but CA bundle %s is missing — "
            "agent not yet approved? check the admin tab",
            https_url, ca_path,
        )
        return
    try:
        r = requests.get(f"{https_url}/health", timeout=4, verify=str(ca_path))
        if not r.ok:
            _diag_throttle(
                "mgr_https_probe_status",
                "control channel: HTTPS probe %s returned HTTP %s — staying on %s",
                https_url, r.status_code, CONFIG.MANAGER_URL or "(unset)",
            )
            return
    except Exception as e:
        _diag_throttle(
            "mgr_https_probe_err",
            "control channel: HTTPS probe %s failed (%s: %s) — staying on %s",
            https_url, type(e).__name__, e, CONFIG.MANAGER_URL or "(unset)",
        )
        return
    old = CONFIG.MANAGER_URL
    CONFIG.MANAGER_URL = https_url
    _configure_manager_tls_verify()  # points _post_session.verify at the CA
    with _runtime_lock:
        _state["mgr_url_applied"] = https_url
        # Clear the throttle so a future regression re-logs immediately
        # instead of silently waiting out the 5-min window.
        _state.get("_diag_throttle", {}).pop("mgr_https_no_url", None)
        _state.get("_diag_throttle", {}).pop("mgr_https_no_ca", None)
        _state.get("_diag_throttle", {}).pop("mgr_https_probe_status", None)
        _state.get("_diag_throttle", {}).pop("mgr_https_probe_err", None)
    logger.warning("control channel upgraded to TLS: %s -> %s", old or "(unset)", https_url)


def _ca_bundle_path() -> Path:
    ca_path = Path(CONFIG.TLS_CA_FILE)
    if not ca_path.is_absolute():
        ca_path = Path(CONFIG.AGENT_INSTALL_DIR) / ca_path
    return ca_path


def _configure_ae_tls_verify() -> None:
    """Point the push session at the CA when ALARM_ENGINE_URL is https, so the
    AE's internal-CA-signed cert validates. Mirrors _configure_manager_tls_verify
    for the metric-push channel; safe to call repeatedly (no-op for http)."""
    if not (CONFIG.ALARM_ENGINE_URL or "").lower().startswith("https://"):
        return
    ca_path = _ca_bundle_path()
    if ca_path.is_file():
        _post_session.verify = str(ca_path)


def _maybe_sync_ae_url(ack: dict) -> None:
    """Adopt the alarm-engine URL the manager advertises. When that URL is
    https (AE TLS on), verify against the CA and probe /health BEFORE
    switching, and point the push session at the CA — so a TLS misconfig
    can't silently break metric delivery (we keep the working URL and retry).
    Plain http URLs switch directly.

    Value-driven (not one-shot): every heartbeat re-checks the advertised URL
    against the last one we successfully applied. When they diverge (operator
    edited [manager].alarm_engine_url in the TOML, AE moved hosts, AE TLS got
    toggled, the box was renamed/re-IP'd) we re-probe and re-switch without
    waiting for an agent restart. The previous one-shot flag was the reason a
    new URL after a config change required a restart to take effect."""
    new_ae = (ack.get("alarm_engine_url") or "").strip().rstrip("/")
    with _runtime_lock:
        last_applied = _state.get("ae_url_applied") or ""
    # Empty ack carries no information — wait for a future heartbeat to
    # carry a real URL. Crucially do NOT mark anything applied here: the
    # previous code locked synced=True on the first empty ack and never
    # re-checked, which is what made the test-1 box keep posting to a stale
    # http://localhost:8081 after the operator changed alarm_engine_url.
    if not new_ae:
        _configure_ae_tls_verify()
        return
    if new_ae == last_applied and new_ae == CONFIG.ALARM_ENGINE_URL:
        _configure_ae_tls_verify()
        return
    if new_ae.lower().startswith("https://"):
        ca_path = _ca_bundle_path()
        if not ca_path.is_file():
            _diag_throttle(
                "ae_no_ca",
                "AE push: HTTPS URL %s advertised but CA bundle %s is missing — "
                "agent not yet approved? check the admin tab",
                new_ae, ca_path,
            )
            return
        try:
            r = requests.get(f"{new_ae}/health", timeout=4, verify=str(ca_path))
            if not r.ok:
                _diag_throttle(
                    "ae_probe_status",
                    "AE push: HTTPS probe %s returned HTTP %s — keeping %s",
                    new_ae, r.status_code, CONFIG.ALARM_ENGINE_URL,
                )
                return
        except Exception as e:
            _diag_throttle(
                "ae_probe_err",
                "AE push: HTTPS probe %s failed (%s: %s) — keeping %s",
                new_ae, type(e).__name__, e, CONFIG.ALARM_ENGINE_URL,
            )
            return
        _post_session.verify = str(ca_path)
    old_ae = CONFIG.ALARM_ENGINE_URL
    CONFIG.ALARM_ENGINE_URL = new_ae
    if _metric_client is not None:
        _metric_client.update_alarm_engine_url(new_ae)
    with _runtime_lock:
        _state["ae_url_applied"] = new_ae
        _state.get("_diag_throttle", {}).pop("ae_no_ca", None)
        _state.get("_diag_throttle", {}).pop("ae_probe_status", None)
        _state.get("_diag_throttle", {}).pop("ae_probe_err", None)
    if old_ae != new_ae:
        logger.info("AE URL synced from manager: %s -> %s", old_ae or "(unset)", new_ae)


_log_hb_last = 0.0
_register_403_last = 0.0
_status_poll_warn_last = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_provider() -> Optional[str]:
    with _runtime_lock:
        return _state.get("token")


def _ingest_token_provider() -> Optional[str]:
    """Bearer for alarm-engine metric pushes; prefers shared ingest token over agent bearer."""
    with _runtime_lock:
        return _state.get("ingest_token") or _state.get("token")


def _is_approved() -> bool:
    with _runtime_lock:
        return bool(_state.get("approved"))


# Hardware collectors live in agent/collectors/ (Tier 3 PRs A1a + A1b):
#   _shared.py   — `sensors -j` cache + sensors_val
#   gpu.py       — AMD sysfs + NVIDIA via nvidia-smi
#   ups.py       — upower
#   liquidctl.py — AIO + HX1000i PSU + Smart Device V2
#   system.py    — CPU/RAM/swap/net/disk + iscsi + cpu_governor/cpu_temp_c + collect_system_metrics




# macOS powermetrics — long-lived subprocess streaming plist samples.
# Requires sudoers.d NOPASSWD entry for /usr/bin/powermetrics.

_pm_lock = threading.Lock()
_pm_latest: dict[str, Any] = {}
_pm_proc: Optional[subprocess.Popen] = None
_pm_thread: Optional[threading.Thread] = None
_pm_disabled: bool = False


def _pm_should_run() -> bool:
    if _pm_disabled:
        return False
    if sys.platform != "darwin":
        return False
    return bool(getattr(CONFIG, "COLLECT_POWERMETRICS_ENABLED", True))


def _pm_parse_sample(raw: bytes) -> Optional[dict[str, Any]]:
    """Parse one plist sample. gpu.freq_hz is misnamed (small floats are MHz)."""
    try:
        import plistlib
        doc = plistlib.loads(raw)
    except Exception:
        return None

    out: dict[str, Any] = {}

    def _busy_pct(d: dict) -> Optional[float]:
        idle = d.get("idle_ratio")
        if isinstance(idle, (int, float)):
            return round(max(0.0, (1.0 - float(idle)) * 100.0), 1)
        active = d.get("active_ratio")
        if isinstance(active, (int, float)):
            return round(float(active) * 100.0, 1)
        return None

    proc = doc.get("processor") or {}
    cpu_mw = proc.get("cpu_power")
    if isinstance(cpu_mw, (int, float)):
        out["cpu_package_w"] = round(float(cpu_mw) / 1000.0, 3)
    combined = proc.get("combined_power")
    if isinstance(combined, (int, float)):
        out["soc_total_w"] = round(float(combined) / 1000.0, 3)
    gpu_mw = proc.get("gpu_power")
    if isinstance(gpu_mw, (int, float)):
        out["gpu_w"] = round(float(gpu_mw) / 1000.0, 3)
    ane_mw = proc.get("ane_power")
    if isinstance(ane_mw, (int, float)):
        out["ane_w"] = round(float(ane_mw) / 1000.0, 3)

    # M-series Pro/Max can have multiple P-clusters; aggregate freq=max, busy=mean.
    clusters = proc.get("clusters") or []
    p_freqs: list[float] = []
    p_busy: list[float] = []
    e_freqs: list[float] = []
    e_busy: list[float] = []
    if isinstance(clusters, list):
        for c in clusters:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").upper()
            freq = c.get("freq_hz")
            freq_mhz = round(float(freq) / 1_000_000.0, 1) if isinstance(freq, (int, float)) else None
            busy = _busy_pct(c)
            tgt_f, tgt_b = (p_freqs, p_busy) if name.startswith("P") else (
                            (e_freqs, e_busy) if name.startswith("E") else (None, None))
            if tgt_f is None:
                continue
            if freq_mhz is not None and freq_mhz > 0:
                tgt_f.append(freq_mhz)
            if busy is not None:
                tgt_b.append(busy)
    if p_freqs: out["pcore_freq_mhz"] = round(max(p_freqs), 1)
    if e_freqs: out["ecore_freq_mhz"] = round(max(e_freqs), 1)
    if p_busy:  out["pcore_util_pct"] = round(sum(p_busy) / len(p_busy), 1)
    if e_busy:  out["ecore_util_pct"] = round(sum(e_busy) / len(e_busy), 1)

    gpu = doc.get("gpu") or {}
    if isinstance(gpu, dict):
        freq = gpu.get("freq_hz")
        if isinstance(freq, (int, float)):
            f = float(freq)
            out["gpu_freq_mhz"] = round(f / 1_000_000.0 if f > 1_000_000 else f, 1)
        busy = _busy_pct(gpu)
        if busy is not None:
            out["gpu_busy_pct"] = busy

    thermal = doc.get("thermal_pressure")
    if isinstance(thermal, str):
        out["thermal_pressure"] = thermal
        out["thermal_pressure_n"] = {
            "Nominal": 0, "Fair": 1, "Serious": 2, "Critical": 3,
        }.get(thermal, 0)

    net = doc.get("network") or {}
    if isinstance(net, dict):
        ipi = net.get("ipacket_rate") or net.get("ipackets_per_sec")
        opi = net.get("opacket_rate") or net.get("opackets_per_sec")
        if isinstance(ipi, (int, float)):
            out["net_in_pkts_s"] = round(float(ipi), 1)
        if isinstance(opi, (int, float)):
            out["net_out_pkts_s"] = round(float(opi), 1)
    return out or None


def _pm_reader_loop() -> None:
    """Read NUL-delimited plist samples and refresh the latest snapshot."""
    global _pm_proc, _pm_disabled
    assert _pm_proc is not None and _pm_proc.stdout is not None
    buf = bytearray()
    try:
        while True:
            chunk = _pm_proc.stdout.read(8192)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                idx = buf.find(b"\x00")
                if idx < 0:
                    break
                raw, buf = bytes(buf[:idx]), buf[idx + 1:]
                if raw.strip():
                    parsed = _pm_parse_sample(raw)
                    if parsed:
                        with _pm_lock:
                            _pm_latest.clear()
                            _pm_latest.update(parsed)
    except Exception as e:
        logger.debug("powermetrics reader error: %s", e)
    finally:
        # Disable after any exit to avoid respawn loops when powermetrics is broken.
        if _pm_proc is not None:
            with best_effort("powermetrics: reap reader process"):
                _pm_proc.wait(timeout=1)
        rc = _pm_proc.returncode if _pm_proc is not None else None
        _pm_disabled = True
        logger.warning("powermetrics terminated (rc=%s) — disabling further restarts", rc)


def _pm_ensure_running() -> None:
    global _pm_proc, _pm_thread, _pm_disabled
    if not _pm_should_run():
        return
    if _pm_proc is not None and _pm_proc.poll() is None:
        return
    interval_ms = max(1000, int(getattr(CONFIG, "POWERMETRICS_INTERVAL_MS", 5000)))
    # macOS 26 removed the `smc` sampler — keep this list to surviving samplers.
    samplers = "cpu_power,gpu_power,thermal,network"
    cmd = ["sudo", "-n", "/usr/bin/powermetrics",
           "--samplers", samplers,
           "-i", str(interval_ms),
           "-f", "plist"]
    try:
        _pm_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except FileNotFoundError as e:
        _pm_disabled = True
        logger.warning("powermetrics not available: %s", e)
        return
    _pm_thread = threading.Thread(target=_pm_reader_loop, name="pm-reader", daemon=True)
    _pm_thread.start()
    logger.info("powermetrics started (interval=%sms)", interval_ms)


def collect_powermetrics() -> dict[str, Any]:
    """Latest powermetrics sample or {} when unavailable / disabled."""
    if not _pm_should_run():
        return {}
    _pm_ensure_running()
    with _pm_lock:
        if not _pm_latest:
            return {}
        return dict(_pm_latest)


def _match_processes(pattern: str) -> list[psutil.Process]:
    needle = (pattern or "").lower()
    if not needle:
        return []
    matches: list[psutil.Process] = []
    for p in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if needle in name or needle in cmd:
                matches.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _systemd_unit_state(unit: str) -> dict[str, Any]:
    """Returns {running, pid, rss_mb, uptime_s, installed}; installed=False if LoadState != loaded."""
    if sys.platform == "darwin":
        return {"available": False, "installed": False}
    out: dict[str, Any] = {"available": True, "installed": True, "running": False,
                           "pid": None, "rss_mb": None, "uptime_s": None}
    try:
        r = subprocess.run(
            ["systemctl", "show", unit,
             "--property=LoadState,ActiveState,MainPID,MemoryCurrent,ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0 and not r.stdout:
            return out
        props: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        load_state = props.get("LoadState", "")
        if load_state and load_state != "loaded":
            out["installed"] = False
            return out
        out["running"] = props.get("ActiveState", "") == "active"
        try:
            pid = int(props.get("MainPID") or 0)
            if pid > 0:
                out["pid"] = pid
        except ValueError:
            pass
        try:
            mem = int(props.get("MemoryCurrent") or 0)
            if mem > 0:
                out["rss_mb"] = round(mem / (1024 * 1024), 1)
        except ValueError:
            pass
        if out["pid"]:
            try:
                proc = psutil.Process(out["pid"])
                out["uptime_s"] = int(time.time() - proc.create_time())
                if out["rss_mb"] is None:
                    out["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        out["available"] = False
    except Exception as e:
        logger.debug("systemctl show %s: %s", unit, e)
    return out


def collect_process_watchlist() -> list[dict[str, Any]]:
    """Snapshot watchlist running state; systemd/process entries carry mode-specific fields."""
    if not getattr(CONFIG, "PROCESS_WATCH_ENABLED", True):
        return []
    items: list[dict[str, Any]] = []
    for entry in (CONFIG.PROCESS_WATCHLIST or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        mode = str(entry.get("match") or "process").strip().lower()
        if mode == "systemd":
            state = _systemd_unit_state(str(entry.get("unit") or ""))
            if not state.get("installed", True):
                continue
            items.append({
                "name": name,
                "match": "systemd",
                "unit": entry.get("unit"),
                # int 0/1 — metric_flatten.flatten deliberately skips bools.
                "running": 1 if state.get("running") else 0,
                "available": 1 if state.get("available", True) else 0,
                "pid": state.get("pid"),
                "rss_mb": state.get("rss_mb"),
                "uptime_s": state.get("uptime_s"),
            })
        else:
            procs = _match_processes(str(entry.get("pattern") or ""))
            rss = 0
            for p in procs:
                try:
                    rss += p.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            primary = procs[0] if procs else None
            uptime = None
            pid = None
            if primary is not None:
                try:
                    pid = primary.pid
                    uptime = int(time.time() - primary.create_time())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            items.append({
                "name": name,
                "match": "process",
                "pattern": entry.get("pattern"),
                "running": 1 if procs else 0,
                "count": len(procs),
                "pid": pid,
                "rss_mb": round(rss / (1024 * 1024), 1) if rss else 0.0,
                "uptime_s": uptime,
            })
    return items


# collect_system_metrics moved to agent/collectors/system.py in Tier 3 PR A1b;
# re-exported via `from collectors.system import collect_system_metrics` above.




# LMS helpers (lms_get_models/lms_get_ps/lms_get_status) moved to
# agent/providers/lms.py (Tier 3 A2); re-exported above.
# llama helpers (llama_get_state, collect_llama_for_metrics) + perf-controller moved to
# agent/providers/llama.py (Tier 3 A3); re-exported above. start_background() called from _lifespan.




def _capabilities() -> dict[str, bool]:
    # 'sysperf' is always true so system-only agents have a non-empty capability set.
    return {
        "sysperf": True,
        "lms": CONFIG.LMS_ENABLED,
        "llama": CONFIG.LLAMA_ENABLED,
        "openclaw": CONFIG.OPENCLAW_ENABLED,
        "image_gen": CONFIG.IMGGEN_ENABLED,
        "perf_controller": CONFIG.PERF_CONTROLLER_ENABLED,
        "monitor_manager": CONFIG.MONITOR_MANAGER_ENABLED,
        "monitor_alarm_engine": CONFIG.MONITOR_ALARM_ENGINE_ENABLED,
    }


def _provider_specs() -> "list[dict]":
    """Each provider module under agent/providers/ may expose a PROVIDER_SPEC
    constant ({name, capability_key, push_endpoint, ...}). Manager-side PR3+
    uses this to discover what an agent serves — terminal lacks a spec so it's
    skipped. Returns [] when providers package isn't importable yet."""
    try:
        import providers as _ag_providers  # type: ignore
    except Exception:
        return []
    out: list[dict] = []
    for mod in getattr(_ag_providers, "_MODULES", ()):
        spec = getattr(mod, "PROVIDER_SPEC", None)
        if not spec:
            continue
        cap_key = spec.get("capability_key") or spec.get("name") or ""
        enabled = getattr(CONFIG, f"{cap_key.upper()}_ENABLED", False) if cap_key else False
        out.append({
            "name": spec.get("name"),
            "capability_key": cap_key,
            "push_endpoint": spec.get("push_endpoint"),
            "enabled": bool(enabled),
        })
    return out


# Background prober for manager / alarm-engine / Influx latencies.
# Results land in `_meta_perf_state` and ride along on the next collector tick.
# Source name after metric_flatten.resolve() is `manager_self_monitor`.

_meta_perf_state: dict[str, Optional[float]] = {}
_meta_perf_lock = threading.Lock()

# Reused InfluxDB clients for the probe; rebuilt only when conn details change.
# Accessed only by the single meta-perf thread.
_influx_client_cache: dict = {"main": None, "main_key": None,
                              "rollup": None, "rollup_key": None}


def _influx_drop_client(slot: str) -> None:
    """Close and forget the cached probe client in `slot`."""
    client = _influx_client_cache.get(slot)
    if client is not None:
        with best_effort(f"influx probe: close {slot} client"):
            client.close()
    _influx_client_cache[slot] = None
    _influx_client_cache[slot + "_key"] = None


def _influx_reset_clients() -> None:
    """Drop both cached probe clients (config gone/disabled)."""
    _influx_drop_client("main")
    _influx_drop_client("rollup")


def _probe_http_latency(
    method: str, url: str, timeout: float, body: Any = None,
    expect_status: tuple[int, ...] = (200,),
    headers: Optional[dict[str, str]] = None,
) -> Optional[float]:
    """Time a single HTTP request, return latency in ms or None on failure."""
    try:
        t0 = time.perf_counter()
        if method == "GET":
            r = _post_session.get(url, timeout=timeout, headers=headers)
        else:
            r = _post_session.post(url, json=body, timeout=timeout, headers=headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if r.status_code not in expect_status:
            return None
        return elapsed_ms
    except Exception as e:
        logger.debug("probe %s %s failed: %s", method, url, e)
        return None


def _probe_ae_health_with_cycle() -> tuple[Optional[float], Optional[float]]:
    """GET AE /health; returns (latency_ms, components.rule_eval_last_cycle_ms or None)."""
    if not CONFIG.ALARM_ENGINE_URL:
        return None, None
    base = CONFIG.ALARM_ENGINE_URL.rstrip("/")
    try:
        t0 = time.perf_counter()
        r = _post_session.get(f"{base}/health", timeout=CONFIG.META_PERF_TIMEOUT_S)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if r.status_code != 200:
            return None, None
        comps = (r.json() or {}).get("components", {}) or {}
        return elapsed, comps.get("rule_eval_last_cycle_ms")
    except Exception as e:
        logger.debug("probe AE /health failed: %s", e)
        return None, None


def _probe_ae_ingest() -> Optional[float]:
    """Synthetic batch ingest — POST one tiny sample tagged with a sentinel host."""
    if not CONFIG.ALARM_ENGINE_URL:
        return None
    base = CONFIG.ALARM_ENGINE_URL.rstrip("/")
    payload = {
        "host": "__probe__",
        "samples": [{
            "ts": _now_iso(),
            "host": "__probe__",
            "__probe_meta": {"latency_synthetic": 1.0},
        }],
    }
    tok = _ingest_token_provider()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        t0 = time.perf_counter()
        r = _post_session.post(
            f"{base}/api/alarm/metrics/ingest",
            json=payload, headers=headers,
            timeout=CONFIG.META_PERF_TIMEOUT_S,
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        if r.status_code not in (200, 201):
            return None
        return elapsed
    except Exception as e:
        logger.debug("probe AE ingest failed: %s", e)
        return None


def _probe_influxdb() -> dict[str, Optional[float]]:
    """Probe InfluxDB write + 5m/24h query latencies; failed keys stay None."""
    out: dict[str, Optional[float]] = {
        "influx_write_latency_ms":     None,
        "influx_query_5m_latency_ms":  None,
        "influx_query_24h_latency_ms": None,
    }
    cfg = unified_config_reader.read_influx_settings_cached(CONFIG.UNIFIED_CONFIG_TOML_PATH)
    if cfg is None:
        _influx_reset_clients()
        _diag_throttle(
            "influx_cfg_unavailable",
            "influx probe: unified-config TOML unavailable; influx self-monitor disabled",
            level=logging.DEBUG,
        )
        return out
    with _runtime_lock:
        _state.get("_diag_throttle", {}).pop("influx_cfg_unavailable", None)

    host = cfg["host"]
    port = cfg["port"]
    org = cfg["org"]
    bucket = cfg["metrics_bucket"]
    rollup_bucket = cfg["metrics_rollup_bucket"]
    token = cfg["token"]
    rollup_token = cfg["rollup_token"]

    if not (host and token):
        _influx_reset_clients()
        return out

    url = f"http://{host}:{port}"
    try:
        from influxdb_client import InfluxDBClient, Point  # type: ignore
        from influxdb_client.client.write_api import SYNCHRONOUS  # type: ignore
    except Exception as e:
        logger.debug("influx probe: influxdb_client unavailable: %s", e)
        return out

    def _get_client(slot: str, tok: str):
        key = (url, tok, org)
        c = _influx_client_cache
        if c[slot] is not None and c[slot + "_key"] == key:
            return c[slot]
        if c[slot] is not None:
            with best_effort(f"influx probe: close stale {slot} client"):
                c[slot].close()
            c[slot] = None
            c[slot + "_key"] = None
        client = InfluxDBClient(url=url, token=tok, org=org,
                                timeout=int(CONFIG.META_PERF_TIMEOUT_S * 1000))
        c[slot] = client
        c[slot + "_key"] = key
        return client

    try:
        client = _get_client("main", token)
    except Exception as e:
        logger.debug("influx probe: client init failed: %s", e)
        _influx_drop_client("main")
        return out

    main_ok = True
    try:
        wapi = client.write_api(write_options=SYNCHRONOUS)
        p = (
            Point("__probe__")
            .tag("source", "manager_self_monitor")
            .field("latency_synthetic", 1.0)
        )
        t0 = time.perf_counter()
        wapi.write(bucket=bucket, record=p)
        out["influx_write_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        logger.debug("influx write probe failed: %s", e)

    qapi = client.query_api()
    flux_5m = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: -5m) '
        f'|> filter(fn: (r) => r._measurement == "metrics") '
        f'|> limit(n: 10)'
    )
    try:
        t0 = time.perf_counter()
        _ = qapi.query(flux_5m)
        out["influx_query_5m_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        logger.debug("influx 5m query probe failed: %s", e)
        main_ok = False

    # 24h reads the rollup bucket via its scoped token; falls back to the
    # raw bucket when no rollup token is set.
    use_rollup = bool(rollup_token) and bool(rollup_bucket) and rollup_bucket != bucket
    if not use_rollup:
        _influx_drop_client("rollup")
    q24_bucket = rollup_bucket if use_rollup else bucket
    flux_24h = (
        f'from(bucket: "{q24_bucket}") '
        f'|> range(start: -24h) '
        f'|> filter(fn: (r) => r._measurement == "metrics") '
        f'|> limit(n: 10)'
    )
    rollup_ok = True
    try:
        q24_api = _get_client("rollup", rollup_token).query_api() if use_rollup else qapi
        t0 = time.perf_counter()
        _ = q24_api.query(flux_24h)
        out["influx_query_24h_latency_ms"] = (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        logger.debug("influx 24h query probe failed: %s", e)
        if use_rollup:
            rollup_ok = False

    # Self-heal: drop a client whose canonical query failed so the next tick
    # rebuilds, preserving the recovery the per-tick rebuild used to give.
    if not main_ok:
        _influx_drop_client("main")
    if use_rollup and not rollup_ok:
        _influx_drop_client("rollup")

    return out


def _meta_perf_loop() -> None:
    """Background prober; writes results into _meta_perf_state for the next collector tick."""
    while True:
        try:
            results: dict[str, Optional[float]] = {}

            if CONFIG.MONITOR_MANAGER_ENABLED and CONFIG.MANAGER_URL:
                base = CONFIG.MANAGER_URL.rstrip("/")
                # Probes hit auth-gated routes; agent bearer bypasses _auth_gate.
                mgr_tok = _state.get("token") or ""
                mgr_hdrs = {"Authorization": f"Bearer {mgr_tok}"} if mgr_tok else None
                results["manager_api_latency_ms"] = _probe_http_latency(
                    "GET", f"{base}/api/metrics",
                    CONFIG.META_PERF_TIMEOUT_S, headers=mgr_hdrs,
                )
                results["manager_history_latency_ms"] = _probe_http_latency(
                    "GET", f"{base}/api/history?since_minutes=60",
                    CONFIG.META_PERF_TIMEOUT_S, headers=mgr_hdrs,
                )

            if CONFIG.MONITOR_ALARM_ENGINE_ENABLED:
                ae_health_ms, last_cycle_ms = _probe_ae_health_with_cycle()
                results["ae_health_latency_ms"] = ae_health_ms
                results["rule_eval_cycle_ms"] = last_cycle_ms

                results["ae_ingest_latency_ms"] = _probe_ae_ingest()

                if CONFIG.ALARM_ENGINE_URL:
                    base = CONFIG.ALARM_ENGINE_URL.rstrip("/")
                    results["ae_query_24h_latency_ms"] = _probe_http_latency(
                        "GET",
                        f"{base}/api/alarm/metrics/system/cpu_total?since_minutes=1440",
                        CONFIG.META_PERF_TIMEOUT_S,
                    )

                results.update(_probe_influxdb())

            with _meta_perf_lock:
                _meta_perf_state.clear()
                _meta_perf_state.update(results)
        except Exception:
            logger.exception("meta_perf loop tick failed")
        time.sleep(max(5, int(CONFIG.META_PERF_INTERVAL_S)))


def _build_meta_perf_block() -> Optional[dict[str, float]]:
    """Snapshot probe results sans None; returns None on cold start."""
    with _meta_perf_lock:
        block = {k: v for k, v in _meta_perf_state.items() if v is not None}
    return block or None


def _tls_paths() -> tuple[Path, Path]:
    """Resolve TLS cert+key paths; CONFIG paths can be absolute or relative to AGENT_INSTALL_DIR."""
    install = Path(CONFIG.AGENT_INSTALL_DIR)
    crt = Path(CONFIG.TLS_CERT_FILE)
    key = Path(CONFIG.TLS_KEY_FILE)
    if not crt.is_absolute():
        crt = install / crt
    if not key.is_absolute():
        key = install / key
    return crt, key


def _tls_enabled() -> bool:
    """True when both cert and key files exist and are readable. The
    operator drops the trio in via the manager's cert-bundle endpoint;
    until they do, the agent stays on plain HTTP."""
    crt, key = _tls_paths()
    return crt.is_file() and key.is_file()


# True iff the RUNNING uvicorn was bound with TLS (vs cert files merely on disk).
_SERVED_WITH_TLS = False


def _tls_cert_san_ips() -> list[str]:
    """Return SAN IPs from the cert, or [] on missing/parse-fail."""
    crt, _ = _tls_paths()
    if not crt.is_file():
        return []
    try:
        st = crt.stat()
        cache_key = (st.st_mtime, st.st_size)
        cached = getattr(_tls_cert_san_ips, "_cache_key", None)
        if cached == cache_key:
            return _tls_cert_san_ips._cache_val  # type: ignore[attr-defined]
        out = subprocess.check_output(
            ["openssl", "x509", "-in", str(crt), "-noout", "-ext", "subjectAltName"],
            text=True, timeout=2,
        )
        ips: list[str] = []
        for tok in out.replace("\n", ",").split(","):
            tok = tok.strip()
            if tok.lower().startswith("ip address:"):
                ips.append(tok.split(":", 1)[1].strip())
        _tls_cert_san_ips._cache_key = cache_key   # type: ignore[attr-defined]
        _tls_cert_san_ips._cache_val = ips         # type: ignore[attr-defined]
        return ips
    except Exception:
        return []


def _tls_cert_expiry_iso() -> Optional[str]:
    """Parse cert's not_after as ISO-8601; None on missing/parse-fail (manager treats both as 'reissue')."""
    crt, _ = _tls_paths()
    if not crt.is_file():
        return None
    try:
        # Cached by mtime+size so the 60s heartbeat doesn't fork openssl every time.
        st = crt.stat()
        cache_key = (st.st_mtime, st.st_size)
        if getattr(_tls_cert_expiry_iso, "_cache_key", None) == cache_key:
            return _tls_cert_expiry_iso._cache_val  # type: ignore[attr-defined]
        out = subprocess.check_output(
            ["openssl", "x509", "-in", str(crt), "-noout", "-enddate"],
            text=True, timeout=2,
        )
        if "=" not in out:
            return None
        date_str = out.split("=", 1)[1].strip()
        from datetime import datetime as _dt
        dt = _dt.strptime(date_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        iso = dt.isoformat()
        _tls_cert_expiry_iso._cache_key = cache_key   # type: ignore[attr-defined]
        _tls_cert_expiry_iso._cache_val = iso         # type: ignore[attr-defined]
        return iso
    except Exception:
        return None


def _tls_write_bundle(tls: dict) -> None:
    """Persist {cert_pem, key_pem, ca_pem} from heartbeat ack atomically. Key 0600, others 0644."""
    crt_path, key_path = _tls_paths()
    ca_path = Path(CONFIG.TLS_CA_FILE)
    if not ca_path.is_absolute():
        ca_path = Path(CONFIG.AGENT_INSTALL_DIR) / ca_path

    for p in (crt_path, key_path, ca_path):
        p.parent.mkdir(parents=True, exist_ok=True)

    cert_pem = tls.get("cert_pem") or ""
    key_pem  = tls.get("key_pem")  or ""
    ca_pem   = tls.get("ca_pem")   or ""
    if not (cert_pem and key_pem and ca_pem):
        logger.warning("heartbeat-ack TLS block incomplete; ignoring")
        return
    atomic_write_text(crt_path, cert_pem, mode=0o644)
    atomic_write_text(key_path, key_pem,  mode=0o600)
    atomic_write_text(ca_path,  ca_pem,   mode=0o644)
    if hasattr(_tls_cert_expiry_iso, "_cache_key"):
        delattr(_tls_cert_expiry_iso, "_cache_key")
    reason = tls.get("reason") or "issued"
    expires = tls.get("expires_at") or "?"

    # First-time receipt: uvicorn is on plain HTTP. Self-restart so the next
    # process binds HTTPS. Rotations skip this — operator stays in control.
    if not _SERVED_WITH_TLS:
        logger.warning(
            "TLS bundle %s (expires %s) — auto-restarting to bind HTTPS on %s:%d",
            reason, expires, CONFIG.AGENT_BIND_HOST, CONFIG.AGENT_BIND_PORT,
        )
        import threading as _th, signal as _sig
        def _restart_for_tls() -> None:
            # Brief delay so the heartbeat handler can return before SIGTERM.
            time.sleep(2)
            logger.info("TLS bundle received — sending SIGTERM to pick up HTTPS bind")
            os.kill(os.getpid(), _sig.SIGTERM)
        _th.Thread(target=_restart_for_tls, name="tls-restart", daemon=True).start()
        return

    logger.warning(
        "TLS bundle %s (expires %s) — RESTART_REQUIRED to bind HTTPS on %s:%d",
        reason, expires, CONFIG.AGENT_BIND_HOST, CONFIG.AGENT_BIND_PORT,
    )


def _registration_body() -> dict[str, Any]:
    fp_input = f"{CONFIG.AGENT_HOSTNAME}|{CONFIG.AGENT_OS}|{platform.uname()}|{psutil.boot_time()}"
    import hashlib
    fingerprint = "sha256:" + hashlib.sha256(fp_input.encode()).hexdigest()
    scheme = "https" if _tls_enabled() else "http"
    return {
        "hostname": CONFIG.AGENT_HOSTNAME,
        "os": CONFIG.AGENT_OS,
        "role": CONFIG.AGENT_ROLE,
        "bind_url": f"{scheme}://{_advertise_host()}:{CONFIG.AGENT_BIND_PORT}",
        "version": VERSION,
        "description": CONFIG.AGENT_DESCRIPTION,
        "capabilities": _capabilities(),
        "providers": _provider_specs(),
        "fingerprint": fingerprint,
        "agent_user": CONFIG.AGENT_USER,
        "image_gen_port": CONFIG.IMGGEN_PORT,
    }


def _pick_non_loopback_ip() -> Optional[str]:
    """First non-loopback IPv4; fallback when MANAGER_URL routes via 127.0.0.1."""
    try:
        # UDP "connect" — kernel picks default-route source, no packets sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("1.1.1.1", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError as e:
        logger.debug("default-route IP probe failed: %s (falling back to hostname lookup)", e)
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None,
                                                    family=socket.AF_INET):
            ip = sa[0]
            if ip and not ip.startswith("127."):
                return ip
    except (OSError, socket.gaierror) as e:
        logger.debug("hostname IP fallback failed: %s", e)
    return None


def _advertise_host() -> str:
    """Routable address the manager will dial back on; substitutes LAN IP for loopback routes."""
    if CONFIG.AGENT_BIND_HOST not in ("0.0.0.0", "::", ""):
        return CONFIG.AGENT_BIND_HOST

    try:
        from urllib.parse import urlparse
        parsed = urlparse(CONFIG.MANAGER_URL)
        target_host = parsed.hostname or "8.8.8.8"
        target_port = parsed.port or 80
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect((target_host, target_port))
            src = s.getsockname()[0]
            if src and not src.startswith("127."):
                return src
            lan = _pick_non_loopback_ip()
            if lan:
                logger.info(
                    "MANAGER_URL=%s routes via loopback; advertising %s instead of %s",
                    CONFIG.MANAGER_URL, lan, src)
                return lan
            return src
    except Exception as e:
        # MANAGER_URL probe failed (most often gaierror — the agent host
        # can't resolve the manager's hostname yet, e.g. DNS not warmed up
        # at boot). Try a manager-independent IP discovery before falling
        # back to socket.gethostname(), which forces the manager to depend
        # on the agent's hostname being resolvable from the dashboard side
        # (the smoke test and any non-manager LAN dialer do not get the
        # manager's registered_from-substitution fallback).
        lan = _pick_non_loopback_ip()
        if lan:
            logger.info(
                "MANAGER_URL probe failed (%s); advertising %s instead of hostname",
                e, lan,
            )
            return lan
        logger.warning("could not detect routable IP, falling back to hostname: %s", e)
        return CONFIG.AGENT_HOSTNAME


def _persist_token(token: str) -> None:
    p = Path(CONFIG.TOKEN_FILE)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token)
        os.chmod(p, 0o600)
    except Exception as e:
        logger.warning("failed to persist token to %s: %s", p, e)


def _load_token() -> Optional[str]:
    p = Path(CONFIG.TOKEN_FILE)
    if not p.is_file():
        return None
    try:
        t = p.read_text().strip()
        return t or None
    except Exception:
        return None


def registry_register_blocking() -> None:
    """Register with the manager, then poll until approved.

    Uses a cached token if present and validated. Once approved, sets
    _state['approved']=True and _state['token']=<bearer>.
    """
    cached = _load_token()
    if cached:
        try:
            r = _post_session.get(
                f"{CONFIG.MANAGER_URL.rstrip('/')}/api/agents/whoami",
                headers={"Authorization": f"Bearer {cached}"},
                timeout=5,
            )
            if r.ok:
                d = r.json()
                if d.get("status") == "approved":
                    with _runtime_lock:
                        _state["approved"] = True
                        _state["token"] = cached
                        _state["agent_id"] = d.get("agent_id")
                    logger.info("token cache validated; agent_id=%s", d.get("agent_id"))
                    # Re-POST registration so bind_url (http→https) refreshes; manager matches by (hostname, os) and updates in place.
                    try:
                        body = _registration_body()
                        rr = _post_session.post(
                            f"{CONFIG.MANAGER_URL.rstrip('/')}/api/agents/register",
                            json=body, timeout=5,
                        )
                        if rr.ok:
                            logger.info("registration refresh OK (bind_url=%s)", body.get("bind_url"))
                        else:
                            logger.warning("registration refresh returned %s: %s",
                                           rr.status_code, rr.text[:160])
                    except Exception as e:
                        logger.warning("registration refresh failed: %s", e)
                    return
        except Exception as e:
            logger.debug("whoami check failed (will re-register): %s", e)

    body = _registration_body()
    while True:
        try:
            r = _post_session.post(
                f"{CONFIG.MANAGER_URL.rstrip('/')}/api/agents/register",
                json=body, timeout=10,
            )
            if r.ok:
                d = r.json()
                agent_id = d.get("agent_id")
                status = d.get("status")
                with _runtime_lock:
                    _state["agent_id"] = agent_id
                if status == "approved":
                    tok = d.get("token")
                    if tok:
                        _persist_token(tok)
                        with _runtime_lock:
                            _state["approved"] = True
                            _state["token"] = tok
                        logger.info("registered + auto-approved (agent_id=%s)", agent_id)
                        return
                logger.info(
                    "registration accepted; status=%s agent_id=%s — waiting for admin approval",
                    status, agent_id,
                )
                break
            else:
                # 403 = stale record on the manager; surface recovery steps once per 5min.
                if r.status_code == 403:
                    global _register_403_last
                    _now = time.time()
                    if _now - _register_403_last >= 300:
                        _register_403_last = _now
                        try:
                            body = r.json() or {}
                        except Exception:
                            body = {}
                        stale_id = body.get("agent_id") or "<unknown>"
                        stale_status = body.get("status") or "<unknown>"
                        base = (CONFIG.MANAGER_URL or "").rstrip("/")
                        logger.critical(
                            "registration BLOCKED by stale record on the manager.\n"
                            "      Manager already has an %s agent for hostname=%r (id=%s).\n"
                            "      The new install has no matching token/IP/fingerprint,\n"
                            "      so the manager refuses to hand over the existing record.\n"
                            "      Recovery (one of):\n"
                            "        1. Admin tab → find agent %s → click the ⋯ menu → Delete,\n"
                            "           then this agent will re-register fresh.\n"
                            "           %s/?tab=admin#agent=%s\n"
                            "        2. If you have the original token, restore it to\n"
                            "           %s and restart this agent.\n"
                            "      Will keep retrying every 15s; next reminder in 5 min.",
                            stale_status, CONFIG.AGENT_HOSTNAME, stale_id,
                            stale_id, base, stale_id,
                            CONFIG.TOKEN_FILE,
                        )
                else:
                    logger.warning(
                        "registration rejected: %s %s",
                        r.status_code, r.text[:200],
                    )
        except Exception as e:
            logger.warning("registration POST failed: %s", e)
        time.sleep(15)

    while not _state.get("approved"):
        agent_id = _state.get("agent_id")
        if not agent_id:
            time.sleep(15)
            continue
        try:
            r = _post_session.get(
                f"{CONFIG.MANAGER_URL.rstrip('/')}/api/agents/{agent_id}/status",
                timeout=10,
            )
            if r.ok:
                d = r.json()
                if d.get("status") == "approved":
                    tok = d.get("token")
                    if tok:
                        _persist_token(tok)
                        with _runtime_lock:
                            _state["approved"] = True
                            _state["token"] = tok
                        logger.info("approved by admin; agent_id=%s", agent_id)
                        return
            else:
                global _status_poll_warn_last
                _now_warn = time.time()
                if _now_warn - _status_poll_warn_last >= 300:
                    _status_poll_warn_last = _now_warn
                    logger.warning(
                        "status poll returned HTTP %s for agent_id=%s — "
                        "approval cannot be picked up until this clears "
                        "(body=%s)",
                        r.status_code, agent_id, (r.text or "")[:160],
                    )
        except Exception as e:
            logger.debug("status poll failed: %s", e)
        time.sleep(30)


def heartbeat_loop() -> None:
    """Posts /api/agents/heartbeat every HEARTBEAT_INTERVAL_S.

    Fires the first heartbeat immediately, *then* sleeps. The ack carries
    the manager's https URL, the AE URL, the ingest token, and (on first
    receipt) the TLS bundle — one-shots like _maybe_upgrade_manager_https
    and _maybe_sync_ae_url only fire from the ack, so a sleep-first loop
    left a 60s window after every restart where the agent was up but
    pointed at stale URLs. Fire-first collapses that window to ~1s.
    """
    first = True
    while True:
        try:
            if first:
                first = False
            else:
                time.sleep(CONFIG.HEARTBEAT_INTERVAL_S)
            with _runtime_lock:
                tok = _state.get("token")
                aid = _state.get("agent_id")
            if not (tok and aid):
                continue
            body = {
                "agent_id": aid,
                "ts": _now_iso(),
                "collection_enabled": CONFIG.COLLECTION_ENABLED,
                # Only report llama_state when llama is actually enabled —
                # otherwise the manager records 'unknown' which looks like
                # an error rather than 'this agent doesn't track llama'.
                # When perf controller is running, _state['llama_state'] is the
                # truth (transition-driven). Otherwise fall back to llama_get_state(),
                "llama_state": (
                    _state.get("llama_state")
                    if (CONFIG.LLAMA_ENABLED and _state.get("llama_state") in ("awake", "sleeping"))
                    else (llama_get_state() if CONFIG.LLAMA_ENABLED else None)
                ),
                "samples_posted": _state.get("samples_posted"),
                "version": VERSION,
                "has_tls_cert": _tls_enabled(),
                "tls_expires_at": _tls_cert_expiry_iso(),
                "tls_san_ips": _tls_cert_san_ips(),
                "control_channel_tls": (CONFIG.MANAGER_URL or "").lower().startswith("https://"),
                "providers": _provider_specs(),
            }
            r = _post_session.post(
                f"{CONFIG.MANAGER_URL.rstrip('/')}/api/agents/heartbeat",
                json=body,
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            if r.ok:
                ack = r.json() or {}
                # Outside the lock to keep file I/O off it.
                tls = ack.get("tls")
                if tls:
                    try:
                        _tls_write_bundle(tls)
                    except Exception as e:
                        logger.exception("TLS bundle write failed: %s", e)
                with _runtime_lock:
                    was_approved = _state.get("approved")
                    _state["last_heartbeat_at"] = _now_iso()
                    _state["last_heartbeat_ack"] = ack
                    _state["auth_disabled_global"] = bool(ack.get("auth_disabled", False))
                    # HMAC secret for offline stream-token verification.
                    secret_hex = ack.get("manager_secret") or ""
                    if secret_hex:
                        try:
                            _state["manager_secret"] = bytes.fromhex(secret_hex)
                        except ValueError:
                            logger.warning("manager_secret in heartbeat ack is not hex; ignoring")
                    _state["ingest_token"] = ack.get("ingest_token") or ""
                    # PR2: the manager sends `default_for` (the list of providers
                    # this agent is the default for). Old managers only send
                    # `is_primary_llama` — fall back to that and synthesize an
                    # equivalent default_for list so downstream consumers don't
                    # have to branch.
                    df = ack.get("default_for")
                    if df is not None:
                        _state["default_for"] = list(df)
                        _state["is_primary_llama"] = "llama" in _state["default_for"]
                    else:
                        is_prim = bool(ack.get("is_primary_llama"))
                        _state["is_primary_llama"] = is_prim
                        _state["default_for"] = ["llama"] if is_prim else []
                    # Re-flip approved in case a prior 401 had cleared it.
                    _state["approved"] = True
                if not was_approved:
                    logger.info("heartbeat succeeded after re-enable — collection resumed")
                _maybe_upgrade_manager_https(ack)
                _maybe_sync_ae_url(ack)
            elif r.status_code in (401, 403):
                with _runtime_lock:
                    was_approved = _state.get("approved")
                    _state["approved"] = False
                if was_approved:
                    logger.warning(
                        "heartbeat rejected (%s); agent disabled or token revoked — pausing collection",
                        r.status_code,
                    )
            else:
                logger.warning("heartbeat HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            logger.debug("heartbeat exception: %s", e)


# `du -sb` on the TSM tree is expensive at scale; cache the result.
_INFLUXDB_BYTES_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _influxdb_data_dirs() -> list[str]:
    """InfluxDB data-dir candidates: CONFIG → $INFLUXD_ENGINE_PATH → /var/lib/influxdb{,2}."""
    candidates: list[str] = []
    if CONFIG.INFLUXDB_DATA_PATH:
        candidates.append(CONFIG.INFLUXDB_DATA_PATH)
    env_path = os.environ.get("INFLUXD_ENGINE_PATH") or ""
    if env_path:
        candidates.append(env_path)
    candidates.extend(["/var/lib/influxdb", "/var/lib/influxdb2"])
    return candidates


def collect_influxdb_disk_bytes() -> Optional[int]:
    """InfluxDB data dir size via `du -sb` (with sudo fallback); cached per probe interval."""
    now = time.monotonic()
    if (now - _INFLUXDB_BYTES_CACHE["at"]) < CONFIG.INFLUXDB_DISK_PROBE_INTERVAL_S:
        return _INFLUXDB_BYTES_CACHE["value"]
    _INFLUXDB_BYTES_CACHE["at"] = now
    for path in _influxdb_data_dirs():
        if not os.path.isdir(path):
            continue
        for cmd in (["du", "-sb", path], ["sudo", "-n", "du", "-sb", path]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout:
                    val = int(r.stdout.split()[0])
                    _INFLUXDB_BYTES_CACHE["value"] = val
                    return val
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                continue
    _INFLUXDB_BYTES_CACHE["value"] = None
    return None


def _build_metric_sample() -> dict[str, Any]:
    sample: dict[str, Any] = {
        "ts": _now_iso(),
        "host": CONFIG.AGENT_HOSTNAME,
        "system": collect_system_metrics(),
    }
    if CONFIG.MONITOR_INFLUXDB_DISK_ENABLED:
        try:
            bytes_on_disk = collect_influxdb_disk_bytes()
            if bytes_on_disk is not None:
                sample["influxdb"] = {"bytes_on_disk": bytes_on_disk}
        except Exception as e:
            logger.debug("collect_influxdb_disk_bytes failed: %s", e)
    try:
        procs = collect_process_watchlist()
        if procs:
            sample["processes"] = procs
    except Exception as e:
        logger.debug("collect_process_watchlist failed: %s", e)
    try:
        pm = collect_powermetrics()
        if pm:
            sample["mac_power"] = pm
    except Exception as e:
        logger.debug("collect_powermetrics failed: %s", e)
    if CONFIG.LMS_ENABLED:
        sample["lms"] = {
            "server": lms_get_status(),
            "models": lms_get_models(),
            "ps": lms_get_ps(),
        }
    if CONFIG.LLAMA_ENABLED:
        rich = collect_llama_for_metrics()
        if not rich:
            rich = {"state": llama_get_state()}
        sample["llama"] = rich
    if CONFIG.MONITOR_MANAGER_ENABLED or CONFIG.MONITOR_ALARM_ENGINE_ENABLED:
        mp = _build_meta_perf_block()
        if mp:
            sample["manager_self_monitor"] = mp
    # Scrub non-finite floats (inf/nan → None) before the sample is enqueued or pushed.
    return bmc._sanitize_non_finite(sample)


def _push_dashboard_payload(sample: dict[str, Any]) -> None:
    """POST LMS-shaped dashboard payload to the manager when LMS is enabled."""
    if not CONFIG.LMS_ENABLED:
        return
    lms = sample.get("lms") or {}
    payload = {
        "ts": sample["ts"],
        "server": lms.get("server", {}),
        "models": lms.get("models", []),
        "ps": lms.get("ps", []),
        "active": next(
            (r for r in (lms.get("ps") or []) if r.get("status") not in ("IDLE", "STOPPED", "")),
            None,
        ),
        "system": sample.get("system"),
        "hardware": {
            "name": CONFIG.AGENT_DESCRIPTION or CONFIG.AGENT_HOSTNAME,
            "agent_user": CONFIG.AGENT_USER,
        },
    }
    try:
        tok = _token_provider()
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = _post_session.post(
            f"{CONFIG.MANAGER_URL.rstrip('/')}/api/remote/lmstudio",
            json=payload, headers=headers, timeout=3,
        )
        if r.ok:
            with _runtime_lock:
                prev_err = _state.get("last_manager_error")
                _state["last_manager_error"] = None
            if prev_err:
                logger.info("manager reachable again (was: %s)", prev_err)
        else:
            with _runtime_lock:
                prev_err = _state.get("last_manager_error")
                _state["last_manager_error"] = f"HTTP {r.status_code}"
            if prev_err != f"HTTP {r.status_code}":
                logger.warning(
                    "manager dashboard POST failed: HTTP %s — alarm-engine metrics will buffer",
                    r.status_code,
                )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with _runtime_lock:
            prev_err = _state.get("last_manager_error")
            _state["last_manager_error"] = msg
        if prev_err != msg:
            logger.warning(
                "manager unreachable: %s — alarm-engine metrics will buffer until it returns",
                msg,
            )


# Sticky preference for the host-metrics push endpoint. "auto" → try
# /api/remote/provider-state first; flips to "legacy" on first 404 so we
# don't keep round-tripping the unsupported envelope against an old manager.
_push_endpoint_state = "auto"


def _push_host_payload(sample: dict[str, Any]) -> None:
    """POST host metric snapshot to manager for any llama-capable agent.
    Prefers the /api/remote/provider-state envelope; on 404 falls back to
    legacy /api/remote/host-metrics and remembers the result. PR2: the
    primary-llama gate is dropped — STORE partitions samples per-agent."""
    if not getattr(CONFIG, "PUSH_HOST_METRICS_ENABLED", True):
        return
    if not CONFIG.LLAMA_ENABLED:
        return
    sys_metric = sample.get("system") or {}
    if not sys_metric:
        return
    if sample.get("llama"):
        sys_metric = dict(sys_metric)
        sys_metric["llama"] = sample.get("llama")
    base = CONFIG.MANAGER_URL.rstrip("/")
    try:
        tok = _token_provider()
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        global _push_endpoint_state
        if _push_endpoint_state in ("auto", "envelope"):
            r = _post_session.post(
                f"{base}/api/remote/provider-state",
                json={"provider": "llama", "sample": sys_metric},
                headers=headers, timeout=3,
            )
            if r.status_code == 404:
                _push_endpoint_state = "legacy"
                logger.info("manager has no /api/remote/provider-state; "
                            "falling back to /api/remote/host-metrics")
            else:
                _push_endpoint_state = "envelope"
                if not r.ok:
                    logger.debug("provider-state push HTTP %s", r.status_code)
                return
        r = _post_session.post(
            f"{base}/api/remote/host-metrics",
            json=sys_metric, headers=headers, timeout=3,
        )
        if not r.ok:
            logger.debug("host-metrics push HTTP %s", r.status_code)
    except Exception as e:
        logger.debug("host-metrics push failed: %s", e)


_log_watch_state: dict[str, dict[str, Any]] = {}
_log_watch_compiled: dict[str, "re.Pattern[str]"] = {}
_log_watch_dirty = False


def _log_watch_state_path() -> Path:
    return Path(CONFIG.AGENT_INSTALL_DIR) / "data" / "log-watch-state.json"


def _log_watch_load_state() -> None:
    global _log_watch_state
    p = _log_watch_state_path()
    try:
        if p.is_file():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                _log_watch_state = {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        logger.warning("log-watch: failed to load state %s: %s", p, e)


def _log_watch_save_state() -> None:
    global _log_watch_dirty
    if not _log_watch_dirty:
        return
    p = _log_watch_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(_log_watch_state))
        _log_watch_dirty = False
    except Exception as e:
        logger.debug("log-watch: failed to persist state: %s", e)


def _log_watch_compile(name: str, pattern: str) -> Optional["re.Pattern[str]"]:
    cached = _log_watch_compiled.get(name)
    if cached is not None and cached.pattern == pattern:
        return cached
    try:
        cached = re.compile(pattern)
    except re.error as e:
        logger.warning("log-watch[%s]: invalid regex %r: %s", name, pattern, e)
        return None
    _log_watch_compiled[name] = cached
    return cached


def _log_watch_post(rule: dict, line: str) -> None:
    if not CONFIG.ALARM_ENGINE_URL:
        logger.debug("log-watch: ALARM_ENGINE_URL not set, dropping alert")
        return
    name = str(rule.get("name") or "log-watch")
    severity = str(rule.get("severity") or "warning").lower()
    path = str(rule.get("path") or "")
    template = rule.get("message")
    if template:
        try:
            message = str(template).format(line=line.rstrip(), path=path, name=name)
        except Exception:
            message = f"{name}: {line.rstrip()}"
    else:
        message = f"{name} matched in {path}: {line.rstrip()}"
    body = {
        "name": name,
        "message": message[:2000],
        "severity": severity,
        "source": str(rule.get("source") or "log-watch"),
        "metric": str(rule.get("metric") or (Path(path).name if path else name)),
        "host": CONFIG.AGENT_HOSTNAME,
    }
    url = f"{CONFIG.ALARM_ENGINE_URL.rstrip('/')}/api/alarm/ingest"
    tok = _ingest_token_provider()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = _post_session.post(url, json=body, headers=headers, timeout=5)
        if not r.ok:
            logger.warning("log-watch[%s]: ingest HTTP %s: %s", name, r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("log-watch[%s]: ingest POST failed: %s", name, e)


def _log_watch_scan_rule(rule: dict) -> None:
    global _log_watch_dirty
    name = str(rule.get("name") or "").strip()
    path_str = str(rule.get("path") or "").strip()
    pattern = str(rule.get("pattern") or "").strip()
    if not (name and path_str and pattern):
        return
    cre = _log_watch_compile(name, pattern)
    if cre is None:
        return
    try:
        st = os.stat(path_str)
    except FileNotFoundError:
        return
    except Exception as e:
        logger.debug("log-watch[%s]: stat %s failed: %s", name, path_str, e)
        return

    cur = _log_watch_state.setdefault(name, {})
    inode = getattr(st, "st_ino", 0)
    size = st.st_size
    prev_inode = cur.get("inode")
    prev_offset = int(cur.get("offset") or 0)

    # First-ever observation of this rule: skip to EOF so historical lines
    # don't fire a flood on first enable.
    if prev_inode is None:
        cur.update({"inode": inode, "offset": size, "path": path_str})
        _log_watch_dirty = True
        return

    # File rotated/truncated: start over from 0.
    if inode != prev_inode or size < prev_offset:
        prev_offset = 0

    if size <= prev_offset:
        return

    # Per-rule cooldown gate — once the rule has fired within the cooldown
    # window, skip the read entirely. `max_per_tick` bounds a burst within
    # a single tick (when cooldown has elapsed).
    cooldown = float(rule.get("cooldown_s") or 60.0)
    now = time.time()
    last_fire = float(cur.get("last_fire_ts") or 0.0)
    in_cooldown = (now - last_fire) < cooldown

    max_bytes = max(4096, int(CONFIG.LOG_WATCH_MAX_BYTES_PER_TICK))
    start = prev_offset
    if size - start > max_bytes:
        start = size - max_bytes  # tail-only on huge backlogs

    try:
        with open(path_str, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
    except Exception as e:
        logger.debug("log-watch[%s]: read failed: %s", name, e)
        return

    text = chunk.decode("utf-8", errors="replace")
    # Don't consume a trailing partial line — leave it for the next tick so
    # a writer mid-append doesn't get its line truncated and skipped.
    lines = text.splitlines(keepends=True)
    consumed = len(chunk)
    if lines and not lines[-1].endswith(("\n", "\r")):
        consumed -= len(lines[-1].encode("utf-8", errors="replace"))
        lines.pop()
    new_offset = start + consumed

    if not in_cooldown:
        max_per_tick = int(rule.get("max_per_tick") or 5)
        fired = 0
        for raw in lines:
            line = raw.rstrip("\r\n")
            if not line or not cre.search(line):
                continue
            _log_watch_post(rule, line)
            last_fire = now
            fired += 1
            if fired >= max_per_tick:
                break

    cur.update({"inode": inode, "offset": new_offset, "path": path_str, "last_fire_ts": last_fire})
    _log_watch_dirty = True


def log_watch_loop() -> None:
    _log_watch_load_state()
    logger.info(
        "log-watch enabled: rules=%d interval=%ds",
        len(CONFIG.LOG_WATCH_RULES or []), CONFIG.LOG_WATCH_INTERVAL_S,
    )
    while True:
        try:
            if not _is_approved():
                time.sleep(CONFIG.LOG_WATCH_INTERVAL_S)
                continue
            for rule in (CONFIG.LOG_WATCH_RULES or []):
                if not isinstance(rule, dict):
                    continue
                if rule.get("enabled") is False:
                    continue
                try:
                    _log_watch_scan_rule(rule)
                except Exception as e:
                    logger.warning("log-watch rule %r failed: %s", rule.get("name"), e)
            _log_watch_save_state()
        except Exception as e:
            logger.warning("log_watch_loop tick failed: %s", e, exc_info=True)
        time.sleep(max(1, int(CONFIG.LOG_WATCH_INTERVAL_S)))


def collector_loop() -> None:
    while True:
        try:
            if not CONFIG.COLLECTION_ENABLED:
                time.sleep(CONFIG.POLL_INTERVAL_S)
                continue
            if not _is_approved():
                time.sleep(CONFIG.POLL_INTERVAL_S)
                continue
            sample = _build_metric_sample()
            with _runtime_lock:
                _state["last_metric_sample"] = sample
            if _metric_client is not None:
                try:
                    _metric_client.enqueue(sample)
                    with _runtime_lock:
                        _state["samples_posted"] += 1
                except Exception:
                    logger.exception("enqueue to alarm engine failed")
            _push_dashboard_payload(sample)
            _push_host_payload(sample)

            global _log_hb_last
            now = time.time()
            if now - _log_hb_last >= 60:
                _log_hb_last = now
                bm = _state.get("last_metric_sample") or {}
                sysm = bm.get("system") or {}
                tail = []
                if CONFIG.LLAMA_ENABLED:
                    ls = _state.get("llama_state")
                    if ls not in ("awake", "sleeping"):
                        ls = llama_get_state()
                    tail.append(f"llama={ls}")
                if CONFIG.LMS_ENABLED:
                    lms = (bm.get("lms") or {}).get("server") or {}
                    tail.append(f"lms_server={'on' if lms.get('on') else 'off'}")
                if CONFIG.OPENCLAW_ENABLED:
                    tail.append("openclaw=on")
                if _metric_client is not None:
                    with best_effort("heartbeat: append buffer breakdown"):
                        mem, disk = _metric_client.buffer_breakdown()
                        if mem or disk:
                            tail.append(f"buffer=mem:{mem}/disk:{disk}")
                last_err = _state.get("last_manager_error")
                if last_err:
                    tail.append(f"manager_err={last_err}")
                label = CONFIG.AGENT_DESCRIPTION or CONFIG.AGENT_HOSTNAME
                logger.info(
                    "heartbeat agent[%s]: collection=%s approved=%s cpu=%.1f%% ram=%.1f%%%s",
                    label,
                    CONFIG.COLLECTION_ENABLED,
                    _is_approved(),
                    sysm.get("cpu_total", 0.0),
                    (sysm.get("ram") or {}).get("percent", 0.0),
                    (" " + " ".join(tail)) if tail else "",
                )
        except Exception as e:
            logger.warning("collector_loop tick failed: %s", e, exc_info=True)
        time.sleep(CONFIG.POLL_INTERVAL_S)


@asynccontextmanager
async def _lifespan(_app: "FastAPI") -> AsyncIterator[None]:
    """Spawns the perf-controller task at startup when enabled (delegated to providers.llama)."""
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = max(
            1, int(getattr(CONFIG, "WORKER_THREADS", 64) or 64))
        stream_pool.configure(getattr(CONFIG, "WORKER_THREADS", 64),
                              getattr(CONFIG, "STREAM_RESERVE_THREADS", 24))
        logger.info("worker pool: %d threads, SSE stream cap=%d",
                    int(getattr(CONFIG, "WORKER_THREADS", 64) or 64),
                    stream_pool.POOL.limit())
    except Exception as e:
        logger.warning("worker-pool tuning skipped: %s", e)
    perf_task: Optional[asyncio.Task] = providers.llama.start_background()
    try:
        yield
    finally:
        if perf_task is not None:
            perf_task.cancel()
            try:
                await perf_task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("perf_task shutdown cleanup raised: %r", e)


app = FastAPI(title="LLM Systems Agent", version=VERSION, lifespan=_lifespan)

# CORS so the browser can hit the agent directly for SSE streams.
from fastapi.middleware.cors import CORSMiddleware as _CORSMiddleware
_cors_origins_env = os.environ.get("LSA_AGENT_CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()] or ["*"]
app.add_middleware(
    _CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def _check_bearer(authorization: Optional[str]) -> None:
    if _state.get("auth_disabled_global"):
        return
    expected = _token_provider()
    if not expected:
        raise HTTPException(status_code=401, detail="agent not initialized")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization[len("Bearer "):].strip()
    if presented != expected:
        raise HTTPException(status_code=403, detail="invalid token")


def _verify_stream_token(token: str, expected_path: str) -> bool:
    """Verify "<expiry>.<sig>" where sig=HMAC-SHA256(secret, "<agent_id>|<path>|<expiry>")."""
    import hashlib
    import hmac as _hmac_lib
    if not token or "." not in token:
        return False
    secret = _state.get("manager_secret")
    if not secret:
        return False
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, TypeError):
        return False
    if expiry < time.time():
        return False
    aid = _state.get("agent_id") or ""
    if not aid:
        return False
    msg = f"{aid}|{expected_path}|{expiry}".encode()
    expected_sig = _hmac_lib.new(secret, msg, hashlib.sha256).hexdigest()
    return _hmac_lib.compare_digest(expected_sig, sig)


def _check_stream_auth(
    authorization: Optional[str], token_q: Optional[str], path: str,
) -> None:
    """Either-or SSE auth: bearer header OR stream token in ?token= (EventSource can't set headers)."""
    if _state.get("auth_disabled_global"):
        return
    if token_q and _verify_stream_token(token_q, path):
        return
    if authorization:
        _check_bearer(authorization)
        return
    raise HTTPException(status_code=401, detail="missing bearer or stream token")


def _public_endpoint_map() -> dict[str, list[str]]:
    return {
        "agent": ["/health", "/identify", "/status", "/config", "/config/reload",
                  "/agent/restart", "/agent/collection", "/agent/self-update",
                  "/metrics"],
        "lms": [
            "/lms/server/status", "/lms/server/start", "/lms/server/stop",
            "/lms/server/restart", "/lms/server/log",
            "/lms/models", "/lms/ps",
            "/lms/load", "/lms/unload", "/lms/download",
        ],
        "llama": [
            "/llama/state",
            "/llama/server/status", "/llama/server/start", "/llama/server/stop",
            "/llama/server/restart", "/llama/server/wake", "/llama/server/svcconfig",
            "/llama/log/tail", "/llama/log/stream",
            "/llama/models", "/llama/load", "/llama/unload",
            "/llama/config", "/llama/config/{model_id}",
            "/llama/download", "/llama/download/stream", "/llama/download/cancel",
            "/llama/build", "/llama/build/stream",
            "/llama/cache", "/llama/cache/prune", "/llama/cache/rm", "/llama/hf-trending",
            "/llama/bench/run", "/llama/bench/stream", "/llama/bench/cancel",
            "/llama/bench/perf-mode",
        ],
        "terminal": [
            "/terminal/create", "/terminal/output/{sid}",
            "/terminal/input/{sid}", "/terminal/resize/{sid}", "/terminal/close/{sid}",
        ],
        "p2_stubbed": [],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "os": CONFIG.AGENT_OS,
        "hostname": CONFIG.AGENT_HOSTNAME,
        "role": CONFIG.AGENT_ROLE,
        "version": VERSION,
        "approved": _is_approved(),
        "agent_user": CONFIG.AGENT_USER,
        "install_dir": CONFIG.AGENT_INSTALL_DIR,
        "collection_enabled": CONFIG.COLLECTION_ENABLED,
    }


@app.get("/identify")
async def identify() -> dict[str, Any]:
    base = await health()
    base["capabilities"] = _capabilities()
    base["endpoints"] = _public_endpoint_map()
    return base


@app.get("/status")
async def status_(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    with _runtime_lock:
        snap = dict(_state)
    snap.pop("manager_secret", None)
    if "token" in snap and snap["token"]:
        snap["token"] = "<redacted>"
    if snap.get("ingest_token"):
        snap["ingest_token"] = "<redacted>"
    # ack nests manager_secret + ingest_token + (rotation) TLS key — never echo.
    if snap.get("last_heartbeat_ack") is not None:
        snap["last_heartbeat_ack"] = "<received>"
    snap["has_manager_secret"] = bool(_state.get("manager_secret"))
    snap["config_loaded_from"] = getattr(CONFIG, "_loaded_from", None)
    snap["buffer"] = (
        {
            "memory": _metric_client.buffer_breakdown()[0] if _metric_client else 0,
            "disk": _metric_client.buffer_breakdown()[1] if _metric_client else 0,
            "stats": _metric_client.stats if _metric_client else {},
        }
    )
    try:
        import anyio
        lim = anyio.to_thread.current_default_thread_limiter()
        _st = stream_pool.POOL.stats()
        snap["streams"] = {
            "active": _st["active"],
            "cap": _st["limit"],
            "peak": _st["peak"],
            "refusals": _st["refusals"],
            "worker_threads": int(lim.total_tokens),
            "worker_threads_busy": int(lim.borrowed_tokens),
            "terminal_sessions": len(getattr(providers.terminal, "_term_sessions", {})),
        }
    except Exception as e:
        logger.warning("stream-health snapshot failed: %s", e)
        snap["streams"] = {"error": "unavailable"}
    return snap


@app.get("/config")
async def get_config(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    return CONFIG.to_redacted_dict()


@app.post("/config/reload")
async def reload_config(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    global CONFIG
    CONFIG = AgentConfig.load()
    collectors.configure_all(CONFIG)
    providers.configure_all(AgentContext(
        config=CONFIG,
        check_bearer=_check_bearer,
        check_stream_auth=_check_stream_auth,
        post_session=_post_session,
        runtime_lock=_runtime_lock,
        state=_state,
        now_iso=_now_iso,
        probe_http=_probe_http,
    ))
    logger.info("config reloaded from %s", getattr(CONFIG, "_loaded_from", "<defaults>"))
    return {"ok": True, "loaded_from": getattr(CONFIG, "_loaded_from", None)}


@app.get("/agent/config-file")
async def agent_config_file_get(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Return loaded agent_config.yaml text + path + mtime."""
    _check_bearer(authorization)
    path = getattr(CONFIG, "_loaded_from", None)
    if not path:
        return {
            "ok": False,
            "error": "no config file is currently loaded (agent is running on defaults + env)",
            "path": None,
        }
    try:
        st = os.stat(path)
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")
    return {
        "ok": True,
        "path": path,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "text": text,
    }


@app.put("/agent/config-file")
async def agent_config_file_put(
    body: dict, authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Validate + backup + atomically replace agent_config.yaml. Requires /agent/restart to apply."""
    _check_bearer(authorization)
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="missing 'text' (string) in body")
    expected_mtime = body.get("expected_mtime")

    path = getattr(CONFIG, "_loaded_from", None)
    if not path:
        raise HTTPException(
            status_code=409,
            detail="no config file is currently loaded — refusing to create one blindly",
        )

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"YAML parse error: {e}")
    if parsed is not None and not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="top-level YAML must be a mapping")
    parsed = parsed or {}
    if not parsed.get("MANAGER_URL"):
        raise HTTPException(
            status_code=400,
            detail="MANAGER_URL is required — refusing to write a config that would orphan the agent",
        )

    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"config file disappeared: {path}")
    if expected_mtime is not None:
        try:
            if abs(float(expected_mtime) - st.st_mtime) > 0.001:
                raise HTTPException(
                    status_code=409,
                    detail=f"file changed on disk since GET (expected mtime {expected_mtime}, "
                           f"on-disk {st.st_mtime}) — reload and retry",
                )
        except (TypeError, ValueError):
            pass

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.{ts}.bak"
    try:
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, 0o600)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"backup failed: {e}")

    tmp_path = f"{path}.{ts}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_path, 0o600)
        try:
            shutil.chown(tmp_path, user=st.st_uid, group=st.st_gid)
        except (LookupError, PermissionError, OSError):
            pass
        os.replace(tmp_path, path)
    except Exception as e:
        with best_effort("config write: unlink temp file"):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=f"write failed: {e}")

    new_st = os.stat(path)
    logger.warning(
        "agent_config.yaml rewritten via /agent/config-file (backup=%s, size=%d → %d). "
        "Changes take effect on next restart.",
        backup_path, st.st_size, new_st.st_size,
    )
    return {
        "ok": True,
        "path": path,
        "backup_path": backup_path,
        "mtime": new_st.st_mtime,
        "size": new_st.st_size,
        "note": "Restart the agent for changes to take effect.",
    }


@app.post("/agent/restart")
async def agent_restart(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    with _runtime_lock:
        _state["restart_pending"] = True
    eta = _now_iso()
    logger.warning("self-restart requested via /agent/restart")

    def _do_exit():
        time.sleep(1.0)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_do_exit, daemon=True).start()
    return {"ok": True, "restart_eta": eta}


@app.get("/agent/log/tail")
def agent_log_tail(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Last ~50KB of the agent log, parsed into lines."""
    _check_bearer(authorization)
    path = CONFIG.LOG_FILE
    TAIL_BYTES = 50 * 1024
    try:
        size = os.path.getsize(path)
        offset = max(0, size - TAIL_BYTES)
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
                f.readline()
            data = f.read()
        lines = [l.decode("utf-8", errors="replace").rstrip()
                 for l in data.splitlines()]
        return {"ok": True, "path": path, "lines": lines}
    except FileNotFoundError:
        return {"ok": True, "path": path, "lines": [],
                "note": "log file does not exist yet"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/log/stream")
def agent_log_stream(authorization: Optional[str] = Header(default=None)) -> StreamingResponse:
    """SSE tail -f of the agent log; bearer-only (proxied through the manager)."""
    _check_bearer(authorization)
    path = CONFIG.LOG_FILE

    def generate() -> Iterator[bytes]:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                buf = b""
                last_keepalive = time.time()
                while True:
                    chunk = f.read(65536)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            line = raw.decode("utf-8", errors="replace").rstrip()
                            yield f"data: {json.dumps({'line': line})}\n\n".encode()
                        last_keepalive = time.time()
                        continue
                    time.sleep(0.5)
                    if time.time() - last_keepalive > 15:
                        yield b'data: {"keepalive": true}\n\n'
                        last_keepalive = time.time()
        except FileNotFoundError:
            yield f"data: {json.dumps({'error': f'log file not found at {path}'})}\n\n".encode()
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n".encode()

    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/self-update")
def agent_self_update(authorization: Optional[str] = Header(default=None)) -> StreamingResponse:
    """Fetch agent tarball, run install.sh --update, stream SSE, SIGTERM for restart.

    SSE frames: {"stage": "fetch|deploy|restart|done", ...} | {"line": "..."}
    """
    _check_bearer(authorization)

    if not CONFIG.LLAMA_ENABLED and not CONFIG.LMS_ENABLED and CONFIG.AGENT_ROLE == "system_only":
        logger.info("self-update requested on system-only agent")

    repo_dir = CONFIG.AGENT_REPO_DIR
    tok = _load_token()
    if not tok:
        raise HTTPException(
            status_code=503,
            detail=f"agent token not found at {CONFIG.TOKEN_FILE}; can't self-update",
        )
    if not CONFIG.MANAGER_URL:
        raise HTTPException(
            status_code=503,
            detail="MANAGER_URL not set; can't fetch tarball",
        )

    version_before = VERSION

    def _sse_event(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode()

    def _gen() -> Iterator[bytes]:
        import tarfile, shutil

        # Wipe in case a prior --update aborted and left stale contents.
        try:
            os.makedirs(repo_dir, exist_ok=True)
            for entry in os.listdir(repo_dir):
                p = os.path.join(repo_dir, entry)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try: os.remove(p)
                    except OSError: pass
        except Exception as e:
            logger.warning("self-update: failed to clear repo dir: %s", e)
            yield _sse_event({"stage": "done", "ok": False,
                              "msg": "failed to clear staging directory"})
            return

        tarball_url = CONFIG.MANAGER_URL.rstrip("/") + "/api/agent-tarball"
        tarball_path = os.path.join(repo_dir, ".agent-update.tar.gz")
        yield _sse_event({"stage": "fetch", "msg": f"GET {tarball_url}"})
        try:
            # _post_session carries the CA bundle for https MANAGER_URL.
            r = _post_session.get(
                tarball_url, headers={"Authorization": f"Bearer {tok}"},
                stream=True, timeout=60,
            )
            r.raise_for_status()
            with open(tarball_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.warning("self-update: tarball download failed: %s", e)
            yield _sse_event({"stage": "done", "ok": False,
                              "msg": "tarball download failed"})
            return

        try:
            with tarfile.open(tarball_path, "r:gz") as tf:
                extra = {"filter": "data"} if sys.version_info >= (3, 12) else {}
                tf.extractall(repo_dir, **extra)
        except Exception as e:
            logger.warning("self-update: tar extract failed: %s", e)
            yield _sse_event({"stage": "done", "ok": False,
                              "msg": "tar extract failed"})
            return
        try: os.remove(tarball_path)
        except OSError: pass

        install_sh = os.path.join(repo_dir, "agent", "install", "install.sh")
        if not os.path.isfile(install_sh):
            yield _sse_event({"stage": "done", "ok": False,
                              "msg": f"install.sh missing after extract at {install_sh}"})
            return

        version_to = "unknown"
        with best_effort("self-update: read staged agent VERSION"):
            staged_agent_py = os.path.join(repo_dir, "agent", "llm-systems-agent.py")
            with open(staged_agent_py, "r") as f:
                for raw in f:
                    m = re.match(r'^VERSION\s*=\s*["\'](.+?)["\']', raw)
                    if m:
                        version_to = m.group(1)
                        break

        yield _sse_event({"stage": "deploy",
                          "msg": "running install.sh --update --from-self-update --no-pull"})
        yield _sse_event({"line": f"version_before: {version_before}"})
        yield _sse_event({"line": f"version_to:     {version_to}"})
        yield _sse_event({"line": f"repo_dir:       {repo_dir}"})

        # --install-dir explicit because install.sh's default is Linux-only.
        cmd = ["bash", install_sh,
               "--update", "--from-self-update", "--no-pull",
               "--install-dir", CONFIG.AGENT_INSTALL_DIR]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=repo_dir,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        # Pump output through a queue so the generator can emit a keepalive
        # during long-silent pip phases — stops the manager false-reaping (#109).
        import queue as _queue
        out_q: "_queue.Queue[tuple[str, str]]" = _queue.Queue()

        def _pump() -> None:
            try:
                for raw in iter(proc.stdout.readline, ""):
                    out_q.put(("line", raw.rstrip()))
            finally:
                out_q.put(("eof", ""))

        threading.Thread(target=_pump, daemon=True).start()
        while True:
            try:
                kind, val = out_q.get(timeout=10)
            except _queue.Empty:
                yield _sse_event({"keepalive": True})
                continue
            if kind == "eof":
                break
            yield _sse_event({"line": val} if val else {"blank": True})
        proc.wait()

        if proc.returncode != 0:
            yield _sse_event({
                "stage": "done",
                "ok": False,
                "rc": proc.returncode,
                "version_before": version_before,
                "version_to": version_to,
                "msg": "install.sh --update failed; agent NOT restarted",
            })
            return

        # Schedule self-restart in 1.5s so the SSE response can flush.
        with _runtime_lock:
            _state["restart_pending"] = True

        def _do_exit() -> None:
            time.sleep(1.5)
            logger.warning("self-update complete; SIGTERM-ing for restart")
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_do_exit, daemon=True).start()

        yield _sse_event({
            "stage": "restart",
            "msg": "scheduling SIGTERM in ~1.5s (systemd Restart=always brings the new code up)",
        })
        yield _sse_event({
            "stage": "done",
            "ok": True,
            "rc": 0,
            "version_before": version_before,
            "version_to": version_to,
            "restart_eta_s": 1.5,
            "msg": f"self-update complete; restart pending ({version_before} → {version_to})",
        })

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/collection")
async def agent_collection(
    body: dict, authorization: Optional[str] = Header(default=None)
) -> dict[str, Any]:
    _check_bearer(authorization)
    enabled = bool(body.get("enabled", True))
    CONFIG.COLLECTION_ENABLED = enabled
    logger.info("COLLECTION_ENABLED=%s (set via /agent/collection)", enabled)
    return {"ok": True, "collection_enabled": enabled}


@app.get("/metrics")
async def metrics(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _check_bearer(authorization)
    return collect_system_metrics()


# LMS namespace (10 routes + helpers) moved to agent/providers/lms.py
# (Tier 3 A2); registered via providers.lms.register_routes(app) in main().
# llama.cpp namespace (32 routes) moved to agent/providers/llama.py
# (Tier 3 A3); registered via providers.llama.register_routes(app) in main().



# terminal namespace (5 routes + PTY plumbing) moved to agent/providers/terminal.py
# (Tier 3 A4); registered via providers.terminal.register_routes(app) in main().


import sqlite3 as _oc_sqlite3


def _oc_check_enabled() -> None:
    if not CONFIG.OPENCLAW_ENABLED:
        raise HTTPException(status_code=503, detail="openclaw not enabled on this agent")


def _oc_data_paths() -> dict[str, Path]:
    """Resolve {agents, flows, tasks, delivery} relative to OPENCLAW_AGENTS_DIR.parent."""
    agents_dir = Path(CONFIG.OPENCLAW_AGENTS_DIR)
    parent = agents_dir.parent
    return {
        "agents":   agents_dir,
        "flows":    parent / "flows" / "registry.sqlite",
        "tasks":    parent / "tasks" / "runs.sqlite",
        "delivery": parent / "delivery-queue" / "failed",
    }


def _oc_is_session_file(name: str) -> bool:
    if not name.endswith(".jsonl"):
        return False
    bad = (".deleted.", ".reset.", ".checkpoint.", ".trajectory")
    return not any(b in name for b in bad)


def _oc_normalize_plugin_name(tool_name) -> str:
    name = str(tool_name or "").strip().lower()
    if not name:
        return ""
    for sep in ("/", ":", "."):
        if sep in name:
            name = name.split(sep, 1)[0]
            break
    return name[:64]


def _oc_extract_tool_plugins(obj) -> list[str]:
    plugins: list[str] = []
    message = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
    for part in message.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("toolCall", "tool_use"):
            p = _oc_normalize_plugin_name(part.get("name", ""))
            if p:
                plugins.append(p)
    for tc in obj.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        p = _oc_normalize_plugin_name(
            tc.get("name") or (tc.get("function") or {}).get("name", "")
        )
        if p:
            plugins.append(p)
    for tc in obj.get("tool_use") or []:
        if not isinstance(tc, dict):
            continue
        p = _oc_normalize_plugin_name(tc.get("name", ""))
        if p:
            plugins.append(p)
    return plugins


def _oc_ts_to_day(ts_str) -> Optional[str]:
    if not ts_str:
        return None
    try:
        return ts_str[:10]
    except Exception:
        return None


def _oc_parse_session_file(path: Path) -> dict[str, Any]:
    """One-pass aggregation over a session .jsonl; output schema matches manager's _parse_session_file."""
    agg: dict[str, Any] = {
        "session_id": path.stem,
        "messages": 0, "user_msgs": 0, "assistant_msgs": 0,
        "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
        "cost": 0.0, "tool_uses": 0,
        "models": set(),
        "first_ts": None, "last_ts": None, "cwd": None,
        "tools": {}, "tool_breakdown": {},
        "thinking_events": 0, "thinking_chars": 0,
        "daily": {}, "models_cost": {},
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t == "session":
                    agg["cwd"] = d.get("cwd")
                    continue
                if t != "message":
                    continue
                agg["messages"] += 1
                ts = d.get("timestamp")
                if ts:
                    if not agg["first_ts"]:
                        agg["first_ts"] = ts
                    agg["last_ts"] = ts
                day = _oc_ts_to_day(ts)
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "user":
                    agg["user_msgs"] += 1
                elif role == "assistant":
                    agg["assistant_msgs"] += 1
                model_name = msg.get("model")
                if model_name and model_name != "gateway-injected":
                    agg["models"].add(model_name)
                u = msg.get("usage")
                msg_cost = 0.0
                msg_in = msg_out = 0
                if isinstance(u, dict):
                    msg_in  = u.get("input", 0) or 0
                    msg_out = u.get("output", 0) or 0
                    agg["input"]      += msg_in
                    agg["output"]     += msg_out
                    agg["cacheRead"]  += u.get("cacheRead", 0) or 0
                    agg["cacheWrite"] += u.get("cacheWrite", 0) or 0
                    cost = u.get("cost")
                    if isinstance(cost, dict):
                        msg_cost = float(cost.get("total", 0) or 0)
                        agg["cost"] += msg_cost
                if day:
                    db = agg["daily"].setdefault(day, {"input": 0, "output": 0, "cost": 0.0, "tokens": 0})
                    db["input"]  += msg_in
                    db["output"] += msg_out
                    db["cost"]   += msg_cost
                    db["tokens"] += (msg_in + msg_out)
                if model_name and model_name != "gateway-injected" and msg_cost:
                    agg["models_cost"][model_name] = agg["models_cost"].get(model_name, 0.0) + msg_cost
                for p in _oc_extract_tool_plugins(d):
                    agg["tools"][p] = agg["tools"].get(p, 0) + 1
                    agg["tool_breakdown"][p] = agg["tool_breakdown"].get(p, 0) + 1
                    agg["tool_uses"] += 1
                content = msg.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "thinking":
                            agg["thinking_events"] += 1
                            agg["thinking_chars"] += len(str(item.get("thinking", "")))
    except Exception as e:
        logger.warning("openclaw: failed to parse %s: %s", path, e)
    agg["models"] = sorted(agg["models"])
    return agg


# Cache keyed by abs path; invalidated on (mtime, size) change.
_oc_file_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _oc_collect_sessions(agents_dir: Path) -> list[dict[str, Any]]:
    """Flat list of per-session aggregates with `agent_dir` field for grouping."""
    out: list[dict[str, Any]] = []
    if not agents_dir.exists():
        return out
    for ad in sorted(agents_dir.iterdir()):
        if not ad.is_dir():
            continue
        sessions = ad / "sessions"
        if not sessions.exists():
            continue
        for fn in sessions.iterdir():
            if not fn.is_file() or not _oc_is_session_file(fn.name):
                continue
            try:
                st = fn.stat()
            except OSError:
                continue
            key = str(fn)
            cached = _oc_file_cache.get(key)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                parsed = cached[2]
            else:
                parsed = _oc_parse_session_file(fn)
                _oc_file_cache[key] = (st.st_mtime, st.st_size, parsed)
            row = dict(parsed)
            row["agent_dir"] = ad.name
            out.append(row)
    return out


def _oc_collect_flows(flows_db: Path) -> dict[str, Any]:
    if not flows_db.exists():
        return {"total": 0, "by_status": {}, "recent": []}
    try:
        conn = _oc_sqlite3.connect(f"file:{flows_db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = _oc_sqlite3.Row
        by_status: dict[str, int] = {}
        for row in conn.execute("SELECT status, COUNT(*) c FROM flow_runs GROUP BY status"):
            by_status[row["status"]] = row["c"]
        total = sum(by_status.values())
        recent: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT flow_id, status, goal, created_at, ended_at, owner_key "
            "FROM flow_runs ORDER BY created_at DESC LIMIT 20"
        ):
            created_ms = row["created_at"] or 0
            ended_ms   = row["ended_at"]   or 0
            dur = round((ended_ms - created_ms) / 1000.0, 1) if (ended_ms and created_ms) else None
            recent.append({
                "id": row["flow_id"], "status": row["status"], "goal": row["goal"],
                "owner": row["owner_key"], "duration_s": dur,
                "created_iso": datetime.fromtimestamp(created_ms / 1000.0).isoformat() if created_ms else None,
            })
        conn.close()
        return {"total": total, "by_status": by_status, "recent": recent}
    except Exception as e:
        logger.warning("openclaw flows: %s", e)
        return {"total": 0, "by_status": {}, "recent": [], "error": "collection failed"}


def _oc_collect_tasks(tasks_db: Path) -> dict[str, Any]:
    if not tasks_db.exists():
        return {"total": 0, "by_status": {}, "by_runtime": {},
                "avg_duration_s": None, "recent_failures": []}
    try:
        conn = _oc_sqlite3.connect(f"file:{tasks_db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = _oc_sqlite3.Row
        by_status: dict[str, int] = {}
        for row in conn.execute("SELECT status, COUNT(*) c FROM task_runs GROUP BY status"):
            by_status[row["status"]] = row["c"]
        total = sum(by_status.values())
        by_runtime: dict[str, int] = {}
        for row in conn.execute("SELECT runtime, COUNT(*) c FROM task_runs GROUP BY runtime"):
            by_runtime[row["runtime"] or "?"] = row["c"]
        r = conn.execute(
            "SELECT AVG((ended_at - started_at)/1000.0) AS a FROM task_runs "
            "WHERE status='succeeded' AND ended_at IS NOT NULL AND started_at IS NOT NULL"
        ).fetchone()
        avg_dur = round(r["a"], 2) if r and r["a"] is not None else None
        fails: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT task_id, label, task_kind, runtime, error, created_at, status "
            "FROM task_runs WHERE status IN ('failed','timed_out','lost') "
            "ORDER BY created_at DESC LIMIT 10"
        ):
            created_ms = row["created_at"] or 0
            fails.append({
                "id": row["task_id"], "label": row["label"], "kind": row["task_kind"],
                "runtime": row["runtime"], "status": row["status"],
                "error": (row["error"] or "")[:500],
                "created_iso": datetime.fromtimestamp(created_ms / 1000.0).isoformat() if created_ms else None,
            })
        conn.close()
        return {"total": total, "by_status": by_status, "by_runtime": by_runtime,
                "avg_duration_s": avg_dur, "recent_failures": fails}
    except Exception as e:
        logger.warning("openclaw tasks: %s", e)
        return {"total": 0, "by_status": {}, "by_runtime": {},
                "avg_duration_s": None, "recent_failures": [], "error": "collection failed"}


def _oc_collect_delivery(delivery_dir: Path) -> dict[str, Any]:
    if not delivery_dir.exists():
        return {"total": 0, "by_channel": {}, "total_retries": 0,
                "common_errors": [], "oldest_enqueue_iso": None}
    try:
        by_channel: dict[str, int] = {}
        total_retries = 0
        err_counts: dict[str, int] = {}
        oldest_ms: Optional[float] = None
        count = 0
        for fp in delivery_dir.glob("*.json"):
            try:
                with fp.open("r", encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
            except Exception:
                continue
            count += 1
            ch = d.get("channel") or "?"
            by_channel[ch] = by_channel.get(ch, 0) + 1
            total_retries += int(d.get("retryCount") or 0)
            enq = d.get("enqueuedAt")
            if isinstance(enq, (int, float)):
                if oldest_ms is None or enq < oldest_ms:
                    oldest_ms = enq
            err = (d.get("lastError") or "").strip()
            if err:
                key = err.split(":")[0][:120]
                err_counts[key] = err_counts.get(key, 0) + 1
        common = sorted(err_counts.items(), key=lambda x: -x[1])[:3]
        return {
            "total": count, "by_channel": by_channel, "total_retries": total_retries,
            "common_errors": [{"error": k, "count": v} for k, v in common],
            "oldest_enqueue_iso": datetime.fromtimestamp(oldest_ms / 1000.0).isoformat() if oldest_ms else None,
        }
    except Exception as e:
        logger.warning("openclaw delivery: %s", e)
        return {"total": 0, "by_channel": {}, "total_retries": 0,
                "common_errors": [], "oldest_enqueue_iso": None, "error": "collection failed"}


@app.get("/openclaw/aggregate")
def openclaw_aggregate(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Host's OpenClaw snapshot (sessions + flows + tasks + delivery)."""
    _check_bearer(authorization)
    _oc_check_enabled()
    paths = _oc_data_paths()
    return {
        "host":     CONFIG.AGENT_HOSTNAME,
        "agents_dir": str(paths["agents"]),
        "sessions": _oc_collect_sessions(paths["agents"]),
        "flows":    _oc_collect_flows(paths["flows"]),
        "tasks":    _oc_collect_tasks(paths["tasks"]),
        "delivery": _oc_collect_delivery(paths["delivery"]),
        "ts":       int(time.time()),
    }


P2_STUB_PATHS: list[tuple[str, str]] = []


def _register_p2_stubs() -> None:
    async def _stub(_authorization: Optional[str] = Header(default=None)) -> JSONResponse:
        _check_bearer(_authorization)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "endpoint not yet implemented (Phase 2)"},
        )

    for method, path in P2_STUB_PATHS:
        app.add_api_route(path, _stub, methods=[method])


_register_p2_stubs()


def _init_metric_client() -> Optional[bmc.BufferedMetricClient]:
    if not CONFIG.ALARM_ENGINE_URL:
        return None
    cache_dir = Path(CONFIG.AGENT_INSTALL_DIR) / "data" / "metric-buffer"
    return bmc.BufferedMetricClient(
        endpoint_url=f"{CONFIG.ALARM_ENGINE_URL.rstrip('/')}{bmc.INGEST_PATH}",
        host=CONFIG.AGENT_HOSTNAME,
        cache_dir=cache_dir,
        flush_interval=CONFIG.METRIC_FLUSH_INTERVAL_S,
        max_memory_samples=CONFIG.METRIC_MAX_MEMORY_SAMPLES,
        batch_limit=CONFIG.METRIC_BATCH_LIMIT,
        http_timeout=CONFIG.METRIC_HTTP_TIMEOUT_S,
        auth_token_provider=_ingest_token_provider,
        session=_post_session,
    )


def main() -> None:
    global CONFIG, _metric_client
    CONFIG = AgentConfig.load()
    collectors.configure_all(CONFIG)
    providers.configure_all(AgentContext(
        config=CONFIG,
        check_bearer=_check_bearer,
        check_stream_auth=_check_stream_auth,
        post_session=_post_session,
        runtime_lock=_runtime_lock,
        state=_state,
        now_iso=_now_iso,
        probe_http=_probe_http,
    ))
    providers.register_all_routes(app)
    setup_logging(CONFIG.LOG_FILE, CONFIG.LOG_LEVEL)
    logger.info("=" * 60)
    logger.info("LLM Systems Agent %s starting", VERSION)
    logger.info("=" * 60)
    logger.info(
        "config: os=%s host=%s desc=%r role=%s user=%s install=%s manager=%s alarm=%s "
        "lms=%s llama=%s perf=%s loaded_from=%s",
        CONFIG.AGENT_OS, CONFIG.AGENT_HOSTNAME, CONFIG.AGENT_DESCRIPTION or "",
        CONFIG.AGENT_ROLE, CONFIG.AGENT_USER,
        CONFIG.AGENT_INSTALL_DIR, CONFIG.MANAGER_URL, CONFIG.ALARM_ENGINE_URL,
        CONFIG.LMS_ENABLED, CONFIG.LLAMA_ENABLED, CONFIG.PERF_CONTROLLER_ENABLED,
        getattr(CONFIG, "_loaded_from", None),
    )

    _configure_manager_tls_verify()
    _configure_ae_tls_verify()

    threading.Thread(target=registry_register_blocking, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    _metric_client = _init_metric_client()
    if _metric_client is not None:
        _metric_client.start()

    threading.Thread(target=collector_loop, daemon=True).start()

    if CONFIG.LOG_WATCH_ENABLED and CONFIG.LOG_WATCH_RULES:
        threading.Thread(
            target=log_watch_loop, name="log-watch", daemon=True,
        ).start()

    if CONFIG.MONITOR_MANAGER_ENABLED or CONFIG.MONITOR_ALARM_ENGINE_ENABLED:
        threading.Thread(
            target=_meta_perf_loop, name="meta-perf", daemon=True,
        ).start()
        logger.info(
            "manager_self_monitor probes ENABLED — manager=%s alarm_engine=%s interval=%ds",
            CONFIG.MONITOR_MANAGER_ENABLED, CONFIG.MONITOR_ALARM_ENGINE_ENABLED,
            CONFIG.META_PERF_INTERVAL_S,
        )

    # When TLS cert+key are present, serve HTTPS on AGENT_BIND_PORT (drops HTTP).
    # timeout_graceful_shutdown caps the post-SIGTERM wait. Uvicorn's default
    # of None waits forever for in-flight connections to close — the agent has
    # several long-lived SSE streams (/llama/log/stream, /llama/build/stream,
    # /llama/bench/stream, /llama/autotune/stream, /llama/download/stream,
    # /terminal/output/<sid>) that the manager keeps open indefinitely, so
    # without this cap every systemctl stop / upgrade / admin-tab restart sat
    # until systemd's DefaultTimeoutStopSec (90s on Debian/Ubuntu) elapsed
    # and SIGKILL fired. 10s lets short HTTP requests finish but cuts SSE
    # within the systemd patience window.
    uv_kwargs: dict[str, Any] = dict(
        app=app,
        host=CONFIG.AGENT_BIND_HOST,
        port=CONFIG.AGENT_BIND_PORT,
        log_config=None,
        timeout_graceful_shutdown=10,
    )
    if _tls_enabled():
        crt, key = _tls_paths()
        uv_kwargs["ssl_certfile"] = str(crt)
        uv_kwargs["ssl_keyfile"]  = str(key)
        globals()["_SERVED_WITH_TLS"] = True
        logger.info("TLS enabled: serving https://%s:%d (cert=%s)",
                    CONFIG.AGENT_BIND_HOST, CONFIG.AGENT_BIND_PORT, crt)
    else:
        logger.info("TLS not configured: serving http://%s:%d (drop cert + key into %s + %s to enable)",
                    CONFIG.AGENT_BIND_HOST, CONFIG.AGENT_BIND_PORT,
                    *_tls_paths())
    uvicorn.run(**uv_kwargs)


if __name__ == "__main__":
    main()
