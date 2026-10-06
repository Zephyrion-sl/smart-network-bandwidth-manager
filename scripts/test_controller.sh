#!/usr/bin/env bash
# Live control-loop check inside the lab namespaces.
# Requires root: sudo ./scripts/test_controller.sh
#
# Drives client B with iperf3, runs a few controller ticks against a 20 Mbps
# pool, and looks for an applied decision followed by a clear after the flow
# stops. Exits 2 when this host cannot run the namespace topology.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: this test needs root." >&2
  echo "Run: sudo $0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

for cmd in ip iperf3 python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: ${cmd} is required. The control loop was not measured on a live topology." >&2
    exit 2
  fi
done

if ! ip netns list | awk '{print $1}' | grep -Fxq bw-client-b; then
  echo "error: lab namespaces are not up. Run sudo ./scripts/setup_network.sh" >&2
  exit 2
fi

tmp="$(mktemp)"
cat >"$tmp" <<EOF
sampling_interval: 1.0
congestion_threshold: 0.8
clear_margin: 0.1
total_capacity: 20000000
minimum_bandwidth: 0
maximum_bandwidth: null
cooldown: 3
change_tolerance_bps: 1000000
idle_bps: 10000
weights:
  client-a: 1
  client-b: 1
  client-c: 1
monitor_config: ${ROOT}/config/monitor.example.yaml
qos_config: ${ROOT}/config/qos.example.yaml
EOF

server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" >/dev/null 2>&1 || true
  fi
  python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear \
    >/dev/null 2>&1 || true
  rm -f "$tmp"
}
trap cleanup EXIT

python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear
ip netns exec bw-wan iperf3 -s >/tmp/bw-controller-iperf.log 2>&1 &
server_pid=$!
sleep 0.5
ip netns exec bw-client-b iperf3 -c 10.200.0.2 -t 20 >/tmp/bw-controller-client.log 2>&1 &
flow_pid=$!

log="$(mktemp)"
python3 -m bandwidth_manager.controller --config "$tmp" --iterations 8 >"$log" 2>&1 || true
if ! grep -q '"reason": "applied"' "$log"; then
  echo "FAIL: controller did not apply a ceiling while client B was sending" >&2
  cat "$log" >&2
  exit 1
fi

kill "$flow_pid" >/dev/null 2>&1 || true
wait "$flow_pid" >/dev/null 2>&1 || true
python3 -m bandwidth_manager.controller --config "$tmp" --iterations 8 >"$log" 2>&1 || true
if ! grep -q '"reason": "cleared"' "$log"; then
  echo "FAIL: controller did not clear ceilings after the flow stopped" >&2
  cat "$log" >&2
  exit 1
fi

echo "Controller log showed applied, then cleared. This does not measure iperf goodput."
echo "Enforcement proof is sudo ./scripts/test_end_to_end.sh"
