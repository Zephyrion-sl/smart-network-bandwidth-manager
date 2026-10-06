#!/usr/bin/env bash
# Measure client B with iperf3, install a 20 Mbps HTB ceiling, measure again.
# Requires root: sudo ./scripts/test_qos.sh
#
# Pass, for upload and for download separately:
#   uncapped goodput >= 1.5 * 20 Mbps
#   capped goodput is between 50% and 130% of 20 Mbps
#   capped goodput < 70% of the uncapped goodput
# If the uncapped run is too slow to show a limit, the script exits 2
# and does not call that direction enforced.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: this test needs root." >&2
  echo "Run: sudo $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

CAP_BPS=20000000
MIN_RATIO="0.50"
MAX_RATIO="1.30"
UNCAP_FACTOR="1.5"
DROP_RATIO="0.70"
SERVER_IP="10.200.0.2"
CLIENT_NS="bw-client-b"
WAN_NS="bw-wan"

for cmd in ip iperf3 python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: ${cmd} is required. Enforcement was not measured." >&2
    exit 2
  fi
done

if ! ip netns list | awk '{print $1}' | grep -Fxq "$CLIENT_NS"; then
  echo "error: namespace ${CLIENT_NS} is missing. Run sudo ./scripts/setup_network.sh first." >&2
  exit 2
fi

host_route_before="$(ip -4 route show default || true)"
server_pid=""

cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" >/dev/null 2>&1 || true
  fi
  python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear

ip netns exec "$WAN_NS" iperf3 -s >/tmp/bw-iperf-server.log 2>&1 &
server_pid=$!
sleep 0.5

bits_per_second() {
  local ns="$1"
  shift
  ip netns exec "$ns" iperf3 -J -t 10 -O 2 "$@" | python3 -c '
import json, sys
data = json.load(sys.stdin)
end = data.get("end", {})
sent = end.get("sum_sent", {}).get("bits_per_second")
received = end.get("sum_received", {}).get("bits_per_second")
value = received if received is not None else sent
if value is None:
    raise SystemExit("iperf3 JSON has no bits_per_second")
print(value)
'
}

judge() {
  local direction="$1"
  local uncapped="$2"
  local capped="$3"
  python3 - "$direction" "$uncapped" "$capped" "$CAP_BPS" "$MIN_RATIO" "$MAX_RATIO" "$UNCAP_FACTOR" "$DROP_RATIO" <<'PY'
import sys
direction, uncapped, capped, cap, low, high, uncap_factor, drop_ratio = sys.argv[1:]
uncapped = float(uncapped)
capped = float(capped)
cap = float(cap)
low = float(low) * cap
high = float(high) * cap
if uncapped < float(uncap_factor) * cap:
    print(f"INCONCLUSIVE {direction}: uncapped {uncapped:.0f} bps is not fast enough to show a {cap:.0f} bps ceiling")
    raise SystemExit(2)
if not (low <= capped <= high and capped < uncapped * float(drop_ratio)):
    print(
        f"FAIL {direction}: uncapped {uncapped:.0f} bps, capped {capped:.0f} bps, "
        f"expected {low:.0f}..{high:.0f} and below {uncapped * float(drop_ratio):.0f}"
    )
    raise SystemExit(1)
print(f"PASS {direction}: uncapped {uncapped:.0f} bps, capped {capped:.0f} bps, ceiling {cap:.0f} bps")
PY
}

run_direction() {
  local name="$1"
  shift
  echo "Measuring ${name} without HTB"
  local uncapped capped
  uncapped="$(bits_per_second "$CLIENT_NS" -c "$SERVER_IP" "$@")"
  echo "Installing 20 Mbps ceiling for client-b"
  python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" \
    apply-limit client-b "$CAP_BPS"
  echo "Measuring ${name} with HTB"
  capped="$(bits_per_second "$CLIENT_NS" -c "$SERVER_IP" "$@")"
  python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear
  judge "$name" "$uncapped" "$capped"
}

run_direction upload
run_direction download -R

host_route_after="$(ip -4 route show default || true)"
if [[ "$host_route_before" != "$host_route_after" ]]; then
  echo "error: host default route changed during the QoS test" >&2
  exit 1
fi

echo "QoS enforcement measurements passed within the documented tolerance."
