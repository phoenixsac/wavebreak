#!/usr/bin/env bash
# Create/rotate a Viewer service-account token and restart mcp-grafana with it.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
[[ -f .env ]] || { cp .env.example .env; chmod 600 .env; echo "Created .env from .env.example"; }
set -a
# shellcheck disable=SC1091
. ./.env
set +a

GRAFANA=${GRAFANA_URL:-http://localhost:3000}
ADMIN="${GRAFANA_ADMIN_USER:-admin}:${GRAFANA_ADMIN_PASSWORD:-admin}"
ACCOUNT_NAME=wavebreak-mcp-readonly
TOKEN_NAME="mcp-grafana-$(date +%s)"
api() { curl --max-time 10 -fsS -u "$ADMIN" "$@"; }

./scripts/wait-http.sh "$GRAFANA/api/health" 120 >/dev/null
accounts=$(api "$GRAFANA/api/serviceaccounts/search?query=$ACCOUNT_NAME&perpage=100")
account_id=$(jq -r --arg name "$ACCOUNT_NAME" \
  '.serviceAccounts[]? | select(.name == $name) | .id' <<<"$accounts" | head -1)
if [[ -z $account_id ]]; then
  account=$(api -X POST -H 'Content-Type: application/json' \
    -d "{\"name\":\"$ACCOUNT_NAME\",\"role\":\"Viewer\",\"isDisabled\":false}" \
    "$GRAFANA/api/serviceaccounts")
  account_id=$(jq -er '.id' <<<"$account")
else
  role=$(jq -r --argjson id "$account_id" \
    '.serviceAccounts[] | select(.id == $id) | .role' <<<"$accounts")
  [[ $role == Viewer ]] || {
    echo "Grafana service account $ACCOUNT_NAME has role $role; set it to Viewer before continuing" >&2
    exit 1
  }
fi

old_tokens=$(api "$GRAFANA/api/serviceaccounts/$account_id/tokens")
created=$(api -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"$TOKEN_NAME\",\"secondsToLive\":0}" \
  "$GRAFANA/api/serviceaccounts/$account_id/tokens")
token=$(jq -er '.key' <<<"$created")
token_id=$(jq -er '.id' <<<"$created")

# Update .env atomically, keeping the token out of command-line arguments and logs.
printf '%s' "$token" | python3 -c '
import os, pathlib, sys, tempfile
path = pathlib.Path(".env")
token = sys.stdin.read().strip()
lines = path.read_text().splitlines() if path.exists() else []
lines = [line for line in lines if not line.startswith("GRAFANA_SA_TOKEN=")]
lines.append("GRAFANA_SA_TOKEN=" + token)
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=".")
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
'

# Revoke tokens from earlier runs only after the replacement is safely stored.
jq -r --argjson keep "$token_id" '.[]? | select(.id != $keep) | .id' <<<"$old_tokens" |
while read -r old_id; do
  [[ -n $old_id ]] || continue
  api -X DELETE "$GRAFANA/api/serviceaccounts/$account_id/tokens/$old_id" >/dev/null
 done

COMPOSE=(docker compose --env-file .env -f platform/docker-compose.yml --profile mcp)
"${COMPOSE[@]}" up -d --force-recreate mcp-grafana >/dev/null
./scripts/wait-http.sh "$GRAFANA/api/health" 60 >/dev/null
# Authenticate as the minted Viewer token to verify it works before returning.
curl --max-time 10 -fsS -H "Authorization: Bearer $token" "$GRAFANA/api/org" >/dev/null
if ! "${COMPOSE[@]}" ps --status running --services | grep -qx mcp-grafana; then
  echo "mcp-grafana did not remain running" >&2
  exit 1
fi
echo "Grafana Viewer token stored in .env and mcp-grafana restarted (account id $account_id)."
