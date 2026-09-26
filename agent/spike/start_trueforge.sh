#!/usr/bin/env bash
# Start TrueForge local mode in the background (logs to agent/spike/trueforge.log).
# OUTBOUND_URL_ALLOWED_HOSTS: TrueForge's SSRF guard blocks loopback MCP URLs otherwise.
# MCP_REQUEST_TIMEOUT_MS: per-tool-call timeout (default 240000).
cd "$(dirname "$0")"
source ~/.nvm/nvm.sh >/dev/null 2>&1
OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' \
MCP_REQUEST_TIMEOUT_MS="${MCP_REQUEST_TIMEOUT_MS:-900000}" \
  nohup npx --yes @truefoundry/trueforge@latest --port "${PORT:-8790}" >> trueforge.log 2>&1 &
