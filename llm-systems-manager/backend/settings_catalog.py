"""Curated settings catalog driving the Admin → Settings tab (#606)."""
from __future__ import annotations

import math
import tomllib
from typing import Any, Optional

from config.unified_config import Settings, settings

_UNSET_SECRETS = {"", "REPLACE_ME"}

GROUPS: list[tuple[str, str]] = [
    ("network", "Network & TLS"),
    ("polling", "Polling"),
    ("auth", "Auth & Security"),
    ("history", "History"),
    ("energy", "Energy & Pricing"),
    ("backup", "Backups"),
    ("audit", "Audit Log"),
    ("discord", "Discord Bot"),
    ("companion", "Companion (PWA)"),
    ("gateway", "Inference Gateway"),
    ("proxies", "Proxied UIs"),
    ("openclaw", "OpenClaw Budget"),
    ("branding", "Branding"),
    ("alarm_engine", "Alarm Engine"),
    ("ae_behaviour", "Alarms & Retention"),
    ("influxdb", "InfluxDB"),
    ("notifications", "Notifications"),
    ("logging", "Logging"),
]

MANAGER, AE, BOTH = "manager", "alarm_engine", "both"


def _e(path: str, typ: str, label: str, help_: str, group: str, service: str,
       secret: bool = False, choices: Optional[list] = None,
       min: Optional[float] = None, max: Optional[float] = None,
       nullable: bool = False, hot: bool = False, common: bool = False) -> dict:
    d = {"path": path, "type": typ, "label": label, "help": help_,
         "group": group, "service": service, "secret": secret}
    if choices is not None:
        d["choices"] = choices
    if min is not None:
        d["min"] = min
    if max is not None:
        d["max"] = max
    if nullable:
        d["nullable"] = True
    if hot:
        d["hot"] = True  # applied at runtime; never flags a restart
    if common:
        d["common"] = True  # shown in the Admin → Settings "Most used" card
    return d


