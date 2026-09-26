#!/usr/bin/env bash
# Idempotent hawkBit tenant bootstrap: DDI auth mode, gateway token, polling interval.
# Env (from .env): HAWKBIT_URL, HAWKBIT_USERNAME, HAWKBIT_PASSWORD, HAWKBIT_DDI_AUTH (gateway|target),
#                  HAWKBIT_GATEWAY_TOKEN, HAWKBIT_POLLING_TIME (HH:MM:SS)
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && set -a && . "$REPO/.env" && set +a

HAWKBIT_URL=${HAWKBIT_URL:-http://localhost:8080}
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
DDI_AUTH=${HAWKBIT_DDI_AUTH:-gateway}
TOKEN=${HAWKBIT_GATEWAY_TOKEN:?set HAWKBIT_GATEWAY_TOKEN in .env}
POLL=${HAWKBIT_POLLING_TIME:-00:00:10}

put_config() {
  local key=$1 value=$2
  curl -fsS -u "$AUTH" -X PUT -H 'Content-Type: application/json' \
    -d "{\"value\": $value}" "$HAWKBIT_URL/rest/v1/system/configs/$key" >/dev/null
  echo "set $key"
}

if [[ $DDI_AUTH == gateway ]]; then
  put_config authentication.gatewaytoken.enabled true
  put_config authentication.gatewaytoken.key "\"$TOKEN\""
  put_config authentication.targettoken.enabled false
else
  put_config authentication.targettoken.enabled true
  put_config authentication.gatewaytoken.enabled false
fi
put_config pollingTime "\"$POLL\""
