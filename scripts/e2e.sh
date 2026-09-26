#!/usr/bin/env bash
# Exercise the real runtime fault on one rev-B field device, then recover it.
# Requires `make up PROFILE=lite`, `make fleet PROFILE=lite`, and `make publish`.
# The fleet is removed on exit; backend services stay available for inspection.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && set -a && . "$REPO/.env" && set +a

HB=${HAWKBIT_URL:-http://localhost:8080}/rest/v1
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
PROM=${PROMETHEUS_URL:-http://localhost:9090}
DEVICE=${E2E_DEVICE:-edge-001}
TIMEOUT=${E2E_TIMEOUT:-420}
FLEET_STARTED=0

cleanup() {
  if (( FLEET_STARTED )); then
    make -C "$REPO" fleet-down PROFILE=lite RUNTIME=container >/dev/null || true
  fi
}
trap cleanup EXIT

api() { curl -fsS -u "$AUTH" "$@"; }
uri() { jq -rn --arg v "$1" '$v|@uri'; }
query() {
  curl -fsSG "$PROM/api/v1/query" --data-urlencode "query=$1" \
    | jq -r '[.data.result[].value[1] | tonumber] | max // 0'
}
wait_version() {
  local wanted=$1 deadline=$((SECONDS + TIMEOUT)) version
  while (( SECONDS < deadline )); do
    version=$(api "$HB/targets/$DEVICE/installedDS" | jq -r '.version // empty' || true)
    if [[ $version == "$wanted" ]]; then return 0; fi
    sleep 5
  done
  echo "timeout: $DEVICE did not install $wanted (last=$version)" >&2
  return 1
}
wait_metric() {
  local promql=$1 predicate=$2 deadline=$((SECONDS + TIMEOUT)) value
  while (( SECONDS < deadline )); do
    value=$(query "$promql" || echo 0)
    if awk -v value="$value" -v predicate="$predicate" 'BEGIN { exit !(value > predicate) }'; then
      echo "$value"
      return 0
    fi
    sleep 5
  done
  echo "timeout: metric did not exceed $predicate: $promql (last=$value)" >&2
  return 1
}
wait_metric_zero() {
  local promql=$1 deadline=$((SECONDS + TIMEOUT)) value
  while (( SECONDS < deadline )); do
    value=$(query "$promql" || echo 0)
    if awk -v value="$value" 'BEGIN { exit !(value == 0) }'; then return 0; fi
    sleep 5
  done
  echo "timeout: metric did not reset to zero after install: $promql (last=$value)" >&2
  return 1
}

if ! docker inspect "$DEVICE" >/dev/null 2>&1; then
  echo "fleet device $DEVICE is missing; run make fleet PROFILE=lite first" >&2
  exit 1
fi
if [[ $(docker inspect -f '{{.State.Running}}' "$DEVICE") != true ]]; then
  docker start "$DEVICE" >/dev/null
fi
FLEET_STARTED=1

target=$(api "$HB/targets/$DEVICE/attributes")
hw_rev=$(jq -r '.hw_rev // empty' <<<"$target")
[[ $hw_rev == B ]] || { echo "$DEVICE must be hardware revision B (found ${hw_rev:-unknown})" >&2; exit 1; }

ds_id() {
  api "$HB/distributionsets?q=$(uri "name==wavebreak-app;version==$1")" \
    | jq -er --arg v "$1" '.content[] | select(.version == $v) | .id' | head -n1
}
assign() {
  local version=$1 id body
  id=$(ds_id "$version")
  body=$(jq -nc --arg id "$DEVICE" '[{id:$id,type:"forced"}]')
  api -X POST -H 'Content-Type: application/json' -d "$body" \
    "$HB/distributionsets/$id/assignedTargets" >/dev/null
  wait_version "$version"
  echo "$DEVICE installed $version"
}

assign v1.2
restart_metric="wavebreak_app_restarts_total{device_id=\"$DEVICE\",fw_version=\"v1.2\"}"
wait_metric_zero "$restart_metric"
baseline=$(query "$restart_metric")
echo "$DEVICE v1.2 restart baseline: $baseline"
echo "waiting for real v1.2 rev-B memory fault and systemd restart"
restarts=$(wait_metric "$restart_metric" "$baseline")
echo "$DEVICE restart count reached $restarts"

assign v1.1
wait_metric "wavebreak_app_frames_total{device_id=\"$DEVICE\",fw_version=\"v1.1\"}" 0 >/dev/null
docker exec "$DEVICE" systemctl is-active --quiet inference-app
echo "$DEVICE recovered on v1.1; tearing down lite fleet"