CATALOG: list[dict] = [
    # network
    _e("manager.port", "int", "HTTP port", "Dashboard HTTP listen port.", "network", MANAGER, min=1, max=65535, common=True),
    _e("manager.tls_port", "int", "HTTPS port", "Dashboard HTTPS port; 0 disables.", "network", MANAGER, min=0, max=65535),
    _e("manager.tls_cert_file", "str", "Operator TLS cert (PEM)", "Full-chain cert served via SNI to matching hostnames; blank = internal CA only.", "network", MANAGER),
    _e("manager.tls_key_file", "str", "Operator TLS key (PEM)", "Key for the operator cert.", "network", MANAGER),
    _e("manager.ws_proxy_port", "int", "WS proxy port", "Browser /ws/alarm → AE /ws relay; 0 disables (breaks live toasts if the AE enforces a token).", "network", MANAGER, min=0, max=65535),
    _e("manager.ws_proxy_tls_port", "int", "WSS proxy port", "wss twin, active only when the operator cert is set; 0 disables.", "network", MANAGER, min=0, max=65535),
    _e("manager.stream_proxy_port", "int", "SSE daemon port", "Standalone llama-state SSE daemon; 0 = fall back to the main pool.", "network", MANAGER, min=0, max=65535),
    _e("manager.alarm_engine_url", "str", "Alarm engine URL", "Where the manager finds the AE. Split install: use the AE host's IP.", "network", MANAGER, common=True),
    _e("manager.cors_origins", "str", "CORS origins", "Allowed browser origins for the manager API.", "network", MANAGER),
    # polling
    _e("manager.poll_interval", "int", "Idle poll interval (s)", "Dashboard cadence while llama sleeps and LM Studio is idle.", "polling", MANAGER, min=5, max=3600, common=True),
    _e("manager.fast_poll_interval", "int", "Active poll interval (s)", "Cadence while a provider is active.", "polling", MANAGER, min=2, max=600),
    _e("manager.wait_timeout", "int", "Llama idle threshold (s)", "Seconds without work before llama-server counts as sleeping.", "polling", MANAGER, min=5, max=3600),
    # auth
    _e("manager.auth.mode", "choice", "Auth mode", "auto = UI-managed via the Access Control card; any other value overrides that card on restart.", "auth", MANAGER, choices=["auto", "required", "trusted_cidr", "disabled"], common=True),
    _e("manager.auth.session_lifetime_days", "int", "Session lifetime (days)", "Browser session validity.", "auth", MANAGER, min=1, max=365, common=True),
    _e("manager.auth.bypass_role", "choice", "Bypass role", "Role granted to sessions that skip login (trusted CIDR / disabled mode).", "auth", MANAGER, choices=["admin", "operator"]),
    _e("manager.auth.lockout_threshold", "int", "Lockout threshold", "Failed logins before lockout.", "auth", MANAGER, min=1, max=100),
    _e("manager.auth.lockout_window_s", "int", "Lockout window (s)", "Window the failures are counted in.", "auth", MANAGER, min=60, max=86400),
    _e("manager.auth.lockout_duration_s", "int", "Lockout duration (s)", "How long a lockout lasts.", "auth", MANAGER, min=60, max=86400),
    _e("manager.security.admin_cidrs", "list", "Admin CIDRs", "Networks allowed to call admin-only endpoints. One CIDR per line.", "auth", MANAGER),
    _e("manager.security.stream_token_ttl_s", "int", "Stream token TTL (s)", "Lifetime of short-lived SSE/WS tickets.", "auth", MANAGER, min=30, max=3600),
    _e("manager.security.tls_rotation_warn_days", "int", "Cert expiry warning (days)", "Warn when TLS certs expire within N days.", "auth", MANAGER, min=1, max=365),
    # history
    _e("manager.history.window_minutes", "int", "History window (min)", "Ring-buffer depth behind /api/history; RAM grows with it.", "history", MANAGER, min=1, max=1440),
    _e("manager.history.refresh_interval_s", "float", "Refresh interval (s)", "Background refresher cadence.", "history", MANAGER, min=1, max=300),
    _e("manager.history.fetch_limit", "int", "Fetch limit", "AE fetch cap per series per refresh.", "history", MANAGER, min=100, max=100000),
    _e("manager.history.max_response_rows", "int", "Max response rows", "/api/history rows thinned above this; 0 = raw.", "history", MANAGER, min=0, max=100000),
    # energy
    _e("manager.reportcard.price_kwh", "float", "Report-card $/kWh", "Electricity price for the report card's $/Mtok estimate.", "energy", MANAGER, min=0, max=10),
    _e("manager.energy.price_kwh", "float", "Energy $/kWh", "Energy-tab price; blank inherits the report card's.", "energy", MANAGER, min=0, max=10, nullable=True, common=True),
    _e("manager.energy.retention_days", "int", "Hourly retention (days)", "Prune energy_hourly rows older than this; blank keeps them forever.", "energy", MANAGER, min=45, max=3650, nullable=True),
    _e("manager.energy.cloud_price_in_per_mtok", "float", "Cloud $/Mtok in", "Cloud list price (input tokens) for the savings card.", "energy", MANAGER, min=0, max=1000),
    _e("manager.energy.cloud_price_out_per_mtok", "float", "Cloud $/Mtok out", "Cloud list price (output tokens).", "energy", MANAGER, min=0, max=1000),
    _e("manager.energy.cloud_price_label", "str", "Cloud price label", "As-of-dated label shown beside the savings figures.", "energy", MANAGER),
    # backup
    _e("manager.backup.enabled", "bool", "Scheduled backups", "Automatic export archives to data/backups/.", "backup", MANAGER, common=True),
    _e("manager.backup.interval_hours", "float", "Interval (hours)", "0 disables the scheduler.", "backup", MANAGER, min=0, max=8760, common=True),
    _e("manager.backup.keep_last", "int", "Keep last", "Archives retained after pruning.", "backup", MANAGER, min=1, max=1000),
    _e("manager.backup.passphrase", "str", "Backup passphrase", "12+ chars enables AES-256-GCM; blank = plaintext archives.", "backup", MANAGER, secret=True),
    _e("manager.backup.mirror_dir", "str", "Mirror directory", "Optional second copy destination (e.g. a NAS mount).", "backup", MANAGER),
    # audit (#794) — hot: the manager re-reads these after every save
    _e("manager.audit.retention_days", "int", "Keep entries for (days)", "0 keeps everything; the 100,000-row cap still applies.", "audit", MANAGER, min=0, max=3650, hot=True, common=True),
    _e("manager.audit.page_size", "int", "Rows per page", "Default page size on the Audit Log tab.", "audit", MANAGER, min=10, max=500, hot=True),
    _e("manager.audit.save_automated", "bool", "Unit tests", "Requests tagged X-LLMSys-Source: test (unit tests) are excluded when disabled.", "audit", MANAGER, hot=True),
    _e("manager.audit.disabled_events", "list", "Disabled events", "Event keys that are not recorded.", "audit", MANAGER, hot=True),
    # discord bot
    _e("manager.discord.enabled", "bool", "Discord bot", "Interactive /fleet /host /models /alarms bot.", "discord", MANAGER),
    _e("manager.discord.bot_token", "str", "Bot token", "Discord bot token.", "discord", MANAGER, secret=True),
    _e("manager.discord.guild_id", "str", "Guild ID", "Server the bot binds slash-commands to.", "discord", MANAGER),
    _e("manager.discord.allowed_user_ids", "list", "Allowed user IDs", "Empty list = refuse all. One ID per line.", "discord", MANAGER),
    _e("manager.discord.allow_model_control", "bool", "Allow model control", "Enables /load and /unload for allowed users.", "discord", MANAGER),
    # companion
    _e("manager.companion.push_contact", "str", "Push contact", "VAPID sub claim the browser push services see (mailto:…).", "companion", MANAGER),
    _e("manager.companion.release_check", "bool", "Release check", "Opt-in GitHub release check (the manager's only outbound github.com call).", "companion", MANAGER, common=True),
    _e("manager.companion.release_repo", "str", "Release repo", "GitHub repo the check queries.", "companion", MANAGER),
    _e("manager.companion.push_notify_token", "str", "Push notify token", "Bearer the AE presents on push delivery; blank falls back to AE tokens.", "companion", MANAGER, secret=True),
    # gateway
    _e("manager.gateway.enabled", "bool", "Gateway enabled", "OpenAI-compatible /api/gateway endpoint.", "gateway", MANAGER, hot=True, common=True),
    _e("manager.gateway.api_keys", "list", "Gateway API keys", "Bearer keys for external clients; empty = dashboard sessions only. One per line, optionally \"label=secret\" to name the client in the Routing card.", "gateway", MANAGER, secret=True),
    _e("manager.gateway.read_timeout_s", "float", "Read timeout (s)", "Upstream cap per completion request.", "gateway", MANAGER, min=10, max=7200),
    _e("manager.gateway.expose_proxied_to", "bool", "Expose X-Proxied-To", "Response header naming the serving agent; off hides backend hostnames.", "gateway", MANAGER),
    _e("manager.gateway.usage_probe", "bool", "Usage probe on streams", "Inject stream_options.include_usage on usage-counted streams; off if a backend rejects stream_options.", "gateway", MANAGER),
    # proxies
    _e("manager.proxies.llm_chat", "str", "Llama Chat UI", "auto | false | explicit http URL.", "proxies", MANAGER),
    _e("manager.proxies.openclaw", "str", "OpenClaw UI", "auto | false | explicit http URL.", "proxies", MANAGER),
    _e("manager.proxies.image_gen", "str", "Image-gen UI", "auto | false | explicit http URL.", "proxies", MANAGER),
    # openclaw
    _e("openclaw.monthly_budget_usd", "float", "Monthly budget (USD)", "0 disables budget metrics + seeded rules.", "openclaw", MANAGER, min=0, max=1000000),
    _e("openclaw.budget_warning_pct", "float", "Warning at (%)", "Warn at this percent of budget.", "openclaw", MANAGER, min=1, max=100),
    _e("openclaw.notify_cost_anomalies", "bool", "Cost anomaly alerts", "Alert when a session's cost is >2x the rolling average.", "openclaw", MANAGER),
    # branding
    _e("manager.branding.palette", "choice", "Accent palette", "Login page + logo accent.", "branding", MANAGER, choices=["teal", "indigo", "forest", "steel"]),
    # alarm engine
    _e("alarm_engine.port", "int", "AE port", "Alarm engine listen port.", "alarm_engine", AE, min=1, max=65535),
    _e("alarm_engine.evaluation_interval", "int", "Rule eval interval (s)", "Alert rule evaluation cadence.", "alarm_engine", AE, min=5, max=3600, common=True),
    _e("alarm_engine.metric_max_age_s", "int", "Metric max age (s)", "A rule is skipped as stale when its newest metric point is older than this.", "alarm_engine", AE, min=30, max=86400),
    _e("alarm_engine.manager_url", "str", "Manager URL", "AE's back-channel to the manager.", "alarm_engine", AE),
    _e("alarm_engine.cors_origins", "str", "AE CORS origins", "Allowed browser origins for the AE API.", "alarm_engine", AE),
    # The manager reads these three too (bearer, agent handoff, URL scheme),
    # so they are written to both files on a split install.
    _e("alarm_engine.ingest_token", "str", "Ingest token", "Bearer gating agent metric pushes; blank leaves ingest OPEN. Propagates to agents within ≤60 s.", "alarm_engine", BOTH, secret=True),
    _e("alarm_engine.management_token", "str", "Management token", "Bearer for AE management routes; blank = ingest token accepted. Required (no ingest fallback) for split-install settings sync.", "alarm_engine", BOTH, secret=True),
    _e("alarm_engine.tls_enabled", "bool", "AE TLS", "Serve the AE over HTTPS (encrypts the ingest-token path).", "alarm_engine", BOTH),
    # ae behaviour
    _e("alarm_engine.correlation.enabled", "bool", "Alert correlation", "false = every alert self-roots.", "ae_behaviour", AE),
    _e("alarm_engine.correlation.window_seconds", "float", "Correlation window (s)", "Same-host time-window join.", "ae_behaviour", AE, min=1, max=3600),
    _e("alarm_engine.correlation.notify_per_incident", "bool", "Notify per incident", "Suppress channel dispatch for joiner alerts (toasts unaffected).", "ae_behaviour", AE),
    _e("alarm_engine.retention.alert_history_days", "int", "Alert history (days)", "Purge alert history past this; 0 = keep forever.", "ae_behaviour", AE, min=0, max=3650),
    _e("alarm_engine.retention.purge_interval_s", "float", "Purge interval (s)", "How often the purge task runs.", "ae_behaviour", AE, min=60, max=86400),
    _e("alarm_engine.default_rules.cpu_usage_critical", "float", "CPU usage critical (%)", "Seed threshold, applied only at first boot.", "ae_behaviour", AE, min=1, max=100),
    _e("alarm_engine.default_rules.cpu_temp_critical", "float", "CPU temp critical (°C)", "Seed threshold, applied only at first boot.", "ae_behaviour", AE, min=1, max=150),
    _e("alarm_engine.default_rules.gpu_temp_critical", "float", "GPU temp critical (°C)", "Seed threshold, applied only at first boot.", "ae_behaviour", AE, min=1, max=150),
    _e("alarm_engine.default_rules.gpu_vram_critical", "float", "GPU VRAM critical (%)", "Seed threshold, applied only at first boot.", "ae_behaviour", AE, min=1, max=100),
    _e("alarm_engine.default_rules.ram_usage_critical", "float", "RAM usage critical (%)", "Seed threshold, applied only at first boot.", "ae_behaviour", AE, min=1, max=100),
    # influxdb
    _e("influxdb.host", "str", "InfluxDB host", "Metrics store host.", "influxdb", BOTH),
    _e("influxdb.port", "int", "InfluxDB port", "Metrics store port.", "influxdb", BOTH, min=1, max=65535),
    _e("influxdb.org", "str", "InfluxDB org", "Organization name.", "influxdb", BOTH),
    _e("influxdb.metrics_bucket", "str", "Metrics bucket", "Raw points, short retention.", "influxdb", BOTH),
    _e("influxdb.metrics_rollup_bucket", "str", "Rollup bucket", "1-min rollup, long retention.", "influxdb", BOTH),
    _e("influxdb.tokens.metrics", "str", "Metrics token", "Bucket-scoped read+write token.", "influxdb", BOTH, secret=True),
    _e("influxdb.tokens.metrics_rollup", "str", "Rollup token", "Blank disables the rollup split (reads fall back to raw scans).", "influxdb", BOTH, secret=True),
    _e("influxdb.tokens.admin", "str", "Admin token", "Used only at startup to create the rollup Flux task; blank skips it.", "influxdb", BOTH, secret=True),
    # notifications
    _e("notifications.smtp.server", "str", "SMTP server", "Outgoing mail server.", "notifications", BOTH),
    _e("notifications.smtp.port", "int", "SMTP port", "Usually 587.", "notifications", BOTH, min=1, max=65535),
    _e("notifications.smtp.user", "str", "SMTP user", "Mail account username.", "notifications", BOTH),
    _e("notifications.smtp.password", "str", "SMTP password", "App-specific password.", "notifications", BOTH, secret=True),
    _e("notifications.twilio.account_sid", "str", "Twilio SID", "Blank disables SMS.", "notifications", BOTH, secret=True),
    _e("notifications.twilio.auth_token", "str", "Twilio auth token", "Twilio API token.", "notifications", BOTH, secret=True),
    _e("notifications.twilio.from_number", "str", "Twilio from number", "Sender number for SMS alerts.", "notifications", BOTH),
    _e("notifications.discord.webhook_url", "str", "Discord webhook", "Blank disables Discord channel notifications.", "notifications", BOTH, secret=True),
    # logging
    _e("logging.level", "choice", "Default log level", "Both services; per-service overrides below.", "logging", BOTH, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], common=True),
    _e("manager.log_level", "choice", "Manager log level", "Falls back to the default level.", "logging", MANAGER, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    _e("alarm_engine.log_level", "choice", "AE log level", "Falls back to the default level.", "logging", AE, choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
]

_BY_PATH = {e["path"]: e for e in CATALOG}


def entry_for(path: str) -> Optional[dict]:
    return _BY_PATH.get(path)


def is_hot(path: str) -> bool:
    e = _BY_PATH.get(path)
    return bool(e and e.get("hot"))


def secret_status(value) -> str:
    """'set' / 'unset' chip for a secret's current value."""
    if isinstance(value, list):
        return "set" if value else "unset"
    return "unset" if (value or "") in _UNSET_SECRETS else "set"


class _FileOnlySettings(Settings):
    """Settings variant fed ONLY by init kwargs — no env/file sources."""
    model_config = {"toml_file": None}

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings,
                                   env_settings, dotenv_settings,
                                   file_secret_settings):
        return (init_settings,)


