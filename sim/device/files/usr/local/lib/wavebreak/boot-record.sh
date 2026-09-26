#!/bin/sh
# boot-record start|stop. start: append a boot record; stop: write the clean-shutdown marker.
set -eu
LOG=/var/log/wavebreak/boot.log
MARKER=/var/lib/wavebreak/clean_shutdown
mkdir -p /var/log/wavebreak /var/lib/wavebreak /run/wavebreak

case ${1:-start} in
  start)
    if [ -f "$MARKER" ]; then prev=clean; else prev=unclean; fi
    [ -f /var/lib/wavebreak/booted_before ] || prev=first_boot
    rm -f "$MARKER"
    touch /var/lib/wavebreak/booted_before
    # containers share the host kernel boot_id, so generate one per start
    boot_id=$(cat /proc/sys/kernel/random/uuid)
    echo "$boot_id" > /run/wavebreak/boot_id
    date +%s > /run/wavebreak/boot_time
    # shellcheck disable=SC1091
    . /etc/wavebreak/identity.env
    fw=$(python3 -c 'import json;print(json.load(open("/opt/app/current/manifest.json"))["version"])' 2>/dev/null || echo unknown)
    printf '{"event":"boot","boot_id":"%s","ts":"%s","fw_version":"%s","previous_shutdown":"%s","device_id":"%s","hw_rev":"%s","region":"%s"}\n' \
      "$boot_id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$fw" "$prev" "$DEVICE_ID" "$HW_REV" "$REGION" >> "$LOG"
    ;;
  stop)
    touch "$MARKER"
    ;;
esac
