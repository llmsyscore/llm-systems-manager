#!/bin/bash
# Exercises the llama parts of agent/scripts/common.sh against a temp root.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails + 1)); }

export LLMSYS_TEST_ROOT="$T/root"
mkdir -p "$T/root/opt/llm-systems-agent/install" "$T/root/etc/sudoers.d" "$T/root/usr/local/sbin"
A="$T/root/opt/llm-systems-agent"
cp "$REPO/agent/agent_config.yaml.example" "$A/"
for f in detect_llama.py llm-svcconfig-apply.sh.tmpl llm-systems-agent.sudoers.tmpl; do
  cp "$REPO/agent/install/$f" "$A/install/"
done
# shellcheck source=../agent/scripts/common.sh
. "$REPO/tools/packaging/agent/scripts/common.sh"
set +e
# Ownership changes need root; stub them for an unprivileged run.
chown() { :; }
id() { if [ "${1:-}" = -gn ]; then echo testgrp; else command id "$@"; fi; }
if ! command -v visudo >/dev/null 2>&1; then
  visudo() { return 0; }
  echo "note visudo absent: validation stubbed"
fi

write_config_sets_llama_keys() {
  rm -f "$LLMSYS_AGENT_CFG"
  LLMSYS_CFG_LLAMA_ENABLED=true LLMSYS_CFG_LLAMA_API_URL=http://127.0.0.1:9931 \
    LLMSYS_CFG_LLAMA_CONFIG_INI="$T/presets.ini" LLMSYS_CFG_LLAMA_SYSTEMD_UNIT=llama-server.service \
    llmsys_agent_write_config
  grep -q '^LLAMA_ENABLED: true$' "$LLMSYS_AGENT_CFG" \
    && grep -q '^LLAMA_API_URL: "http://127.0.0.1:9931"$' "$LLMSYS_AGENT_CFG" \
    && grep -q "^LLAMA_CONFIG_INI: \"$T/presets.ini\"\$" "$LLMSYS_AGENT_CFG" \
    && grep -q '^LLAMA_SYSTEMD_UNIT: "llama-server.service"$' "$LLMSYS_AGENT_CFG" \
    && grep -q '^# LLAMA_BIN:' "$LLMSYS_AGENT_CFG" || return 1
  rm -f "$LLMSYS_AGENT_CFG"
  llmsys_agent_write_config
  grep -q '^LLAMA_ENABLED: false$' "$LLMSYS_AGENT_CFG" && grep -q '^# LLAMA_API_URL:' "$LLMSYS_AGENT_CFG"
}

install_privileges_renders_and_validates() {
  rm -f "$LLMSYS_SUDOERS_FILE" "$LLMSYS_WRAPPER_FILE"
  LLMSYS_RUN_USER=svcuser LLMSYS_CFG_LLAMA_SYSTEMD_UNIT=llama-server.service llmsys_agent_install_privileges 2>/dev/null
  ! grep -q 'LSA_VLLM' "$LLMSYS_SUDOERS_FILE" && ! grep -v '^#' "$LLMSYS_SUDOERS_FILE" | grep -qi vllm || return 1
  [ -f "$LLMSYS_SUDOERS_FILE" ] && [ -f "$LLMSYS_WRAPPER_FILE" ] || return 1
  grep -q '^svcuser ALL=(root) NOPASSWD' "$LLMSYS_SUDOERS_FILE" \
    && grep -q ' restart llama-server.service' "$LLMSYS_SUDOERS_FILE" \
    && ! grep -q "\${" "$LLMSYS_SUDOERS_FILE" \
    && grep -q "^UNIT_PATH='$LLMSYS_UNIT_ETC/llama-server.service'\$" "$LLMSYS_WRAPPER_FILE" \
    && ! grep -q '__[A-Z_]*__' "$LLMSYS_WRAPPER_FILE" || return 1
  [ "$(stat -c %a "$LLMSYS_SUDOERS_FILE")" = 440 ] && [ "$(stat -c %a "$LLMSYS_WRAPPER_FILE")" = 755 ] || return 1
  if [ "$(command id -u)" = 0 ]; then
    [ "$(stat -c %U "$LLMSYS_WRAPPER_FILE")" = root ] || return 1
  fi
  llmsys_agent_remove_privileges
  [ ! -e "$LLMSYS_SUDOERS_FILE" ] && [ ! -e "$LLMSYS_WRAPPER_FILE" ]
}

install_privileges_defaults_unit_when_unconfigured() {
  rm -f "$LLMSYS_SUDOERS_FILE" "$LLMSYS_WRAPPER_FILE" "$LLMSYS_AGENT_CFG"
  LLMSYS_RUN_USER=svcuser LLMSYS_CFG_LLAMA_SYSTEMD_UNIT='' llmsys_agent_install_privileges 2>/dev/null
  [ -f "$LLMSYS_SUDOERS_FILE" ] && [ -f "$LLMSYS_WRAPPER_FILE" ] || return 1
  grep -q ' restart llama_server.service' "$LLMSYS_SUDOERS_FILE" \
    && grep -q "^UNIT_PATH='$LLMSYS_UNIT_ETC/llama_server.service'\$" "$LLMSYS_WRAPPER_FILE" || return 1
  llmsys_agent_remove_privileges
  [ ! -e "$LLMSYS_SUDOERS_FILE" ] && [ ! -e "$LLMSYS_WRAPPER_FILE" ]
}

