#!/usr/bin/env bash
# Verify client-to-gateway and client-to-upstream connectivity.
# Requires root: sudo ./scripts/test_connectivity.sh
#
# sudo ./scripts/test_connectivity.sh --recreate
#   destroys the lab, creates it, tests, destroys it again, creates it again, and tests.
#   Leaves the topology up when both rounds pass.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/topology.sh
source "${SCRIPT_DIR}/lib/topology.sh"

require_root "$@"
require_ip

mode="once"
if [[ "${1:-}" == "--recreate" ]]; then
  mode="recreate"
elif [[ -n "${1:-}" ]]; then
  echo "usage: sudo $0 [--recreate]" >&2
  exit 2
fi

if ! command -v ping >/dev/null 2>&1; then
  echo "error: ping is required (install iputils-ping)." >&2
  exit 1
fi

failures=0

check() {
  local desc="$1"
  shift
  local out
  if out="$("$@" 2>&1)"; then
    printf '[pass] %s\n' "$desc"
  else
    printf '[fail] %s\n' "$desc"
    printf '%s\n' "$out" >&2
    failures=$((failures + 1))
  fi
}

require_topology() {
  local ns
  for ns in "${BW_ALL_NS[@]}"; do
    if ! netns_exists "$ns"; then
      echo "error: namespace '${ns}' is missing. Run: sudo ./scripts/setup_network.sh" >&2
      exit 1
    fi
  done
}

gateway_forwarding_on() {
  local value
  value="$(nsexec "$BW_NS_GW" cat /proc/sys/net/ipv4/ip_forward)"
  [[ "$value" == "1" ]]
}

wan_has_lan_route() {
  nsexec "$BW_NS_WAN" ip route show "$BW_LAN_NET" | grep -q "via ${BW_GW_WAN_IP}"
}

client_has_default() {
  local ns="$1"
  nsexec "$ns" ip route show default | grep -q "via ${BW_GW_LAN_IP}"
}

run_checks() {
  failures=0
  require_topology

  local i ns ipaddr
  for i in 0 1 2; do
    ns="${BW_CLIENT_NS[$i]}"
    ipaddr="${BW_CLIENT_IP[$i]}"
    check "Client ${ns} pings gateway ${BW_GW_LAN_IP}" \
      nsexec "$ns" ping -n -q -c 2 -W 2 "$BW_GW_LAN_IP"
    check "Client ${ns} has default route via ${BW_GW_LAN_IP}" \
      client_has_default "$ns"
    check "Client ${ns} (${ipaddr}) reaches upstream ${BW_WAN_IP}" \
      nsexec "$ns" ping -n -q -c 2 -W 2 "$BW_WAN_IP"
    check "Upstream reaches client ${ipaddr}" \
      nsexec "$BW_NS_WAN" ping -n -q -c 2 -W 2 "$ipaddr"
  done

  check "Gateway IPv4 forwarding is enabled" gateway_forwarding_on
  check "Upstream has a route to ${BW_LAN_NET} via ${BW_GW_WAN_IP}" wan_has_lan_route
  check "Host default route and ip_forward are unchanged" host_snapshot_unchanged

  if [[ "$failures" -ne 0 ]]; then
    echo "${failures} check(s) failed."
    return 1
  fi
  echo "All connectivity checks passed."
}

if [[ "$mode" == "recreate" ]]; then
  echo "Round 1: destroy, create, test."
  "${SCRIPT_DIR}/cleanup_network.sh"
  "${SCRIPT_DIR}/setup_network.sh"
  run_checks
  echo
  echo "Round 2: destroy, create, test."
  "${SCRIPT_DIR}/cleanup_network.sh"
  "${SCRIPT_DIR}/setup_network.sh"
  run_checks
  echo
  echo "Topology was destroyed and recreated successfully. It is up now."
else
  run_checks
fi
