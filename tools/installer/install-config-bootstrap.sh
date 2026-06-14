#!/usr/bin/env bash
# =============================================================================
# tools/installer/install-config-bootstrap.sh
#
# Copies config/llm-systems.toml.example → config/llm-systems.toml, prompts
# the operator for the values that have to be host-specific (manager IPs,
# SMTP creds), and substitutes generated InfluxDB tokens from data/influxdb.env
# if present.
#
# Idempotent: if the real config already exists, it's backed up to
# llm-systems.toml.bak.<timestamp> before rewriting (only when overwrite is
# confirmed).
#
# All writes happen as the llmsys user with mode 0600 — secrets only readable
# by the service user.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

detect_os
require_linux
detect_sudo

INSTALL_DIR="${LLMSYS_INSTALL_DIR}"
USER_ARG="${LLMSYS_RUN_USER}"
CFG_DIR="$INSTALL_DIR/config"
EXAMPLE="$CFG_DIR/llm-systems.toml.example"
REAL="$CFG_DIR/llm-systems.toml"
ENV_FILE="$LLMSYS_INFLUXDB_TOKEN_FILE"

[[ -f "$EXAMPLE" ]] || die "Missing example config: $EXAMPLE"

banner "Config bootstrap"

if [[ -f "$REAL" ]]; then
  log "existing $REAL found"
  if [[ -t 0 ]]; then
    read -rp "  Overwrite (existing file will be backed up)? [Y/n] " ans
    case "$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')" in
      n|no) ok "keeping existing config — exiting"; exit 0 ;;
      *) ;;
    esac
  else
    log "stdin not a TTY — overwriting (existing file will be backed up)"
  fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  $SUDO cp -a "$REAL" "$REAL.bak.$stamp"
  ok "backed up existing config → llm-systems.toml.bak.$stamp"
fi

# Copy template
$SUDO cp -a "$EXAMPLE" "$REAL"
$SUDO chown "$USER_ARG:$LLMSYS_RUN_GROUP" "$REAL"
$SUDO chmod 0600 "$REAL"
ok "copied template → $REAL (mode 0600)"

# Auto-detect the primary LAN IP and its /24 — this is this host's address
# from the rest of the lab's perspective.
DETECTED_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$DETECTED_IP" ]] && DETECTED_IP="127.0.0.1"
DETECTED_SUBNET="$(printf '%s' "$DETECTED_IP" | awk -F. '{ printf "%s.%s.%s.0/24", $1, $2, $3 }')"
log "detected primary IP: $DETECTED_IP  (suggested subnet: $DETECTED_SUBNET)"

# What gets installed on THIS box, derived from the install mode:
#   Mode 1/2 → manager + alarm engine co-located here (defaults to localhost
#              for the back-channel URLs).
#   Mode 3   → only the manager here; the alarm engine lives elsewhere.
#   Mode 4   → only the alarm engine here; the manager lives elsewhere.
# Defaults to "1" so a direct re-run of this script (without install.sh
# wrapping it) still works for the all-in-one case.
MODE="${LLMSYS_INSTALL_MODE:-1}"
case "$MODE" in
  1|2) HAS_MGR=1; HAS_AE=1 ;;
  3)   HAS_MGR=1; HAS_AE=0 ;;
  4)   HAS_MGR=0; HAS_AE=1 ;;
  5)   die "Mode 5 (agent-only) doesn't use the manager config — run agent/install/install.sh instead." ;;
  6)   die "Mode 6 (InfluxDB-only) doesn't need a manager config — tokens are printed to stdout at the end of install-influxdb.sh." ;;
  *)   die "unknown LLMSYS_INSTALL_MODE='$MODE' (expected 1-6)" ;;
esac
log "configuring for install mode $MODE (manager=$HAS_MGR, alarm_engine=$HAS_AE)"

# ── Prompts ─────────────────────────────────────────────────────────────────
PROMPT=true
[[ -t 0 ]] || PROMPT=false

# Defaults applied to whichever prompts get skipped.
MGR_IP=""; MGR_HOST="0.0.0.0"; MGR_PORT="5000"
ADMIN_CIDR=""
ADMIN_USER="llmadmin"; ADMIN_PW=""; ADMIN_PW_HASH=""
AE_HOST="0.0.0.0"; AE_PORT="8081"
ALARM_ENGINE_URL=""
MANAGER_URL=""
SMTP_SERVER=""; SMTP_USER=""; SMTP_PASS=""
MGR_INGEST_TOKEN_PASTE=""