stale_dropin_removed_for_missing_user() {
  mkdir -p "$(dirname "$LLMSYS_DROPIN_FILE")"
  printf '# %s.\n[Service]\nUser=nosuchuser-xyz\nGroup=x\n' "$LLMSYS_PRIV_MARKER" > "$LLMSYS_DROPIN_FILE"
  LLMSYS_RUN_USER=llmsys
  llmsys_agent_pick_user
  [ ! -e "$LLMSYS_DROPIN_FILE" ] && [ -z "$LLMSYS_USER_OVERRIDE" ] && [ "$LLMSYS_RUN_USER" = llmsys ]
}

config_unit_wins_over_detected() {
  rm -f "$LLMSYS_SUDOERS_FILE" "$LLMSYS_WRAPPER_FILE"
  printf 'LLAMA_SYSTEMD_UNIT: "mine.service"\n' > "$LLMSYS_AGENT_CFG"
  LLMSYS_RUN_USER=svcuser LLMSYS_CFG_LLAMA_SYSTEMD_UNIT=llama-server.service llmsys_agent_install_privileges 2>/dev/null
  grep -q ' restart mine.service' "$LLMSYS_SUDOERS_FILE" \
    && ! grep -q 'llama-server.service' "$LLMSYS_SUDOERS_FILE" \
    && grep -q "^UNIT_PATH='$LLMSYS_UNIT_ETC/mine.service'\$" "$LLMSYS_WRAPPER_FILE" || return 1
  llmsys_agent_remove_privileges
  rm -f "$LLMSYS_AGENT_CFG"
}

existing_vllm_grant_kept() {
  rm -f "$LLMSYS_SUDOERS_FILE" "$LLMSYS_WRAPPER_FILE" "$LLMSYS_AGENT_CFG"
  # shellcheck disable=SC2016  # literal template placeholders
  llmsys_subst_tokens "$A/install/llm-systems-agent.sudoers.tmpl" \
    '${AGENT_USER}' svcuser '${LLAMA_UNIT}' llama-server.service '${VLLM_UNIT}' vllm.service > "$LLMSYS_SUDOERS_FILE"
  grep -q 'LSA_VLLM' "$LLMSYS_SUDOERS_FILE" || return 1
  LLMSYS_RUN_USER=svcuser LLMSYS_CFG_LLAMA_SYSTEMD_UNIT=llama-server.service llmsys_agent_install_privileges 2>/dev/null
  grep -q '^Cmnd_Alias LSA_VLLM' "$LLMSYS_SUDOERS_FILE" \
    && grep -q 'NOPASSWD: LSA_LLAMA, LSA_PERF, LSA_VLLM$' "$LLMSYS_SUDOERS_FILE" \
    && grep -q ' restart vllm.service' "$LLMSYS_SUDOERS_FILE" || return 1
  llmsys_agent_remove_privileges
}

# Renders the config script as the build does and runs it with a logging
# confmodule shim and a fake python3 that must receive the embedded helper.
debconf_config_embeds_detector() {
  local d="$T/dc"
  mkdir -p "$d/bin"
  "$REPO/tools/packaging/render-debconf-config.sh" "$REPO/tools/packaging/agent/debconf/config" \
    "$REPO/agent/install/detect_llama.py" "$d/config" || return 1
  sh -n "$d/config" || return 1
  grep -q 'LLMSYS_DETECT_EOF' "$d/config" && grep -q 'def ' "$d/config" || return 1
  python3 -I - --sh < "$REPO/agent/install/detect_llama.py" >/dev/null 2>&1 || return 1
  cat > "$d/confmodule" <<'SHIM'
db_input() { echo "input $1 $2" >> "$LOG"; }
db_go() { :; }
db_get() { RET=""; }
db_fget() { RET=false; }
db_set() { echo "set $1 $2" >> "$LOG"; }
db_subst() { :; }
SHIM
  cat > "$d/bin/python3" <<'FAKE'
#!/bin/sh
[ "$2" = - ] && cat > "$LOG.stdin"
printf "LLAMA_DETECTED_FOUND=true\nLLAMA_DETECTED_HOST=0.0.0.0\nLLAMA_DETECTED_PORT=9931\nLLAMA_DETECTED_PRESETS_FILE=/p.ini\nLLAMA_DETECTED_UNIT=llama-server.service\n"
FAKE
  chmod +x "$d/bin/python3"
  python3 - "$d/config" "$d/confmodule" "$A" <<'PY'
import sys
c, m, a = sys.argv[1:4]
t = open(c).read()
t = t.replace("/usr/share/debconf/confmodule", m).replace("/opt/llm-systems-agent", a)
open(c, "w").write(t)
PY
  rm -f "$A/install/detect_llama.py"
  LOG="$d/log" PATH="$d/bin:$PATH" sh "$d/config" || return 1
  grep -q '^set llm-systems-agent/llama-api-url http://127.0.0.1:9931$' "$d/log" \
    && grep -q '^set llm-systems-agent/llama-unit llama-server.service$' "$d/log" \
    && grep -q '^input medium llm-systems-agent/llama-presets-file$' "$d/log" \
    && grep -q 'def ' "$d/log.stdin"
  local rc=$?
  cp "$REPO/agent/install/detect_llama.py" "$A/install/"
  return $rc
}

for t in write_config_sets_llama_keys install_privileges_renders_and_validates \
         install_privileges_defaults_unit_when_unconfigured config_unit_wins_over_detected existing_vllm_grant_kept \
         stale_dropin_removed_for_missing_user \
         debconf_config_embeds_detector; do
  if "$t"; then ok "$t"; else bad "$t"; fi
done
[ "$fails" -eq 0 ]
