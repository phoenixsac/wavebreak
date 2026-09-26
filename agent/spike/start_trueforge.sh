#!/usr/bin/env bash
# Start TrueForge local mode in the background (logs to agent/spike/trueforge.log).
# OUTBOUND_URL_ALLOWED_HOSTS: TrueForge's SSRF guard blocks loopback MCP URLs otherwise.
# MCP_REQUEST_TIMEOUT_MS: per-tool-call timeout (default 240000).
# Refuses to start a second copy when the port is taken.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8790}"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "port $PORT already in use; TrueForge (or something else) is running. Not starting." >&2
  exit 1
fi
mkdir -p ../../run
# shellcheck disable=SC1090
source ~/.nvm/nvm.sh >/dev/null 2>&1 || true
OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' \
MCP_REQUEST_TIMEOUT_MS="${MCP_REQUEST_TIMEOUT_MS:-900000}" \
  nohup npx --yes @truefoundry/trueforge@latest --port "$PORT" >> trueforge.log 2>&1 &
echo "TrueForge pid $! (port $PORT, log agent/spike/trueforge.log)"
