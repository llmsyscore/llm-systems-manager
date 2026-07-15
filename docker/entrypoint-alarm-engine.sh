#!/usr/bin/env bash
set -euo pipefail
. /opt/llm-systems-manager/docker/render-config.sh
render_config

# The manager issues data/ae-tls.{crt,key} into the shared ae-data volume at
# its startup. Wait briefly so first boot serves HTTPS; fail-open to HTTP after.
CERT=/opt/llm-systems-manager/llm-systems-alarm-engine/data/ae-tls.crt
if [ "${LSM_AE_TLS_ENABLED:-true}" = "true" ] && [ ! -s "$CERT" ]; then
  wait_s="${LSM_AE_TLS_WAIT_S:-90}"
  echo "[entrypoint] waiting up to ${wait_s}s for $CERT (issued by the manager)"
  for _ in $(seq 1 "$wait_s"); do
    [ -s "$CERT" ] && break
    sleep 1
  done
  if [ -s "$CERT" ]; then
    echo "[entrypoint] TLS cert present — serving HTTPS"
  else
    echo "[entrypoint] cert not issued in ${wait_s}s — starting anyway (falls back to HTTP; restart once the manager is up to enable TLS)"
  fi
fi

exec "$@"
