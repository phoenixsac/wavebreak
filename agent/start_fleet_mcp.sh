#!/usr/bin/env bash
# Start the wavebreak-fleet MCP server in the background (log: run/fleet-mcp.log).
# Reads LAB_API_TOKEN etc. from ~/wavebreak/.env (read-only) unless already set. Real lab, real hawkBit.
cd "$(dirname "$0")/.."
ENVFILE="${WAVEBREAK_ENV:-$HOME/wavebreak/.env}"
[[ -f $ENVFILE ]] && { set -a; . "$ENVFILE"; set +a; }
export PYTHONPATH=.
export FLEET_LEDGER_PATH="${FLEET_LEDGER_PATH:-run/fleet-ledger.sqlite}"
nohup .venv/bin/python -m agent.fleet_mcp >> run/fleet-mcp.log 2>&1 &
echo "fleet MCP pid $!"
