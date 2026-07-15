#!/usr/bin/env bash
set -euo pipefail
. /opt/llm-systems-manager/docker/render-config.sh
render_config
exec "$@"
