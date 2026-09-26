#!/bin/sh
# Boot-time identity and settings.
# Sources: container runtime → PID 1 environment (/proc/1/environ);
#          Firecracker → kernel cmdline args wavebreak.<key>=<value> (key upper-cased).
# Writes: /etc/wavebreak/identity.env (DEVICE_ID, HW_REV, REGION)
#         /etc/wavebreak/device.env   (everything else the units need)
#         /etc/wavebreak/labels.env   (if missing; ota-agent rewrites it after installs)
# Applies: inference-app MemoryMax, telemetry on/off, kmsg input on Firecracker.
set -eu
ETC=/etc/wavebreak
mkdir -p "$ETC" /run/wavebreak
tmp=$(mktemp /run/wavebreak/identity.XXXXXX)
env_tmp="${tmp}.env"
trap 'rm -f "$tmp" "$env_tmp"' EXIT

KEYS="DEVICE_ID HW_REV REGION RUNTIME OTA_MODE TELEMETRY HAWKBIT_DDI_URL HAWKBIT_TENANT DDI_AUTH_MODE \
HAWKBIT_GATEWAY_TOKEN HAWKBIT_TARGET_TOKEN LAB_DEVICE_TOKEN LOCAL_API_PORT HEALTH_WINDOW_S \
PROM_HOST PROM_PORT LOKI_HOST LOKI_PORT APP_MEMORY_MAX APP_ARGS FRAME_SCALE"

# 1. container env of PID 1
if [ -r /proc/1/environ ]; then
  tr '\0' '\n' < /proc/1/environ > "$env_tmp" || true
fi
# 2. kernel cmdline (Firecracker): wavebreak.device_id=edge-001 → DEVICE_ID=edge-001
# shellcheck disable=SC2013 # cmdline is split into words on purpose
for arg in $(cat /proc/cmdline); do
  case $arg in
    wavebreak.*=*)
      k=${arg#wavebreak.}; v=${k#*=}; k=$(echo "${k%%=*}" | tr '[:lower:]' '[:upper:]')
      echo "$k=$v" >> "$env_tmp" ;;
  esac
done

get() { grep "^$1=" "$env_tmp" 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }

: > "$tmp"
for k in $KEYS; do
  v=$(get "$k")
  [ -n "$v" ] && printf '%s=%s\n' "$k" "$v" >> "$tmp"
done

# Defaults
val() { grep "^$1=" "$tmp" | cut -d= -f2- || true; }
[ -n "$(val DEVICE_ID)" ] || echo "DEVICE_ID=$(hostname)" >> "$tmp"
[ -n "$(val HW_REV)" ] || echo "HW_REV=A" >> "$tmp"
[ -n "$(val REGION)" ] || echo "REGION=unknown" >> "$tmp"
[ -n "$(val RUNTIME)" ] || echo "RUNTIME=container" >> "$tmp"
[ -n "$(val OTA_MODE)" ] || echo "OTA_MODE=ddi" >> "$tmp"
[ -n "$(val TELEMETRY)" ] || echo "TELEMETRY=1" >> "$tmp"
[ -n "$(val PROM_HOST)" ] || echo "PROM_HOST=prometheus" >> "$tmp"
[ -n "$(val PROM_PORT)" ] || echo "PROM_PORT=9090" >> "$tmp"
[ -n "$(val LOKI_HOST)" ] || echo "LOKI_HOST=loki" >> "$tmp"
[ -n "$(val LOKI_PORT)" ] || echo "LOKI_PORT=3100" >> "$tmp"
[ -n "$(val APP_MEMORY_MAX)" ] || echo "APP_MEMORY_MAX=48M" >> "$tmp"

grep -E '^(DEVICE_ID|HW_REV|REGION)=' "$tmp" > "$ETC/identity.env"
grep -vE '^(DEVICE_ID|HW_REV|REGION)=' "$tmp" > "$ETC/device.env"
chmod 600 "$ETC/device.env"
rm -f "$tmp"

# shellcheck disable=SC1091
. "$ETC/identity.env"
# shellcheck disable=SC1091
. "$ETC/device.env"
hostname "$DEVICE_ID" 2>/dev/null || true

if [ ! -s "$ETC/labels.env" ]; then
  fw=$(python3 -c 'import json;print(json.load(open("/opt/app/current/manifest.json"))["version"])' 2>/dev/null || echo unknown)
  printf 'DEVICE_ID=%s\nHW_REV=%s\nREGION=%s\nFW_VERSION=%s\n' "$DEVICE_ID" "$HW_REV" "$REGION" "$fw" > "$ETC/labels.env"
fi

# Runtime-only unit settings (no swap so the leak ends in an OOM kill, not swapping)
systemctl set-property --runtime inference-app.service "MemoryMax=$APP_MEMORY_MAX" MemorySwapMax=0 || true

# Telemetry: field devices push to the backend; lab devices have no route to it
if [ "$TELEMETRY" = 1 ]; then touch /run/wavebreak/telemetry; else rm -f /run/wavebreak/telemetry; fi

# kmsg only in Firecracker (in containers /dev/kmsg is the host's)
if [ "$RUNTIME" = firecracker ]; then
  ln -sf kmsg.conf /etc/fluent-bit/wavebreak-runtime.conf
else
  ln -sf empty.conf /etc/fluent-bit/wavebreak-runtime.conf
fi
echo "identity: $DEVICE_ID hw_rev=$HW_REV region=$REGION runtime=$RUNTIME ota_mode=$OTA_MODE"
