#!/usr/bin/env bash
# Start the wavebreak-fleet MCP server in the background (log: run/fleet-mcp.log).
# Reads LAB_API_TOKEN etc. from <repo>/.env unless already set. Real lab, real hawkBit.
# Refuses to start a second copy when the port is taken.
set -euo pipefail
cd "$(dirname "$0")/.."
ENVFILE="${WAVEBREAK_ENV:-.env}"
# shellcheck disable=SC1090
[[ -f $ENVFILE ]] && { set -a; . "$ENVFILE"; set +a; }
PORT="${FLEET_MCP_PORT:-8792}"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "port $PORT already in use; fleet MCP (or something else) is running. Not starting." >&2
  exit 1
fi
[[ -x .venv/bin/python ]] || { echo ".venv missing: python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt" >&2; exit 1; }
mkdir -p run
export PYTHONPATH=.
export FLEET_LEDGER_PATH="${FLEET_LEDGER_PATH:-run/fleet-ledger.sqlite}"
nohup .venv/bin/python -m agent.fleet_mcp >> run/fleet-mcp.log 2>&1 &
echo "fleet MCP pid $! (port $PORT, log run/fleet-mcp.log)"
