#!/usr/bin/env bash
# Summarize live fleet, hawkBit work, lab devices, health, and operator URLs.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
[[ -f .env ]] || { echo "missing .env; run make up first" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
. ./.env
set +a

HB=${HAWKBIT_URL:-http://localhost:8080}/rest/v1
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
PROM=${PROMETHEUS_URL:-http://localhost:9090}
LAB_URL=${LAB_CONTROLLER_URL:-http://localhost:${LAB_CONTROLLER_PORT:-8090}}
LAB_TOKEN=${LAB_API_TOKEN:-change-me-lab-token}
api() { curl --max-time 5 -fsS -u "$AUTH" "$@"; }
uri() { jq -rn --arg v "$1" '$v|@uri'; }
prom_value() {
  curl --max-time 3 -fsSG --data-urlencode "query=max($1)" "$PROM/api/v1/query" 2>/dev/null |
    jq -r '.data.result[0].value[1] // empty' 2>/dev/null || true
}

printf 'Fleet versions by hardware revision\n'
fleet_ids=$(docker ps -a --filter label=com.wavebreak.fleet=true --format '{{.ID}}' | sort)
fleet_rows=''
while read -r container; do
  [[ -n $container ]] || continue
  env=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container")
  device=$(awk -F= '$1 == "DEVICE_ID" {print $2; exit}' <<<"$env")
  rev=$(awk -F= '$1 == "HW_REV" {print $2; exit}' <<<"$env")
  [[ -n $device ]] || device=$(docker inspect -f '{{.Name}}' "$container" | sed 's#^/##')
  running=$(docker inspect -f '{{.State.Running}}' "$container")
  installed=$(api "$HB/targets/$(uri "$device")/installedDS" 2>/dev/null |
    jq -r '.version // empty' 2>/dev/null || true)
  [[ -n $installed ]] || installed=unknown
  health=unhealthy
  if [[ $running == true ]] && docker exec "$device" systemctl is-active --quiet inference-app 2>/dev/null; then
    frames=$(prom_value "wavebreak_app_frames_total{device_id=\"$device\",fw_version=\"$installed\"}")
    fps=$(prom_value "wavebreak_app_fps{device_id=\"$device\",fw_version=\"$installed\"}")
    restarts=$(prom_value "wavebreak_app_restarts_total{device_id=\"$device\",fw_version=\"$installed\"}")
    if [[ -n $frames && -n $fps && -n $restarts ]] && \
       jq -e -n --arg f "$frames" --arg p "$fps" --arg r "$restarts" \
         '($f|tonumber)>0 and ($p|tonumber)>0 and ($r|tonumber)==0' >/dev/null 2>&1; then
      health=healthy
    fi
  fi
  fleet_rows+="${device}\t${rev:-?}\t${installed}\t${health}\n"
done <<<"$fleet_ids"
if [[ -z $fleet_ids ]]; then
  echo '  no fleet devices'
else
  for rev in A B; do
    versions=$(awk -F '\t' -v r="$rev" '$2 == r {printf "%s%s=%s", sep, $1, $3; sep=" "}' <<<"$(printf '%b' "$fleet_rows")")
    echo "  rev $rev: ${versions:-none}"
  done
  printf '  %-12s %-8s %-10s %s\n' DEVICE HW_REV VERSION HEALTH
  printf '%b' "$fleet_rows" | while IFS=$'\t' read -r device rev version health; do
    [[ -n $device ]] && printf '  %-12s %-8s %-10s %s\n' "$device" "$rev" "$version" "$health"
  done
fi

printf '\nActive hawkBit rollouts and actions\n'
if rollouts=$(api "$HB/rollouts?limit=500" 2>/dev/null); then
  active=$(jq -r '.content[]? | select(.status == "ready" or .status == "running" or .status == "paused" or .status == "creating" or .status == "scheduled" or .status == "approved") | "  rollout \(.id) \(.name) [\(.status)]"' <<<"$rollouts")
  printf '%s\n' "${active:-  none}"
else
  echo '  hawkBit unavailable'
fi

printf '\nLab devices\n'
if labs=$(curl --max-time 5 -fsS -H "Authorization: Bearer $LAB_TOKEN" "$LAB_URL/lab/devices" 2>/dev/null); then
  jq -r 'if (.devices|length)==0 then "  none" else .devices[] | "  \(.id) hw_rev=\(.hw_rev) state=\(.state)" end' <<<"$labs"
else
  echo '  lab controller unavailable'
fi

printf '\nService URLs\n'
printf '  hawkBit API:       %s\n' "${HAWKBIT_URL:-http://localhost:8080}"
printf '  hawkBit UI:        http://localhost:%s\n' "${HAWKBIT_UI_PORT:-8081}"
printf '  Grafana:           %s\n' "${GRAFANA_URL:-http://localhost:3000}"
printf '  Prometheus:        %s\n' "$PROM"
printf '  Loki:              %s\n' "${LOKI_URL:-http://localhost:3100}"
printf '  mcp-grafana:       %s\n' "${MCP_GRAFANA_URL:-http://localhost:8000/mcp}"
printf '  hawkBit MCP:       %s\n' "${HAWKBIT_MCP_URL:-http://localhost:8082/mcp}"
printf '  lab controller:    %s\n' "$LAB_URL"
