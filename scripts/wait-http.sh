#!/usr/bin/env bash
# Usage: wait-http.sh URL [TIMEOUT_SECONDS]. Waits until URL returns HTTP 2xx.
set -euo pipefail
url=$1
timeout=${2:-120}
end=$((SECONDS + timeout))
until curl -fs -o /dev/null "$url"; do
  if ((SECONDS >= end)); then
    echo "timeout waiting for $url" >&2
    exit 1
  fi
  sleep 3
done
echo "ready: $url"
