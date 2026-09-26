#!/usr/bin/env bash
# Return the selected container profile to healthy v1.1 devices and an empty lab.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
[[ -f .env ]] || { cp .env.example .env; echo "Created .env from .env.example"; }
REQUESTED_PROFILE=${PROFILE:-}
set -a
# shellcheck disable=SC1091
. ./.env
set +a

PROFILE=${REQUESTED_PROFILE:-${PROFILE:-lite}}
[[ $PROFILE == lite || $PROFILE == full ]] || {
  echo "demo-reset: unknown PROFILE=$PROFILE (choose lite or full)" >&2
  exit 1
}
START=$SECONDS
TIMEOUT=${DEMO_RESET_TIMEOUT:-175}
HB=${HAWKBIT_URL:-http://localhost:8080}/rest/v1
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
LAB_URL="http://localhost:${LAB_CONTROLLER_PORT:-8090}"
LAB_TOKEN=${LAB_API_TOKEN:-change-me-lab-token}
PROM=${PROMETHEUS_URL:-http://localhost:9090}

fail() { echo "demo-reset: $*" >&2; exit 1; }
check_time() { (( SECONDS - START < TIMEOUT )) || fail "exceeded ${TIMEOUT}s deadline"; }
api() { curl --max-time 8 -fsS -u "$AUTH" "$@"; }
lab_api() { curl --max-time 8 -fsS -H "Authorization: Bearer $LAB_TOKEN" "$@"; }
uri() { jq -rn --arg v "$1" '$v|@uri'; }

read -r DEVICE_COUNT REV_B_SHARE < <("${PYTHON:-${PY:-python3}}" - "$PROFILE" <<'PY'
import sys
import yaml

fleet = yaml.safe_load(open("sim/fleet/fleet.yaml", encoding="utf-8"))
profile = sys.argv[1]
if profile not in fleet["profiles"]:
    raise SystemExit(f"unknown fleet profile: {profile}")
print(int(fleet["profiles"][profile]["devices"]), float(fleet["defaults"]["hw_rev_mix"]["B"]))
PY
)
EXPECTED_REV_B=$(awk -v n="$DEVICE_COUNT" -v share="$REV_B_SHARE" 'BEGIN {print int(n * share + 0.999999)}')
EXPECTED_REV_A=$((DEVICE_COUNT - EXPECTED_REV_B))

echo "demo-reset: ensuring $PROFILE backend and fleet ($DEVICE_COUNT devices)"
make up PROFILE="$PROFILE" RUNTIME=container >/dev/null
make fleet PROFILE="$PROFILE" RUNTIME=container >/dev/null
make lab PROFILE="$PROFILE" >/dev/null
check_time

echo "demo-reset: clearing lab devices"
lab_api "$LAB_URL/lab/devices" | jq -r '.devices[].id' | while read -r id; do
  [[ -n $id ]] && lab_api -X DELETE "$LAB_URL/lab/devices/$id" >/dev/null
done

echo "demo-reset: stopping active hawkBit rollouts"
api "$HB/rollouts?limit=500" | jq -r '.content[]? | select(.status == "ready" or .status == "running" or .status == "paused" or .status == "creating" or .status == "scheduled" or .status == "approved") | .id' |
while read -r id; do
  [[ -n $id ]] || continue
  curl --max-time 8 -fsS -u "$AUTH" -X POST "$HB/rollouts/$id/stop" >/dev/null \
    || fail "could not stop active rollout $id"
done

echo "demo-reset: cancelling active hawkBit actions"
targets=$(api "$HB/targets?limit=500" | jq -r '.content[].controllerId')
while read -r target; do
  [[ -n $target ]] || continue
  api "$HB/targets/$(uri "$target")/actions?limit=500" |
    jq -r '.content[]? | select(.active == true) | .id' |
    while read -r action; do
      [[ -n $action ]] || continue
      curl --max-time 8 -fsS -u "$AUTH" -X DELETE \
        "$HB/targets/$(uri "$target")/actions/$action" >/dev/null \
        || fail "could not cancel active action $action for $target"
    done
done <<<"$targets"

echo "demo-reset: assigning v1.1 and waiting for device reports"
ds=$(api "$HB/distributionsets?q=$(uri 'name==wavebreak-app;version==v1.1')&limit=50" |
  jq -er '[.content[] | select(.name == "wavebreak-app" and .version == "v1.1")][0].id') \
  || fail "hawkBit distribution set wavebreak-app v1.1 is missing; run make publish"
target_json=$(api "$HB/targets?limit=500&q=$(uri 'controllerId==edge-*')")
[[ $(jq '.content | length' <<<"$target_json") == "$DEVICE_COUNT" ]] \
  || fail "expected $DEVICE_COUNT $PROFILE hawkBit targets"
targets=$(jq -r '.content[].controllerId' <<<"$target_json")
body=$(jq -cn --argjson ts "$(jq '[.content[].controllerId]' <<<"$target_json")" \
  '$ts | map({id: ., type: "forced"})')
api -X POST -H 'Content-Type: application/json' -d "$body" \
  "$HB/distributionsets/$ds/assignedTargets" >/dev/null

while :; do
  check_time
  installed=0
  while read -r target; do
    version=$(api "$HB/targets/$(uri "$target")/installedDS" 2>/dev/null |
      jq -r '.version // empty' || true)
    [[ $version == v1.1 ]] && installed=$((installed + 1))
  done <<<"$targets"
  (( installed == DEVICE_COUNT )) && break
  sleep 3
done

# The OTA agent accepts a release only after its 15s health window. Confirm the
# current app process is still active and current-version telemetry is live.
prom_value() {
  curl --max-time 5 -fsSG --data-urlencode "query=$1" "$PROM/api/v1/query" |
    jq -r '.data.result[0].value[1] // empty'
}
while :; do
  check_time
  healthy=1
  while read -r target; do
    systemd=$(docker exec "$target" systemctl is-active inference-app 2>/dev/null || true)
    frames=$(prom_value "wavebreak_app_frames_total{device_id=\"$target\",fw_version=\"v1.1\"}" || true)
    fps=$(prom_value "wavebreak_app_fps{device_id=\"$target\",fw_version=\"v1.1\"}" || true)
    restarts=$(prom_value "wavebreak_app_restarts_total{device_id=\"$target\",fw_version=\"v1.1\"}" || true)
    if [[ $systemd != active || -z $frames || -z $fps || -z $restarts || \
          $(awk -v f="$frames" -v p="$fps" -v r="$restarts" \
            'BEGIN{print (f+0)<=0 || (p+0)<=0 || (r+0)>0}') == 1 ]]; then
      healthy=0
    fi
  done <<<"$targets"
  (( healthy )) && break
  sleep 3
done

revision_counts=$(curl --max-time 5 -fsSG --data-urlencode \
  'query=count by (hw_rev) (wavebreak_app_fps{device_id=~"edge-.*",fw_version="v1.1"})' \
  "$PROM/api/v1/query" | jq -c '[.data.result[] | {revision: .metric.hw_rev, count: (.value[1] | tonumber)}]')
jq -e --argjson a "$EXPECTED_REV_A" --argjson b "$EXPECTED_REV_B" \
  '(. | length) == 2 and any(.[]; .revision == "A" and .count == $a) and
  any(.[]; .revision == "B" and .count == $b)' <<<"$revision_counts" >/dev/null \
  || fail "expected $EXPECTED_REV_A rev-A and $EXPECTED_REV_B rev-B v1.1 devices; got $revision_counts"

elapsed=$((SECONDS - START))
echo "demo-reset: ready; $DEVICE_COUNT $PROFILE devices healthy on v1.1 ($EXPECTED_REV_A rev A, $EXPECTED_REV_B rev B), lab empty (${elapsed}s)"
