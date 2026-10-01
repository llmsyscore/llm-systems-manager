#!/usr/bin/env bash
set -euo pipefail
. /opt/llm-systems-manager/docker/render-config.sh
render_config

# Says how to create the admin's temporary password while no admin can sign in.
admin_tool=/opt/llm-systems-manager/llm-systems-manager/backend/admin_password.py
case "$(python3 "$admin_tool" status 2>/dev/null || true)" in
  empty|reset-required)
    echo "[entrypoint] No admin password is set. Create a temporary one with:"
    echo "[entrypoint]   docker compose exec manager python3 backend/admin_password.py reset"
    ;;
esac

exec "$@"
