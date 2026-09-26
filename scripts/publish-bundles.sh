#!/usr/bin/env bash
# Publishes build/bundles/*.tar to hawkBit via the Management API, idempotently:
# one software module (type "application") + one distribution set (type "app") per version,
# both named "wavebreak-app". Types are discovered at runtime by key.
# Env (from .env): HAWKBIT_URL, HAWKBIT_USERNAME, HAWKBIT_PASSWORD.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck disable=SC1091
[[ -f "$REPO/.env" ]] && set -a && . "$REPO/.env" && set +a

HB=${HAWKBIT_URL:-http://localhost:8080}/rest/v1
AUTH="${HAWKBIT_USERNAME:-admin}:${HAWKBIT_PASSWORD:-admin}"
NAME=wavebreak-app
SM_TYPE_KEY=${SM_TYPE_KEY:-application}
DS_TYPE_KEY=${DS_TYPE_KEY:-app}
BUNDLES="$REPO/build/bundles"

api() {  # api METHOD PATH [curl args...]
  local method=$1 path=$2
  shift 2
  curl -fsS -u "$AUTH" -X "$method" "$HB$path" "$@"
}
json() { api "$1" "$2" -H 'Content-Type: application/json' -d "$3"; }
q() { jq -rn --arg v "$1" '$v|@uri'; }

type_key() {  # type_key <collection> <key>: fails unless the type exists (POST bodies take the key)
  api GET "/$1?limit=100" | jq -er --arg k "$2" '.content[] | select(.key == $k) | .key'
}

sm_type=$(type_key softwaremoduletypes "$SM_TYPE_KEY")
ds_type=$(type_key distributionsettypes "$DS_TYPE_KEY")
echo "types: softwaremodule $sm_type, distributionset $ds_type"

shopt -s nullglob
tarballs=("$BUNDLES"/"$NAME"-v*.tar)
((${#tarballs[@]})) || { echo "no bundles in $BUNDLES; run scripts/build-bundles.sh" >&2; exit 1; }

for tarball in "${tarballs[@]}"; do
  file=$(basename "$tarball")
  version=${file#"$NAME"-}
  version=${version%.tar}
  sha=$(cut -d' ' -f1 "$tarball.sha256")
  desc=$(tar -xOf "$tarball" manifest.json | jq -r '.description // ""')

  sm_id=$(api GET "/softwaremodules?q=$(q "name==$NAME;version==$version")" | jq -r '.content[0].id // empty')
  if [[ -z $sm_id ]]; then
    sm_id=$(json POST /softwaremodules "$(jq -nc --arg n "$NAME" --arg v "$version" --arg d "$desc" \
      --arg t "$sm_type" '[{name: $n, version: $v, type: $t, description: $d, vendor: "Wavebreak"}]')" \
      | jq -r '.[0].id')
    echo "$version: created software module $sm_id"
  fi

  have_sha=$(api GET "/softwaremodules/$sm_id/artifacts" | jq -r --arg f "$file" '.[] | select(.providedFilename == $f) | .hashes.sha256')
  if [[ -z $have_sha ]]; then
    api POST "/softwaremodules/$sm_id/artifacts" -F "file=@$tarball" -F "filename=$file" >/dev/null
    echo "$version: uploaded $file"
  elif [[ $have_sha != "$sha" ]]; then
    echo "$version: artifact $file already published with a different sha256 ($have_sha); bump the version" >&2
    exit 1
  fi

  ds_id=$(api GET "/distributionsets?q=$(q "name==$NAME;version==$version")" | jq -r '.content[0].id // empty')
  if [[ -z $ds_id ]]; then
    ds_id=$(json POST /distributionsets "$(jq -nc --arg n "$NAME" --arg v "$version" --arg d "$desc" \
      --arg t "$ds_type" --argjson sm "$sm_id" \
      '[{name: $n, version: $v, type: $t, description: $d, modules: [{id: $sm}]}]')" | jq -r '.[0].id')
    echo "$version: created distribution set $ds_id"
  fi
  echo "$version: sm=$sm_id ds=$ds_id sha256=${sha:0:16}…"
done