def _snapshot():
    """Typed snapshot of the on-disk config so reads reflect saved-but-unapplied
    edits; falls back to the boot-time singleton."""
    try:
        import settings_toml_io
        path = settings_toml_io.resolve_config_path()
        if path.is_file():
            return _FileOnlySettings(**tomllib.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass  # unreadable/invalid file: serve the boot-time values below
    return settings


def _current(path: str, root=None):
    node = root if root is not None else settings
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


_DEFAULTS_CACHE: "Optional[dict]" = None


def defaults() -> dict:
    """{path: pydantic model default} for every non-secret entry; a secret
    never has a shipped default worth showing."""
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is not None:
        return dict(_DEFAULTS_CACHE)
    out: dict[str, Any] = {}
    try:
        root = _FileOnlySettings()
    except Exception:
        _DEFAULTS_CACHE = {}
        return {}
    for e in CATALOG:
        if e["secret"]:
            continue
        val = _current(e["path"], root)
        if val is not None:
            out[e["path"]] = val
    _DEFAULTS_CACHE = out
    return dict(out)


def describe() -> dict:
    values: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    snap = _snapshot()
    for e in CATALOG:
        cur = _current(e["path"], snap)
        if e["secret"]:
            secrets[e["path"]] = secret_status(cur)
        elif cur is not None:
            values[e["path"]] = cur
    return {
        "groups": [{"key": k, "title": t} for k, t in GROUPS],
        "entries": [dict(e) for e in CATALOG],
        "values": values,
        "secrets": secrets,
        "defaults": defaults(),
    }


def _coerce(entry: dict, value: Any):
    typ = entry["type"]
    if typ == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected true/false")
    if typ == "int":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        v = int(value)
        if isinstance(value, float) and value != v:
            raise ValueError("expected a whole number")
    elif typ == "float":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
    elif typ in ("str", "choice"):
        if not isinstance(value, str):
            raise ValueError("expected a string")
        v = value.strip()
        if typ == "choice" and v not in entry["choices"]:
            raise ValueError(f"must be one of: {', '.join(entry['choices'])}")
    elif typ == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("expected a list of strings")
        v = [x.strip() for x in value if x.strip()]
    else:
        raise ValueError(f"unknown type {typ}")
    if entry.get("min") is not None and v < entry["min"]:
        raise ValueError(f"must be ≥ {entry['min']}")
    if entry.get("max") is not None and v > entry["max"]:
        raise ValueError(f"must be ≤ {entry['max']}")
    return v


def validate_and_coerce(changes: dict) -> tuple[dict, dict]:
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for path, value in changes.items():
        entry = _BY_PATH.get(path)
        if entry is None:
            errors[path] = "not an editable setting"
            continue
        if entry["secret"]:
            if value == "" or value == []:
                continue  # blank secret input = leave unchanged
            if value is None:
                clean[path] = [] if entry["type"] == "list" else ""
                continue
        elif value is None:
            # None = remove the key, so the model default applies again.
            clean[path] = None
            continue
        try:
            clean[path] = _coerce(entry, value)
        except (ValueError, TypeError) as e:
            errors[path] = str(e)
    return clean, errors


_MISSING = object()


def file_catalog_values() -> "Optional[dict]":
    """Raw on-disk value per catalog path (_MISSING when the key is absent);
    None when the file can't be read."""
    try:
        import settings_toml_io
        path = settings_toml_io.resolve_config_path()
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        return None
    out: dict[str, Any] = {}
    for e in CATALOG:
        node: Any = data
        for part in e["path"].split("."):
            if not isinstance(node, dict) or part not in node:
                node = _MISSING
                break
            node = node[part]
        out[e["path"]] = node
    return out


# Raw file values at process start; pending_restart derives from drift off this.
_BOOT_FILE_VALUES = file_catalog_values()


def pending_restart_services(now: "Optional[dict]" = None) -> set[str]:
    """Services whose on-disk catalog values differ from the file this process
    started with. Stateless across UI saves, hand edits, and shell restarts."""
    if _BOOT_FILE_VALUES is None:
        return set()
    if now is None:
        now = file_catalog_values()
    if now is None:
        return set()
    changed = [p for p, v in now.items()
               if v != _BOOT_FILE_VALUES.get(p, _MISSING)]
    return services_for(changed)


def services_for(paths) -> set[str]:
    out: set[str] = set()
    for p in paths:
        e = _BY_PATH.get(p)
        if e is None or is_hot(p):
            continue
        out |= {MANAGER, AE} if e["service"] == BOTH else {e["service"]}
    return out
