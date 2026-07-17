#!/usr/bin/env bash
# Plants every removed-paths.manifest entry as stale state in the install dir
# so CI can assert that an upgrade/reinstall prunes it (run as root).
# Usage: ci-plant-stale-fixture.sh <manifest> [install_dir]
set -eu

MANIFEST="${1:?usage: ci-plant-stale-fixture.sh <manifest> [install_dir]}"
INSTALL_DIR="${2:-/opt/llm-systems-manager}"
[ -f "$MANIFEST" ] || { echo "no manifest — skipping fixture"; exit 0; }

planted=0
# shellcheck disable=SC2013  # manifest file paths are whitespace-free by construction
for rel in $(awk -F'|' '$1=="file"{print $3}' "$MANIFEST"); do
  dir="$INSTALL_DIR/$(dirname "$rel")"
  mkdir -p "$dir"
  echo "# stale fixture" > "$INSTALL_DIR/$rel"
  case "$rel" in *.py)
    mkdir -p "$dir/__pycache__"
    touch "$dir/__pycache__/$(basename "$rel" .py).cpython-312.pyc" ;;
  esac
  echo "planted stale file: $rel"
  planted=$((planted+1))
done

TOML="$INSTALL_DIR/config/llm-systems.toml"
# Depth-2 key in its own (currently undefined) section.
if grep -q '^toml-key|.*|manager\.benchmark\.stream_queue_size$' "$MANIFEST"; then
  printf '\n[manager.benchmark]\nstream_queue_size = 5000\n' >> "$TOML"
  echo "planted stale TOML key: manager.benchmark.stream_queue_size"
  planted=$((planted+1))
fi
# Depth-3 key inserted into the existing [alarm_engine.timeouts] section.
if grep -q '^toml-key|.*|alarm_engine\.timeouts\.manager_health$' "$MANIFEST"; then
  sed -i '/^\[alarm_engine\.timeouts\]/a manager_health = 1.5' "$TOML"
  grep -q '^manager_health' "$TOML" || { echo "fixture failed to plant manager_health"; exit 1; }
  echo "planted stale TOML key: alarm_engine.timeouts.manager_health"
  planted=$((planted+1))
fi

[ "$planted" -gt 0 ] || { echo "fixture planted nothing — manifest/fixture drift"; exit 1; }
echo "planted $planted stale artifact(s)"
