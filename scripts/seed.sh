#!/usr/bin/env bash
# Waits until every fleet device is registered in hawkBit, assigns the initial version's
# distribution set to all of them, and waits for the actions to close (ota-agent reports
# success without reinstalling when the version is already installed).
# Env: PROFILE (lite|full), SEED_VERSION (default fleet initial_version), SEED_TIMEOUT (s).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && set -a && . "$REPO/.env" && set +a

HB=${HAWKBIT_URL:-http://localhost:8080}/rest/v1
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
PROFILE=${PROFILE:-lite}
FLEET="$REPO/sim/fleet/fleet.yaml"
TIMEOUT=${SEED_TIMEOUT:-300}

count=$(awk -v p="$PROFILE:" '$1 == p {f=1; next} f && $1 == "devices:" {print $2; exit}' "$FLEET")
version=${SEED_VERSION:-$(awk '$1 == "initial_version:" {print $2; exit}' "$FLEET")}
[[ -n $count && -n $version ]] || { echo "cannot read profile $PROFILE from $FLEET" >&2; exit 1; }

api() { curl -fsS -u "$AUTH" "$@"; }
uri() { jq -rn --arg v "$1" '$v|@uri'; }

echo "seed: waiting for $count devices (profile $PROFILE) to register"
end=$((SECONDS + TIMEOUT))
while :; do
  registered=$(api "$HB/targets?limit=500&q=$(uri 'controllerId==edge-*')" | jq '.total')
  ((registered >= count)) && break
  ((SECONDS < end)) || { echo "timeout: $registered of $count registered" >&2; exit 1; }
  sleep 5
done

ds=$(api "$HB/distributionsets?q=$(uri "name==wavebreak-app;version==$version")" | jq -er '.content[0].id') \
  || { echo "distribution set wavebreak-app $version not found; run make publish" >&2; exit 1; }

targets=$(api "$HB/targets?limit=500&q=$(uri 'controllerId==edge-*')" | jq -c '[.content[].controllerId]')
body=$(jq -c '[.[] | {id: ., type: "forced"}]' <<<"$targets")
api -X POST -H 'Content-Type: application/json' -d "$body" "$HB/distributionsets/$ds/assignedTargets" \
  | jq -c '{assigned, alreadyAssigned, total}'

echo "seed: waiting for installedDS = $version on all devices"
while :; do
  done_n=0
  for t in $(jq -r '.[]' <<<"$targets"); do
    v=$(api "$HB/targets/$t/installedDS" 2>/dev/null | jq -r '.version // empty' || true)
    [[ $v == "$version" ]] && done_n=$((done_n + 1))
  done
  ((done_n >= count)) && break
  ((SECONDS < end)) || { echo "timeout: $done_n of $count on $version" >&2; exit 1; }
  sleep 5
done
echo "seed: $count devices on $version"
