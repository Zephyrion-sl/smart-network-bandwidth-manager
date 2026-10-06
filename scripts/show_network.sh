#!/usr/bin/env bash
# Print addresses, routes, and gateway forwarding for the lab namespaces.
# Requires root: sudo ./scripts/show_network.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/topology.sh
source "${SCRIPT_DIR}/lib/topology.sh"

require_root "$@"
require_ip

present=0
for ns in "${BW_ALL_NS[@]}"; do
  if netns_exists "$ns"; then
    present=1
    echo "======== ${ns} ========"
    echo "-- links --"
    nsexec "$ns" ip -br link
    echo "-- addresses --"
    nsexec "$ns" ip -br addr
    echo "-- routes --"
    nsexec "$ns" ip route
    if [[ "$ns" == "$BW_NS_GW" ]]; then
      echo "-- bridge --"
      nsexec "$ns" ip -br link show master "$BW_LAN_BR" || true
      echo "-- ipv4 forwarding --"
      nsexec "$ns" cat /proc/sys/net/ipv4/ip_forward
      echo "-- neighbors --"
      nsexec "$ns" ip neigh show
    fi
    echo
  else
    echo "======== ${ns} (absent) ========"
    echo
  fi
done

echo "======== host default route (must not be a lab interface) ========"
ip -4 route show default || true
echo
echo "======== host net.ipv4.ip_forward ========"
cat /proc/sys/net/ipv4/ip_forward
echo

if [[ "$present" -eq 0 ]]; then
  echo "No lab namespaces are present. Create them with: sudo ./scripts/setup_network.sh"
fi
