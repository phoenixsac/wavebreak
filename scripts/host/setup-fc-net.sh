#!/usr/bin/env bash
# Host networking for the Firecracker runtime. Run once with sudo (not persistent across reboot / `wsl --shutdown`).
#
#   sudo scripts/host/setup-fc-net.sh            # create bridges, NAT, taps (N_FIELD=2 N_LAB=2 by default)
#   sudo N_FIELD=20 N_LAB=4 scripts/host/setup-fc-net.sh
#   sudo scripts/host/setup-fc-net.sh --down     # remove everything this script created
#
# Zones:
#   field  wbfield0 172.30.0.1/24  taps fc-field-<i>  NAT to outside + reach backend ports published on the host
#   lab    wblab0   172.31.0.1/24  taps fc-lab-<i>    no NAT, no forwarding, no new connections into the host
#                                                     (the lab controller on the host initiates; replies are allowed)
# Taps are owned by $TAP_USER (default: $SUDO_USER) so Firecracker runs without root afterwards.
set -euo pipefail

N_FIELD=${N_FIELD:-2}
N_LAB=${N_LAB:-2}
TAP_USER=${TAP_USER:-${SUDO_USER:-$(id -un)}}
FIELD_BR=wbfield0; FIELD_CIDR=172.30.0.1/24; FIELD_NET=172.30.0.0/24
LAB_BR=wblab0;     LAB_CIDR=172.31.0.1/24

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# iptables helper: add rule only if missing (idempotent); delete if present.
ipt_add() { local t=$1; shift; iptables -t "$t" -C "$@" 2>/dev/null || iptables -t "$t" -A "$@"; }
ipt_ins() { local t=$1; shift; local c=$1; shift; iptables -t "$t" -C "$c" "$@" 2>/dev/null || iptables -t "$t" -I "$c" 1 "$@"; }
ipt_del() { local t=$1; shift; while iptables -t "$t" -C "$@" 2>/dev/null; do iptables -t "$t" -D "$@"; done; }

# Docker sets FORWARD policy DROP; its DOCKER-USER chain is the supported place for our rules.
FWD_CHAIN=FORWARD
iptables -t filter -nL DOCKER-USER >/dev/null 2>&1 && FWD_CHAIN=DOCKER-USER

down() {
  for dev in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^fc-(field|lab)-[0-9]+$' || true); do
    ip link del "$dev" 2>/dev/null || true
  done
  ip link del "$FIELD_BR" 2>/dev/null || true
  ip link del "$LAB_BR" 2>/dev/null || true
  ipt_del nat POSTROUTING -s "$FIELD_NET" ! -o "$FIELD_BR" -j MASQUERADE
  ipt_del filter "$FWD_CHAIN" -i "$FIELD_BR" -j ACCEPT
  ipt_del filter "$FWD_CHAIN" -o "$FIELD_BR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  ipt_del filter "$FWD_CHAIN" -i "$LAB_BR" -j DROP
  ipt_del filter "$FWD_CHAIN" -o "$LAB_BR" -j DROP
  ipt_del filter INPUT -i "$LAB_BR" -j DROP
  ipt_del filter INPUT -i "$LAB_BR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  echo "removed wavebreak firecracker networking"
}

mkbridge() {
  local br=$1 cidr=$2
  ip link show "$br" >/dev/null 2>&1 || ip link add "$br" type bridge
  ip addr show dev "$br" | grep -q "${cidr%/*}/" || ip addr add "$cidr" dev "$br"
  ip link set "$br" up
}

mktaps() {
  local prefix=$1 br=$2 n=$3 i
  for ((i = 1; i <= n; i++)); do
    local tap="${prefix}-${i}"
    ip link show "$tap" >/dev/null 2>&1 || ip tuntap add dev "$tap" mode tap user "$TAP_USER"
    ip link set "$tap" master "$br"
    ip link set "$tap" up
  done
}

up() {
  sysctl -qw net.ipv4.ip_forward=1

  mkbridge "$FIELD_BR" "$FIELD_CIDR"
  mkbridge "$LAB_BR" "$LAB_CIDR"
  mktaps fc-field "$FIELD_BR" "$N_FIELD"
  mktaps fc-lab "$LAB_BR" "$N_LAB"

  # field: outbound NAT + forwarding. Backend ports published by Docker on the host are reached via 172.30.0.1.
  ipt_add nat POSTROUTING -s "$FIELD_NET" ! -o "$FIELD_BR" -j MASQUERADE
  ipt_ins filter "$FWD_CHAIN" -i "$FIELD_BR" -j ACCEPT
  ipt_ins filter "$FWD_CHAIN" -o "$FIELD_BR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

  # lab: isolated. No forwarding either way; host accepts only replies to connections it opened.
  ipt_ins filter "$FWD_CHAIN" -i "$LAB_BR" -j DROP
  ipt_ins filter "$FWD_CHAIN" -o "$LAB_BR" -j DROP
  ipt_add filter INPUT -i "$LAB_BR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  ipt_add filter INPUT -i "$LAB_BR" -j DROP

  echo "field: $FIELD_BR $FIELD_CIDR taps fc-field-1..$N_FIELD (owner $TAP_USER)"
  echo "lab:   $LAB_BR $LAB_CIDR taps fc-lab-1..$N_LAB (owner $TAP_USER)"
  echo "forward chain: $FWD_CHAIN"
}

case "${1:-up}" in
  --down|down) down ;;
  up|"") up ;;
  *) echo "usage: $0 [--down]" >&2; exit 2 ;;
esac