if $PROMPT; then
  banner "Interactive config edits (mode $MODE)"
  echo "  Press ENTER to keep the default shown in brackets."
  echo

  if (( HAS_MGR )); then
    read -rp "  Manager IP (browser-facing) [$DETECTED_IP]: " MGR_IP
    MGR_IP="${MGR_IP:-$DETECTED_IP}"
    read -rp "  Manager listen host [0.0.0.0]: " ans;   MGR_HOST="${ans:-$MGR_HOST}"
    read -rp "  Manager port [5000]: "          ans;   MGR_PORT="${ans:-$MGR_PORT}"
    read -rp "  admin CIDR (subnet allowed to call admin endpoints) [$DETECTED_SUBNET]: " ADMIN_CIDR
    ADMIN_CIDR="${ADMIN_CIDR:-$DETECTED_SUBNET}"
    echo
    echo "  Dashboard admin login. Press ENTER on the password to keep the"
    echo "  built-in default (llmadmin / llmadmin) — change it later in"
    echo "  Admin → Authentication. Custom passwords must be at least 8"
    echo "  characters, printable only (no control chars / newlines / NUL)."
    read -rp  "  Manager admin username [llmadmin]: " ans; ADMIN_USER="${ans:-llmadmin}"
    while :; do
      read -rsp "  Manager admin password [llmadmin]: " ADMIN_PW; echo
      if [[ -z "$ADMIN_PW" ]]; then
        break
      fi
      if (( ${#ADMIN_PW} < 8 )); then
        warn "  password must be at least 8 characters (got ${#ADMIN_PW}) — try again."
        continue
      fi
      # Reject any control char (incl. NUL, \n, \r, \t, ESC) — printable ASCII
      # plus high-bit unicode are fine. Comparing byte length before/after a
      # `tr -d [:cntrl:]` strip catches embedded newlines too, which a plain
      # line-oriented grep misses.
      _stripped_len=$(LC_ALL=C printf '%s' "$ADMIN_PW" | LC_ALL=C tr -d '[:cntrl:]' | wc -c)
      _raw_len=$(LC_ALL=C printf '%s' "$ADMIN_PW" | wc -c)
      if (( _stripped_len != _raw_len )); then
        warn "  password contains control characters (newline/tab/etc.) — try again."
        continue
      fi
      break
    done
  fi

  if (( HAS_AE )); then
    read -rp "  Alarm engine listen host [0.0.0.0]: " ans; AE_HOST="${ans:-$AE_HOST}"
    read -rp "  Alarm engine port [8081]: "          ans; AE_PORT="${ans:-$AE_PORT}"
  fi

  # Cross-host wiring: Mode 3 needs to know where the remote alarm engine
  # lives; Mode 4 needs to know where the remote manager lives. Modes 1/2
  # default the back-channel to localhost since both services are colocated.
  if (( HAS_MGR && ! HAS_AE )); then
    echo
    echo "  The manager proxies the alarm-engine UI and API. Where does the"
    echo "  alarm engine run? (e.g. http://192.0.2.10:8081, or just"
    echo "  192.0.2.10 — http:// and :8081 are auto-added.)"
    read -rp "  Alarm engine URL [http://${DETECTED_IP}:8081]: " ALARM_ENGINE_URL
    ALARM_ENGINE_URL="${ALARM_ENGINE_URL:-http://${DETECTED_IP}:8081}"
    ALARM_ENGINE_URL="$(sanitize_url "$ALARM_ENGINE_URL" 8081)"
    log "  → using $ALARM_ENGINE_URL"
    _ae_h="$(url_host "$ALARM_ENGINE_URL")"
    # A hostname here must resolve on EVERY agent; an IP needs no DNS and the
    # AE TLS cert SAN auto-covers it. Nudge toward an IP for split installs.
    if [[ ! "$_ae_h" =~ ^[0-9.]+$ && "$_ae_h" != *:* ]]; then
      warn "  '$_ae_h' is a hostname — every agent must resolve it (DNS or /etc/hosts)."
      warn "  Prefer an IP here so agents need no name resolution (the AE TLS cert covers it)."
    fi
    check_resolves "$_ae_h" "alarm engine host" \
      || warn "  Continuing anyway; you can fix /etc/hosts and re-run install."
    # Offer to paste the AE's generated ingest_token now. The Mode 4 installer
    # writes it commented-out in the AE's TOML so the operator has a value to
    # capture; entering it here lands it live on the manager side. Empty Enter
    # = skip and edit the TOML later.
    echo
    echo "  The alarm engine generates an ingest_token at install (Mode 4)."
    echo "  Paste it here to wire the manager side now, or press Enter to add"
    echo "  it later via [alarm_engine].ingest_token in $REAL."
    read -rp "  AE ingest_token (or Enter to skip): " MGR_INGEST_TOKEN_PASTE
    MGR_INGEST_TOKEN_PASTE="$(printf '%s' "$MGR_INGEST_TOKEN_PASTE" | tr -d '[:space:]')"
    if [[ -n "$MGR_INGEST_TOKEN_PASTE" ]]; then
      log "  → ingest_token will be written to [alarm_engine].ingest_token"
    else
      log "  → no ingest_token entered; warning will be emitted below"
    fi
    # InfluxDB host. The manager doesn't query InfluxDB for data (the AE does),
    # but it uses [influxdb].host for the admin tab's InfluxDB status/co-location
    # chip. Left at the template default (localhost) it points the chip at the
    # manager host instead of the real DB server. Default to the alarm-engine
    # host since the DB is usually co-located with the AE on a split install.
    echo
    echo "  Where does InfluxDB run? Used for the admin tab's InfluxDB status."
    echo "  Usually the same host as the alarm engine. (e.g. http://192.0.2.10:8086,"
    echo "  or just 192.0.2.10 — http:// and :8086 are auto-added.)"
    _influx_default="$(url_host "$ALARM_ENGINE_URL")"
    read -rp "  InfluxDB host [${_influx_default}]: " _influx_in
    _influx_in="$(sanitize_url "${_influx_in:-$_influx_default}" 8086)"
    INFLUX_HOSTNAME="$(url_host "$_influx_in")"
    INFLUX_PORT="$(url_port "$_influx_in")"
    [[ -n "$INFLUX_PORT" ]] || INFLUX_PORT="8086"
    log "  → InfluxDB at ${INFLUX_HOSTNAME}:${INFLUX_PORT}"
    check_resolves "$INFLUX_HOSTNAME" "InfluxDB host" \
      || warn "  Continuing anyway; you can fix /etc/hosts and re-run install."
  elif (( HAS_AE && ! HAS_MGR )); then
    echo
    echo "  The alarm engine pushes back-channel calls to the manager. Where"
    echo "  does the manager run? (e.g. http://192.0.2.10:5000, or just"
    echo "  192.0.2.10 — http:// and :5000 are auto-added.)"
    read -rp "  Manager URL [http://${DETECTED_IP}:5000]: " MANAGER_URL
    MANAGER_URL="${MANAGER_URL:-http://${DETECTED_IP}:5000}"
    MANAGER_URL="$(sanitize_url "$MANAGER_URL" 5000)"
    log "  → using $MANAGER_URL"
    # MGR_IP is still needed downstream for CORS allow-lists.
    MGR_IP="$(url_host "$MANAGER_URL")"
    check_resolves "$MGR_IP" "manager host" \
      || warn "  Continuing anyway; you can fix /etc/hosts and re-run install."
  fi

  # SMTP only matters where the alarm engine runs (it's the sender).
  if (( HAS_AE )); then
    echo
    echo "  SMTP — leave password blank to disable email alerts."
    read -rp "  SMTP server [smtp.example.com]: " SMTP_SERVER
    read -rp "  SMTP user (e.g. you@example.com): " SMTP_USER
    read -rp "  SMTP app-password: " SMTP_PASS
  fi
else
  log "stdin not a TTY — using detected defaults"
  if (( HAS_MGR )); then
    MGR_IP="$DETECTED_IP"; ADMIN_CIDR="$DETECTED_SUBNET"
  fi
  if (( HAS_MGR && ! HAS_AE )); then
    ALARM_ENGINE_URL="http://${DETECTED_IP}:8081"
    # Best-effort guess for the admin tab's InfluxDB chip — default to the AE
    # host (== detected IP here). Override in [influxdb].host for a real split.
    INFLUX_HOSTNAME="$DETECTED_IP"; INFLUX_PORT="8086"
  elif (( HAS_AE && ! HAS_MGR )); then
    MANAGER_URL="http://${DETECTED_IP}:5000"
    MGR_IP="$DETECTED_IP"
  fi
fi

# ── Load generated InfluxDB tokens if available ─────────────────────────────
# Only the alarm engine talks to InfluxDB; skip the token plumbing entirely
# in Mode 3 (manager-only).
INFLUX_HOST=""
INFLUX_METRICS_TOKEN=""
INFLUX_METRICS_ROLLUP_TOKEN=""
INFLUX_OPERATOR_TOKEN=""
# Preserve a value set by the Mode 3 prompt / non-TTY default above; otherwise
# default to empty (HAS_AE modes fill these from the token handoff below).
: "${INFLUX_HOSTNAME:=}"
: "${INFLUX_PORT:=}"
if (( HAS_AE )); then
  if $SUDO test -f "$ENV_FILE"; then
    set +u
    # shellcheck disable=SC1090
    source <($SUDO cat "$ENV_FILE")
    set -u
    ok "loaded InfluxDB tokens from in-process handoff"
    [[ -n "$INFLUX_METRICS_TOKEN" ]] \
      || die "$ENV_FILE present but INFLUX_METRICS_TOKEN is empty — re-run resolve-influxdb.sh or install-influxdb.sh"
    # Parse INFLUX_HOST (a full URL like http://192.0.2.10:8086) into the
    # hostname + port the TOML expects in [influxdb].
    if [[ -n "$INFLUX_HOST" ]]; then
      INFLUX_HOSTNAME="$(url_host "$INFLUX_HOST")"
      INFLUX_PORT="$(url_port "$INFLUX_HOST")"
      [[ -n "$INFLUX_PORT" ]] || INFLUX_PORT="8086"
      check_resolves "$INFLUX_HOSTNAME" "InfluxDB host" \
        || warn "  Continuing anyway; you can fix /etc/hosts and re-run install."
    fi
  else
    warn "$ENV_FILE not present — InfluxDB tokens left as REPLACE_ME"
    warn "  set [influxdb] host/port + [influxdb.tokens].* in $REAL before starting the alarm engine"
  fi
else
  log "Mode $MODE has no alarm engine on this host — skipping InfluxDB token setup"
fi

# Shared alarm-engine ingest token. Only auto-generated when the manager AND the
# alarm engine land on THIS host — then the manager reads the same token and can
# hand it to agents, so the ingest surface ships locked down with no manual step.
# On a split install (manager and AE on different hosts) we leave it blank (ingest
# stays open, back-compat) because the two hosts have separate TOMLs: a token set
# only on the AE host would make the manager hand agents a blank token and every
# push would 401. The operator must then set the SAME ingest_token in both hosts'
# config and restart, which the warning below points out.
INGEST_TOKEN=""
INGEST_COMMENTED=0
if (( HAS_AE && HAS_MGR )); then
  INGEST_TOKEN="$(openssl rand -hex 32)"
  ok "generated alarm-engine ingest token (manager + engine co-located)"
elif (( HAS_AE )); then
  # Split AE-only install: generate a token but write it commented out. Setting
  # it live without the matching value on the manager host would 401 every
  # agent push, so we surface the value to the operator and leave activation
  # as a deliberate one-time step once the manager side is configured.
  INGEST_TOKEN="$(openssl rand -hex 32)"
  INGEST_COMMENTED=1
  banner "Save this ingest token — you'll need it on the manager host"
  cat <<EOF

  A security token was generated to protect the metrics channel between
  agents and this alarm engine. The manager host needs the SAME value.

  ──────────────────────────────────────────────────────────────────────
    Ingest token (copy this somewhere safe NOW):

        ${INGEST_TOKEN}

  ──────────────────────────────────────────────────────────────────────

  What to do next:

    1. Save the token above to your password manager / notes.

    2. When you install the manager on the other host, paste it at the
       prompt that reads:
           AE ingest_token (or Enter to skip):

    3. After the manager install finishes, come back to THIS host and
       activate the token here:

         a. Open the config file:
                sudo nano ${REAL}

         b. Find the line that starts with:
                # ingest_token =

         c. Remove the '#' and the space at the start of that line, so
            it reads:
                ingest_token = "...the token value..."

         d. Save and exit nano (Ctrl-O, Enter, Ctrl-X).

    4. Restart the alarm engine on this host:
           sudo systemctl restart llm-systems-alarm-engine

  Until step 4 is complete, agents may still push metrics without the
  token — this prevents being locked out partway through a split install.
EOF
  echo
elif (( HAS_MGR )); then
  if [[ -n "$MGR_INGEST_TOKEN_PASTE" ]]; then
    ok "manager will hand agents the AE ingest_token you provided"
  else
    warn "manager installed without a co-located alarm engine — if the remote engine"
    warn "  enforces an ingest_token, paste it into [alarm_engine].ingest_token in"
    warn "  $REAL and restart llm-systems-manager"
  fi
fi

# Hash the admin password (scrypt, matching the manager's _scrypt_hash) so the
# config stores only the hash — never the plaintext. An empty password leaves
# the hash blank and the manager falls back to the llmadmin/llmadmin default.
if (( HAS_MGR )) && [[ -n "$ADMIN_PW" ]]; then
  ADMIN_PW_HASH="$(ADMIN_PW="$ADMIN_PW" python3 - <<'PYH'
import os, hashlib, base64
salt = os.urandom(16)
dk = hashlib.scrypt(os.environ["ADMIN_PW"].encode("utf-8"),
                    salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
print("scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode())
PYH
)"
  if [[ -n "$ADMIN_PW_HASH" ]]; then
    ok "admin password hashed (scrypt) for user '$ADMIN_USER'"
  else
    warn "admin password hashing failed — built-in default (llmadmin) will apply"
  fi
fi
unset ADMIN_PW

# ── Apply edits via python (TOML-aware string substitution) ─────────────────
as_run_user python3 - "$REAL" <<PYEOF
import re, sys, pathlib

path = pathlib.Path(sys.argv[1])
text = path.read_text()

def sub_in_section(text, section, key, new_value):
    """Replace the first 'key = "..."' line inside [section] (header line
    starts with [section]). Leaves the file untouched if section/key not
    found. Both quoted and unquoted scalars are handled."""
    if new_value is None or new_value == "":
        return text
    pattern = re.compile(
        r'(^\[' + re.escape(section) + r'\][^\[]*?\n' + re.escape(key) + r'\s*=\s*)("[^"]*"|[^\n#]+)',
        re.MULTILINE | re.DOTALL,
    )
    repl = lambda m: m.group(1) + ('"' + str(new_value) + '"')
    return pattern.sub(repl, text, count=1)

def sub_numeric_in_section(text, section, key, new_value):
    if not str(new_value or "").strip():
        return text
    return re.sub(
        r'(^\[' + re.escape(section) + r'\][^\[]*?\n' + re.escape(key) + r'\s*=\s*)(\d+)',
        lambda m: m.group(1) + str(new_value).strip(),
        text, count=1, flags=re.MULTILINE | re.DOTALL,
    )

has_mgr     = ${HAS_MGR}
has_ae      = ${HAS_AE}
mgr_host    = """${MGR_HOST}"""
mgr_port    = """${MGR_PORT}"""
mgr_ip      = """${MGR_IP}"""
admin_cidr  = """${ADMIN_CIDR}"""
admin_user  = """${ADMIN_USER}"""
admin_hash  = """${ADMIN_PW_HASH}"""
ae_host     = """${AE_HOST}"""
ae_port     = """${AE_PORT}"""
ae_url      = """${ALARM_ENGINE_URL}"""
mgr_url     = """${MANAGER_URL}"""
smtp_srv    = """${SMTP_SERVER}"""
smtp_user   = """${SMTP_USER}"""
smtp_pass   = """${SMTP_PASS}"""

tok_metrics        = """${INFLUX_METRICS_TOKEN}"""
tok_metrics_rollup = """${INFLUX_METRICS_ROLLUP_TOKEN}"""
tok_admin          = """${INFLUX_OPERATOR_TOKEN}"""
ingest_token_val      = """${INGEST_TOKEN}"""
ingest_commented_flag = ${INGEST_COMMENTED}
mgr_ingest_paste      = """${MGR_INGEST_TOKEN_PASTE}"""
influx_host        = """${INFLUX_HOSTNAME}"""
influx_port        = """${INFLUX_PORT}"""

# ── Manager-side keys ──
if has_mgr:
    text = sub_in_section(text, "manager", "host", mgr_host)
    text = sub_numeric_in_section(text, "manager", "port", mgr_port)
    # Mode 3 only: operator can paste the AE's ingest_token at prompt time.
    # The [alarm_engine] section is present in the unified TOML regardless
    # of which services this host runs, so writing into it is safe even when
    # the AE itself lives on another box.
    if mgr_ingest_paste and not has_ae:
        text = sub_in_section(text, "alarm_engine", "ingest_token", mgr_ingest_paste)
    # admin_cidrs: keep loopback, swap the placeholder /24 with the detected one.
    if admin_cidr:
        text = re.sub(
            r'(^\[manager\.security\][^\[]*?\nadmin_cidrs\s*=\s*)\[[^\]]*\]',
            lambda m: m.group(1) + f'["127.0.0.1", "::1", "{admin_cidr}"]',
            text, count=1, flags=re.MULTILINE | re.DOTALL,
        )
    # The manager needs to know where the alarm engine lives. In Mode 1/2
    # both are local; in Mode 3 the operator gave us a remote URL.
    if ae_url:
        text = sub_in_section(text, "manager", "alarm_engine_url", ae_url)
    # Dashboard admin login. Only the scrypt hash is written — never the
    # plaintext. Blank hash leaves the built-in llmadmin/llmadmin default.
    if admin_user:
        text = sub_in_section(text, "manager.auth", "username", admin_user)
    if admin_hash:
        text = sub_in_section(text, "manager.auth", "password_hash", admin_hash)

# ── Alarm-engine-side keys ──
if has_ae:
    text = sub_in_section(text, "alarm_engine", "host", ae_host)
    text = sub_numeric_in_section(text, "alarm_engine", "port", ae_port)
    # manager_url: in Mode 1/2 mgr_ip is local; in Mode 4 we asked.
    target_mgr_url = mgr_url or (f"http://{mgr_ip}:5000" if mgr_ip else "")
    if target_mgr_url:
        text = sub_in_section(text, "alarm_engine", "manager_url", target_mgr_url)
    # Shared ingest token. Live for co-located installs; written commented-out
    # for split-AE installs so the operator can uncomment after copying the
    # same value into the manager host's TOML (live without a matching manager
    # value would 401 every agent push).
    if ingest_token_val:
        if ingest_commented_flag:
            text = re.sub(
                r'^(\s*)(ingest_token\s*=\s*)("[^"]*"|[^\n#]+)',
                lambda m: f'{m.group(1)}# {m.group(2)}"{ingest_token_val}"',
                text, count=1, flags=re.MULTILINE,
            )
        else:
            text = sub_in_section(text, "alarm_engine", "ingest_token", ingest_token_val)

# CORS allow-lists and the agent dashboard push URL all need the manager's
# browser-facing IP — whether that's local (Modes 1/2) or remote (Mode 4).
if mgr_ip:
    cors_value = f"http://{mgr_ip}:5000,http://localhost:5000,http://{mgr_ip}:8081"
    if has_mgr:
        text = sub_in_section(text, "manager",      "cors_origins",  cors_value)
    if has_ae:
        text = sub_in_section(text, "alarm_engine", "cors_origins",  cors_value)
    text = sub_in_section(text, "agent", "dashboard_url",
                          f"http://{mgr_ip}:5000/api/remote/lmstudio")

# SMTP + InfluxDB tokens only live where the alarm engine runs.
if has_ae:
    text = sub_in_section(text, "notifications.smtp", "server",   smtp_srv)
    text = sub_in_section(text, "notifications.smtp", "user",     smtp_user)
    text = sub_in_section(text, "notifications.smtp", "password", smtp_pass)
    text = sub_in_section(text, "influxdb.tokens", "metrics_rollup", tok_metrics_rollup)
    text = sub_in_section(text, "influxdb.tokens", "metrics",        tok_metrics)
    text = sub_in_section(text, "influxdb.tokens", "admin",          tok_admin)

# InfluxDB connection host/port is needed by the manager too — it reads
# [influxdb].host for the admin tab's InfluxDB status/co-location chip. The
# manager-only (Mode 3) prompt sets these; sub_* no-op on empty so a missing
# value safely leaves the template default untouched.
if influx_host:
    text = sub_in_section(text, "influxdb", "host", influx_host)
if influx_port:
    text = sub_numeric_in_section(text, "influxdb", "port", influx_port)

path.write_text(text)
print(f"  rewrote {path}")
PYEOF

$SUDO chmod 0600 "$REAL"
ok "$REAL ready (mode 0600)"

# Copy the sanitized unified_config.py.example over the deployed module so
# the live python schema/defaults file matches the example template. The
# TOML above remains the source of truth at runtime — this just keeps the
# code-level defaults free of any maintainer-specific IPs. The .example is
# eventually intended to be the only checked-in copy.
UC_EXAMPLE="$CFG_DIR/unified_config.py.example"
UC_LIVE="$CFG_DIR/unified_config.py"
if [[ -f "$UC_EXAMPLE" ]]; then
  $SUDO cp -a "$UC_EXAMPLE" "$UC_LIVE"
  $SUDO chown "$USER_ARG:$LLMSYS_RUN_GROUP" "$UC_LIVE"
  $SUDO chmod 0644 "$UC_LIVE"
  ok "unified_config.py installed from .example template"
else
  die "$UC_EXAMPLE not found — installer cannot produce a sanitized unified_config.py"
fi

# Strict post-rewrite check: if any of the four token slots still says
# REPLACE_ME, the alarm engine will 401 every read/write — fail now so the
# operator sees the problem before services start. Skipped in Mode 3 where
# the alarm engine isn't on this host.
if (( HAS_AE )) && [[ -r "$ENV_FILE" ]]; then
  STALE=$($SUDO awk '
    /^\[influxdb\.tokens\]/ { in_tokens=1; next }
    /^\[/                   { in_tokens=0 }
    in_tokens && /REPLACE_ME/ { print }
  ' "$REAL")
  if [[ -n "$STALE" ]]; then
    err "After rewrite, the [influxdb.tokens] section still has REPLACE_ME entries:"
    printf '    %s\n' "$STALE"
    die "Token substitution failed — check the heredoc python in install-config-bootstrap.sh"
  fi
  log "tokens substituted (4 slots populated)"
fi

# ── Cross-host reachability probes ─────────────────────────────────────────
# Best-effort: if the remote service the operator just wired isn't up yet,
# warn but don't fail — the operator may be installing this side first.
if (( HAS_MGR && ! HAS_AE )) && [[ -n "$ALARM_ENGINE_URL" ]]; then
  banner "Probing remote alarm engine"
  log "checking $ALARM_ENGINE_URL/health …"
  code="$(probe_url "$ALARM_ENGINE_URL/health" || echo 000)"
  if [[ "$code" == "200" ]]; then
    ok "alarm engine reachable ($code)"
  else
    warn "alarm engine at $ALARM_ENGINE_URL is not reachable yet (HTTP $code)"
    warn "  this is OK if you'll bring it up later — the manager will retry on every request"
    warn "  fix later in $REAL → [manager].alarm_engine_url"
  fi
fi

if (( HAS_AE && ! HAS_MGR )) && [[ -n "$MANAGER_URL" ]]; then
  banner "Probing remote manager"
  log "checking $MANAGER_URL/api/agents …"
  code="$(probe_url "$MANAGER_URL/api/agents" || echo 000)"
  # The manager rejects unauthenticated /api/agents calls with 401/403,
  # which is still proof the service is reachable. 000 = unreachable.
  if [[ "$code" =~ ^(200|401|403)$ ]]; then
    ok "manager reachable ($code)"
  else
    warn "manager at $MANAGER_URL is not reachable yet (HTTP $code)"
    warn "  this is OK if you'll bring it up later — the back-channel will retry"
    warn "  fix later in $REAL → [alarm_engine].manager_url"
  fi
fi

# ── Pre-issue the alarm-engine TLS cert ─────────────────────────────────────
# AE TLS ships on by default, so the cert MUST be on disk before either
# service starts. systemd unit ordering has the manager start AFTER the AE
# (manager.service: After=…llm-systems-alarm-engine.service), so we can't rely
# on the manager's own _ensure_ae_server_cert to land the cert in time on a
# cold boot: the AE would come up first, find no cert, and fail-open to plain
# HTTP. Pre-issuing here is a one-shot at install time using the manager's
# venv (which has `cryptography` from the manager's requirements.txt).
#
# Skipped on:
#   - HAS_AE=0 (manager-only — manager auto-issues into its own data/ as a
#     copy-source for the operator to scp over to the AE host).
#   - HAS_MGR=0 (AE-only — no manager venv here; the manager-host install
#     auto-issues, operator copies.)
if (( HAS_MGR && HAS_AE )); then
  banner "Pre-issuing alarm-engine TLS cert (internal CA)"
  MGR_VENV_PY="$INSTALL_DIR/llm-systems-manager/venv/bin/python3"
  if [[ ! -x "$MGR_VENV_PY" ]]; then
    warn "manager venv not at $MGR_VENV_PY — cert pre-issue skipped"
    warn "  the manager will issue ae-tls.{crt,key} at its first startup;"
    warn "  if the AE comes up before the manager, restart the AE once"
    warn "  the manager has logged 'Alarm-engine TLS cert: issued'."
  else
    # Inline python — imports manager's _pki module via PYTHONPATH so we
    # don't duplicate sign_agent_cert here. Writes data/internal-ca.{crt,key}
    # (created on first call) and llm-systems-alarm-engine/data/ae-tls.{crt,key}.
    AE_CERT_OUT="$(as_run_user \
      env PYTHONPATH="$INSTALL_DIR/llm-systems-manager/backend" \
          INSTALL_DIR="$INSTALL_DIR" \
          DETECTED_IP="$DETECTED_IP" \
          ALARM_ENGINE_URL="$ALARM_ENGINE_URL" \
      "$MGR_VENV_PY" - <<'PYEOF' 2>&1 || true
import os, socket, ipaddress
from pathlib import Path
from urllib.parse import urlparse
import _pki

install_dir  = Path(os.environ["INSTALL_DIR"])
# Must match the runtime manager's DATA_DIR = _REPO_ROOT_PATH / "data"
# (llm-systems-manager.py: DATA_DIR points at <install_dir>/data, NOT
# <install_dir>/llm-systems-manager/data — the package layout puts runtime
# state next to the package dirs, not inside them).
mgr_data_dir = install_dir / "data"
ae_data_dir  = install_dir / "llm-systems-alarm-engine" / "data"
detected_ip  = os.environ.get("DETECTED_IP", "").strip() or "127.0.0.1"

# Whatever host agents will dial for the AE. On a co-located install
# (HAS_AE=1) this is usually the manager's IP/hostname; the operator may
# also point it at a separate hostname for the AE host they intend to scp
# the cert to later. Either way we need it in the SAN — otherwise the
# manager's wss verify against alarm_engine_url fails with a hostname
# mismatch the moment the URL doesn't match the cert's CN/SAN.
ae_url = os.environ.get("ALARM_ENGINE_URL", "").strip()
ae_host_from_url = urlparse(ae_url).hostname if ae_url else ""

extra_dns_sans = ["localhost"]
extra_ip_sans  = ["127.0.0.1", detected_ip]
if ae_host_from_url:
    try:
        ipaddress.ip_address(ae_host_from_url)
        if ae_host_from_url not in extra_ip_sans:
            extra_ip_sans.append(ae_host_from_url)
    except ValueError:
        if ae_host_from_url not in extra_dns_sans:
            extra_dns_sans.append(ae_host_from_url)

# Manager owns the CA — load_or_create_ca writes data/internal-ca.{crt,key}
# if absent, returns existing otherwise. Idempotent across re-runs.
mgr_data_dir.mkdir(parents=True, exist_ok=True)
ca_cert, ca_key = _pki.load_or_create_ca(mgr_data_dir)
print(f"  CA: {mgr_data_dir / 'internal-ca.crt'}")

# AE server cert. SAN covers localhost + 127.0.0.1 + the detected LAN IP so
# manager → AE and agent → AE both verify cleanly regardless of which name
# they dial. The runtime _ensure_ae_server_cert rotates within 30 days of
# expiry AND re-issues when the SAN no longer covers alarm_engine_url —
# so a later URL change auto-recovers without manual intervention.
ae_data_dir.mkdir(parents=True, exist_ok=True)
crt_pem, key_pem = _pki.sign_agent_cert(
    ca_cert, ca_key,
    agent_id="llm-systems-alarm-engine",
    hostname=socket.gethostname(),
    ip_san=detected_ip,
    extra_dns_sans=extra_dns_sans,
    extra_ip_sans=extra_ip_sans,
)
crt = ae_data_dir / "ae-tls.crt"
key = ae_data_dir / "ae-tls.key"
crt.write_text(crt_pem); os.chmod(crt, 0o644)
key.write_text(key_pem); os.chmod(key,  0o600)
print(f"  AE cert: {crt}")
print(f"  AE key:  {key}")
print(f"  SAN: DNS={extra_dns_sans}  IP={extra_ip_sans}")
PYEOF
)"
    if printf '%s' "$AE_CERT_OUT" | grep -q "AE cert:"; then
      printf '%s\n' "$AE_CERT_OUT" | sed 's/^/  /'
      ok "alarm-engine cert pre-issued (SAN includes $DETECTED_IP + localhost)"
    else
      warn "cert pre-issue failed — manager will retry at first startup"
      printf '%s\n' "$AE_CERT_OUT" | sed 's/^/    /'
    fi
  fi
fi

cat <<EOF

Config bootstrap complete.
  Review and tune (as $USER_ARG):
    sudo -u $USER_ARG \$EDITOR $REAL

  Defaults that ship ON (flip off only if you have a reason to):
    [manager].tls_port      = 5443     # manager HTTPS (internal-CA cert)
    [manager].ws_proxy_port = 5444     # browser → AE WS proxy
    [manager].stream_proxy_port = 5445 # off-pool llama-state SSE daemon
    [alarm_engine].tls_enabled = true  # AE HTTPS (cert pre-issued above)
    [alarm_engine].ingest_token = <set by installer>  # agent push auth

  Split install? Copy the AE TLS cert and key to the AE host.
  The manager issues them on its first startup into its own data dir:
    scp $INSTALL_DIR/data/ae-tls.{crt,key} \\
        <ae-host>:$INSTALL_DIR/llm-systems-alarm-engine/data/
EOF
