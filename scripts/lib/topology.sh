# Shared names and teardown for the bandwidth-lab network namespaces.
# Sourced by the scripts in scripts/. Do not execute this file directly.
#
# Safety contract:
# - Only the namespaces and interface names listed below are ever created or deleted.
# - No command here changes the host default route, host ip_forward, or any firewall.

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "error: run these scripts with bash." >&2
  exit 1
fi

readonly BW_NS_GW="bw-gw"
readonly BW_NS_WAN="bw-wan"
readonly BW_NS_A="bw-client-a"
readonly BW_NS_B="bw-client-b"
readonly BW_NS_C="bw-client-c"

readonly BW_LAN_BR="bw-lan"
readonly BW_LAN_NET="192.168.100.0/24"
readonly BW_GW_LAN_IP="192.168.100.1"
readonly BW_GW_LAN_CIDR="192.168.100.1/24"

readonly BW_GW_WAN_IP="10.200.0.1"
readonly BW_GW_WAN_CIDR="10.200.0.1/30"
readonly BW_WAN_IP="10.200.0.2"
readonly BW_WAN_CIDR="10.200.0.2/30"

readonly BW_VETH_WAN="bwv-wan"
readonly BW_VETH_GW_WAN="bwv-gw-wan"

readonly BW_STATE_DIR="/run/bw-net-foundation"

BW_CLIENT_NS=("$BW_NS_A" "$BW_NS_B" "$BW_NS_C")
BW_CLIENT_IP=("192.168.100.11" "192.168.100.12" "192.168.100.13")
BW_CLIENT_VETH=("bwv-a" "bwv-b" "bwv-c")
BW_GW_VETH=("bwv-gw-a" "bwv-gw-b" "bwv-gw-c")

BW_ALL_NS=("$BW_NS_GW" "$BW_NS_WAN" "$BW_NS_A" "$BW_NS_B" "$BW_NS_C")

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "error: network namespaces require root." >&2
    echo "Run: sudo $0 $*" >&2
    exit 1
  fi
}

require_ip() {
  if ! command -v ip >/dev/null 2>&1; then
    echo "error: iproute2 is required (the 'ip' command was not found)." >&2
    exit 1
  fi
}

netns_exists() {
  local name="$1"
  ip netns list | awk '{print $1}' | grep -Fxq -- "$name"
}

link_exists() {
  ip link show dev "$1" >/dev/null 2>&1
}

link_is_kind() {
  local name="$1"
  local kind="$2"
  ip -d link show dev "$name" 2>/dev/null | grep -Eq "^[[:space:]]*${kind}([[:space:]]|$)"
}

nsexec() {
  local ns="$1"
  shift
  ip netns exec "$ns" "$@"
}

save_host_snapshot() {
  mkdir -p "$BW_STATE_DIR"
  ip -4 route show default >"${BW_STATE_DIR}/host-default-route.txt"
  cat /proc/sys/net/ipv4/ip_forward >"${BW_STATE_DIR}/host-ip-forward.txt"
}

host_snapshot_unchanged() {
  local saved_route now_route saved_fwd now_fwd
  if [[ ! -f "${BW_STATE_DIR}/host-default-route.txt" || ! -f "${BW_STATE_DIR}/host-ip-forward.txt" ]]; then
    echo "error: missing host snapshot at ${BW_STATE_DIR}." >&2
    return 1
  fi
  saved_route="$(cat "${BW_STATE_DIR}/host-default-route.txt")"
  now_route="$(ip -4 route show default)"
  saved_fwd="$(cat "${BW_STATE_DIR}/host-ip-forward.txt")"
  now_fwd="$(cat /proc/sys/net/ipv4/ip_forward)"
  if [[ "$saved_route" != "$now_route" ]]; then
    echo "error: host default route changed." >&2
    echo "before: ${saved_route:-<none>}" >&2
    echo "now:    ${now_route:-<none>}" >&2
    return 1
  fi
  if [[ "$saved_fwd" != "$now_fwd" ]]; then
    echo "error: host net.ipv4.ip_forward changed from '${saved_fwd}' to '${now_fwd}'." >&2
    return 1
  fi
}

clear_host_snapshot() {
  if [[ "$BW_STATE_DIR" != "/run/bw-net-foundation" ]]; then
    echo "error: refusing to remove unexpected state path '${BW_STATE_DIR}'." >&2
    return 1
  fi
  rm -rf -- "$BW_STATE_DIR"
}

preflight_absent() {
  local name
  for name in "${BW_ALL_NS[@]}"; do
    if netns_exists "$name"; then
      echo "error: namespace '${name}' already exists. Refusing to overwrite it." >&2
      echo "Run: sudo scripts/cleanup_network.sh" >&2
      exit 1
    fi
  done
  for name in "$BW_VETH_WAN" "$BW_VETH_GW_WAN" "${BW_CLIENT_VETH[@]}" "${BW_GW_VETH[@]}" "$BW_LAN_BR"; do
    if link_exists "$name"; then
      echo "error: host interface '${name}' already exists. Refusing to overwrite it." >&2
      echo "Run: sudo scripts/cleanup_network.sh" >&2
      exit 1
    fi
  done
}

delete_host_link_if_ours() {
  local name="$1"
  local kind="$2"
  if ! link_exists "$name"; then
    return 0
  fi
  if link_is_kind "$name" "$kind"; then
    echo "deleting leftover host ${kind} ${name}"
    ip link delete dev "$name" || echo "warning: failed to delete ${name}" >&2
  else
    echo "warning: host interface '${name}' exists but is not a ${kind}. Leaving it in place." >&2
  fi
}

destroy_topology() {
  local ns
  for ns in "$BW_NS_A" "$BW_NS_B" "$BW_NS_C" "$BW_NS_WAN" "$BW_NS_GW"; do
    if netns_exists "$ns"; then
      echo "deleting namespace ${ns}"
      ip netns delete "$ns" || echo "warning: failed to delete namespace ${ns}" >&2
    else
      echo "namespace ${ns} is already absent"
    fi
  done

  # Leftovers exist only if a lab interface was left in the host namespace.
  delete_host_link_if_ours "$BW_VETH_WAN" veth
  delete_host_link_if_ours "$BW_VETH_GW_WAN" veth
  delete_host_link_if_ours "bwv-a" veth
  delete_host_link_if_ours "bwv-gw-a" veth
  delete_host_link_if_ours "bwv-b" veth
  delete_host_link_if_ours "bwv-gw-b" veth
  delete_host_link_if_ours "bwv-c" veth
  delete_host_link_if_ours "bwv-gw-c" veth
  delete_host_link_if_ours "$BW_LAN_BR" bridge

  clear_host_snapshot
}
