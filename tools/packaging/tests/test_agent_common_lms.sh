#!/bin/bash
# Exercises the LM Studio parts of agent/scripts/common.sh against a temp root.
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
# shellcheck source=../agent/scripts/common.sh
. "$REPO/tools/packaging/agent/scripts/common.sh"
set +e
# Ownership changes need root; stub them for an unprivileged run.
chown() { :; }
id() { if [ "${1:-}" = -gn ]; then echo testgrp; else command id "$@"; fi; }

# Fake passwd lookups for ana and bob.
getent() {
  [ "$1" = passwd ] || return 2
  case "$2" in
    ana) echo 'ana:x:2001:2001::/home/ana:/bin/sh' ;;
    bob) echo 'bob:x:2002:2002::/home/bob:/bin/sh' ;;
    *) return 2 ;;
  esac
}

# Writes a fake detect_lms.py that prints the given LMS_DETECTED_* lines.
fake_detector() {
  printf 'print(%s)\n' "$(python3 -c 'import sys; print(repr(sys.argv[1]))' "$1")" > "$A/install/detect_lms.py"
}

write_config_sets_lms_keys() {
  rm -f "$LLMSYS_AGENT_CFG"
  LLMSYS_CFG_LMS_ENABLED=true LLMSYS_CFG_LMS_API_URL=http://127.0.0.1:1240 \
    LLMSYS_CFG_LMS_CMD=/home/ana/.lmstudio/bin/lms llmsys_agent_write_config
  grep -q '^LMS_ENABLED: true$' "$LLMSYS_AGENT_CFG" \
    && grep -q '^LMS_API_URL: "http://127.0.0.1:1240"$' "$LLMSYS_AGENT_CFG" \
    && grep -q '^LMS_CMD: "/home/ana/.lmstudio/bin/lms"$' "$LLMSYS_AGENT_CFG" \
    && grep -q '^# LMS_LOAD_TIMEOUT_S:' "$LLMSYS_AGENT_CFG" || return 1
  rm -f "$LLMSYS_AGENT_CFG"
  llmsys_agent_write_config
  grep -q '^LMS_ENABLED: false$' "$LLMSYS_AGENT_CFG" && grep -q '^# LMS_API_URL:' "$LLMSYS_AGENT_CFG" \
    && grep -q '^# LMS_CMD:' "$LLMSYS_AGENT_CFG"
}

detect_lms_keeps_llama_user() {
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=ana
LMS_DETECTED_PORT=1240
LMS_DETECTED_API_URL=http://127.0.0.1:1240
LMS_DETECTED_LMS_CMD=/home/ana/.lmstudio/bin/lms
LMS_DETECTED_LMS_USER=ana"
  LLMSYS_CFG_LMS_ENABLED="" LLMSYS_CFG_LMS_API_URL="" LLMSYS_CFG_LMS_CMD=""
  LLMSYS_RUN_USER_DEFAULT=llama
  llmsys_agent_detect_lms
  [ "$LLMSYS_RUN_USER_DEFAULT" = llama ] && [ "$LLMSYS_CFG_LMS_ENABLED" = true ] \
    && [ "$LLMSYS_CFG_LMS_API_URL" = http://127.0.0.1:1240 ] \
    && [ "$LLMSYS_CFG_LMS_CMD" = /home/ana/.lmstudio/bin/lms ]
}

detect_lms_sets_user_when_llama_none() {
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=''
LMS_DETECTED_PORT=''
LMS_DETECTED_API_URL=''
LMS_DETECTED_LMS_CMD=/home/ana/.lmstudio/bin/lms
LMS_DETECTED_LMS_USER=ana"
  LLMSYS_CFG_LMS_ENABLED="" LLMSYS_CFG_LMS_API_URL="" LLMSYS_CFG_LMS_CMD=""
  LLMSYS_RUN_USER_DEFAULT=""
  llmsys_agent_detect_lms
  [ "$LLMSYS_RUN_USER_DEFAULT" = ana ] && [ "$LLMSYS_CFG_LMS_ENABLED" = true ] \
    && [ -z "$LLMSYS_CFG_LMS_API_URL" ] && [ "$LLMSYS_CFG_LMS_CMD" = /home/ana/.lmstudio/bin/lms ] || return 1
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=root
LMS_DETECTED_LMS_CMD=/usr/local/bin/lms
LMS_DETECTED_LMS_USER=root"
  LLMSYS_RUN_USER_DEFAULT=""
  llmsys_agent_detect_lms
  [ -z "$LLMSYS_RUN_USER_DEFAULT" ] || return 1
  fake_detector "LMS_DETECTED_FOUND=false"
  LLMSYS_CFG_LMS_ENABLED="" LLMSYS_CFG_LMS_API_URL="" LLMSYS_CFG_LMS_CMD=""
  llmsys_agent_detect_lms
  [ -z "$LLMSYS_CFG_LMS_ENABLED" ] && [ -z "$LLMSYS_RUN_USER_DEFAULT" ]
}

detect_lms_used_when_llama_user_is_root() {
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=ana
LMS_DETECTED_LMS_CMD=/home/ana/.lmstudio/bin/lms
LMS_DETECTED_LMS_USER=ana"
  LLMSYS_CFG_LMS_ENABLED="" LLMSYS_CFG_LMS_API_URL="" LLMSYS_CFG_LMS_CMD=""
  LLMSYS_RUN_USER_DEFAULT=root
  llmsys_agent_detect_lms
  [ "$LLMSYS_RUN_USER_DEFAULT" = ana ] || return 1
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=root
LMS_DETECTED_LMS_CMD=/usr/local/bin/lms
LMS_DETECTED_LMS_USER=root"
  LLMSYS_RUN_USER_DEFAULT=root
  llmsys_agent_detect_lms
  [ "$LLMSYS_RUN_USER_DEFAULT" = root ]
}

lms_user_fallback_requires_own_home() {
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=''
LMS_DETECTED_LMS_CMD=/srv/shared/lms
LMS_DETECTED_LMS_USER=ana"
  LLMSYS_CFG_LMS_ENABLED="" LLMSYS_CFG_LMS_API_URL="" LLMSYS_CFG_LMS_CMD=""
  LLMSYS_RUN_USER_DEFAULT=""
  llmsys_agent_detect_lms
  [ -z "$LLMSYS_RUN_USER_DEFAULT" ] || return 1
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=''
LMS_DETECTED_LMS_CMD=/home/bob/.lmstudio/bin/lms
LMS_DETECTED_LMS_USER=ana"
  LLMSYS_CFG_LMS_CMD=""
  llmsys_agent_detect_lms
  [ -z "$LLMSYS_RUN_USER_DEFAULT" ] || return 1
  fake_detector "LMS_DETECTED_FOUND=true
LMS_DETECTED_PROCESS_USER=''
LMS_DETECTED_LMS_CMD=/home/bob/.lmstudio/bin/lms
LMS_DETECTED_LMS_USER=bob"
  LLMSYS_CFG_LMS_CMD=""
  llmsys_agent_detect_lms
  [ "$LLMSYS_RUN_USER_DEFAULT" = bob ]
}

for t in write_config_sets_lms_keys detect_lms_keeps_llama_user detect_lms_sets_user_when_llama_none \
         detect_lms_used_when_llama_user_is_root lms_user_fallback_requires_own_home; do
  if "$t"; then ok "$t"; else bad "$t"; fi
done
[ "$fails" -eq 0 ]
