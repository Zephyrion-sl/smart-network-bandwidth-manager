#!/usr/bin/env bash
# Create the bandwidth-lab namespace topology.
# Requires root: sudo ./scripts/setup_network.sh
#
# Does not change the host default route, host forwarding flag, or firewall.
# Does not install bandwidth control (no tc, no HTB, no token bucket).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/topology.sh
source "${SCRIPT_DIR}/lib/topology.sh"

require_root "$@"
require_ip

on_setup_error() {
  trap - ERR
  echo "error: setup failed near line ${1}. Removing the partial lab topology." >&2
  destroy_topology
  exit 1
}
trap 'on_setup_error $LINENO' ERR

echo "Checking that lab namespaces and interface names are free."
preflight_absent

echo "Recording the host default route and host ip_forward so they can be checked later."
save_host_snapshot

echo "Creating namespaces."
ip netns add "$BW_NS_GW"
ip netns add "$BW_NS_WAN"
ip netns add "$BW_NS_A"
ip netns add "$BW_NS_B"
ip netns add "$BW_NS_C"

echo "Bringing up loopback in each namespace."
for ns in "${BW_ALL_NS[@]}"; do
  nsexec "$ns" ip link set lo up
done

echo "Enabling IPv4 forwarding inside ${BW_NS_GW} only."
nsexec "$BW_NS_GW" sh -c 'echo 1 > /proc/sys/net/ipv4/ip_forward'

echo "Creating the WAN veth pair inside the namespaces (not on the host)."
ip link add "$BW_VETH_WAN" netns "$BW_NS_WAN" type veth peer name "$BW_VETH_GW_WAN" netns "$BW_NS_GW"

echo "Addressing the WAN link ${BW_GW_WAN_CIDR} <-> ${BW_WAN_CIDR}."
nsexec "$BW_NS_WAN" ip link set "$BW_VETH_WAN" up
nsexec "$BW_NS_WAN" ip addr add "$BW_WAN_CIDR" dev "$BW_VETH_WAN"
nsexec "$BW_NS_GW" ip link set "$BW_VETH_GW_WAN" up
nsexec "$BW_NS_GW" ip addr add "$BW_GW_WAN_CIDR" dev "$BW_VETH_GW_WAN"

echo "Installing namespace routes. NAT is not used."
nsexec "$BW_NS_WAN" ip route add "$BW_LAN_NET" via "$BW_GW_WAN_IP"
nsexec "$BW_NS_GW" ip route add default via "$BW_WAN_IP"

echo "Creating the LAN bridge inside ${BW_NS_GW} with STP off."
nsexec "$BW_NS_GW" ip link add "$BW_LAN_BR" type bridge
nsexec "$BW_NS_GW" ip link set "$BW_LAN_BR" type bridge stp_state 0 forward_delay 0
nsexec "$BW_NS_GW" ip link set "$BW_LAN_BR" up
nsexec "$BW_NS_GW" ip addr add "$BW_GW_LAN_CIDR" dev "$BW_LAN_BR"

echo "Attaching clients A, B, and C to the LAN bridge."
for i in 0 1 2; do
  ns="${BW_CLIENT_NS[$i]}"
  ipaddr="${BW_CLIENT_IP[$i]}"
  client_if="${BW_CLIENT_VETH[$i]}"
  gw_if="${BW_GW_VETH[$i]}"

  ip link add "$client_if" netns "$ns" type veth peer name "$gw_if" netns "$BW_NS_GW"

  nsexec "$BW_NS_GW" ip link set "$gw_if" master "$BW_LAN_BR"
  nsexec "$BW_NS_GW" ip link set "$gw_if" up

  nsexec "$ns" ip link set "$client_if" up
  nsexec "$ns" ip addr add "${ipaddr}/24" dev "$client_if"
  nsexec "$ns" ip route add default via "$BW_GW_LAN_IP"
done

forwarding="$(nsexec "$BW_NS_GW" cat /proc/sys/net/ipv4/ip_forward)"
if [[ "$forwarding" != "1" ]]; then
  echo "error: gateway forwarding is '${forwarding}', expected 1." >&2
  false
fi

echo "Confirming the host default route and host ip_forward are unchanged."
host_snapshot_unchanged

trap - ERR
echo
echo "Lab topology is up."
echo "  gateway ${BW_NS_GW}: ${BW_GW_LAN_CIDR} on ${BW_LAN_BR}, ${BW_GW_WAN_CIDR} on ${BW_VETH_GW_WAN}"
echo "  upstream ${BW_NS_WAN}: ${BW_WAN_CIDR}"
echo "  client A ${BW_NS_A}: ${BW_CLIENT_IP[0]}/24"
echo "  client B ${BW_NS_B}: ${BW_CLIENT_IP[1]}/24"
echo "  client C ${BW_NS_C}: ${BW_CLIENT_IP[2]}/24"
echo
echo "Next:"
echo "  sudo ./scripts/show_network.sh"
echo "  sudo ./scripts/test_connectivity.sh"
