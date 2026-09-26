#!/bin/sh
# Writes inference-app restart count (systemd NRestarts) for the node_exporter textfile collector.
set -eu
DIR=/var/lib/node_exporter/textfile
n=$(systemctl show -p NRestarts --value inference-app.service 2>/dev/null || echo 0)
result=$(systemctl show -p Result --value inference-app.service 2>/dev/null || echo unknown)
tmp="$DIR/.restarts.prom.$$"
{
  echo "# HELP wavebreak_app_restarts_total systemd NRestarts of inference-app.service."
  echo "# TYPE wavebreak_app_restarts_total counter"
  echo "wavebreak_app_restarts_total ${n:-0}"
  echo "# HELP wavebreak_app_last_result Last systemd Result of inference-app.service (1 for the current value)."
  echo "# TYPE wavebreak_app_last_result gauge"
  echo "wavebreak_app_last_result{result=\"${result:-unknown}\"} 1"
} > "$tmp"
mv "$tmp" "$DIR/restarts.prom"
