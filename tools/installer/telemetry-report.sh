#!/usr/bin/env bash
# telemetry-report.sh --event E --channel C [--mode M] [--outcome O] --data-dir D --root R [--toml T]
# Consent comes from the TOML's [manager.telemetry].install_report; used by deb/rpm postinst.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib-common.sh"
SUDO="${SUDO-}"
EVENT=install; CHANNEL=script; MODE=""; OUTCOME=ok; DATA=""; ROOT=""; TOML=""
while (( $# )); do
  case "$1" in
    --event) EVENT="$2"; shift 2 ;; --channel) CHANNEL="$2"; shift 2 ;; --mode) MODE="$2"; shift 2 ;;
    --outcome) OUTCOME="$2"; shift 2 ;; --data-dir) DATA="$2"; shift 2 ;; --root) ROOT="$2"; shift 2 ;;
    --toml) TOML="$2"; shift 2 ;; *) shift ;;
  esac
done
[[ -n "$DATA" && -n "$ROOT" ]] || exit 0
[[ -n "${CI:-}" ]] && exit 0
consent="$(read_toml_key install_report "$TOML" 2>/dev/null || true)"
[[ "$consent" == "true" ]] && export LLMSYS_TELEMETRY=1 || export LLMSYS_TELEMETRY=0
ver="$(grep -E '^__version__ = ' "$ROOT/llm-systems-manager/backend/llm-systems-manager.py" | head -1 | sed -E 's/.*"(.*)".*/\1/')"
telemetry_report "$EVENT" "$CHANNEL" "$MODE" "$OUTCOME" "$DATA" "${ver:-unknown}" "$ROOT" "$(telemetry_endpoint "$TOML")"
exit 0
