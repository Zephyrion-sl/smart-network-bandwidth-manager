#!/usr/bin/env bash
# End-to-end proof on the namespace topology.
#
#   sudo ./scripts/test_end_to_end.sh
#
# Exit 0 only when iperf3 goodput falls under tc and rises again after the
# ceilings are removed. Exit 2 when this host cannot run that measurement.
# Scripted counters are not a pass.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "error: this proof needs root so it can build namespaces and run tc." >&2
  echo "Run: sudo $0" >&2
  echo "ENFORCEMENT NOT VERIFIED" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
PORT="${BW_E2E_PORT:-8766}"
CAPACITY=100000000

for cmd in ip tc iperf3 python3 ping curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: ${cmd} is required. Actual traffic was not measured." >&2
    echo "ENFORCEMENT NOT VERIFIED" >&2
    exit 2
  fi
done

host_route_before="$(ip -4 route show default || true)"
api_pid=""
server_pids=()
flow_pids=()
tmpdir="$(mktemp -d)"

stop_pid() {
  local pid="${1:-}"
  if [[ -n "$pid" ]]; then
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local pid
  for pid in "${flow_pids[@]:-}"; do
    stop_pid "$pid"
  done
  for pid in "${server_pids[@]:-}"; do
    stop_pid "$pid"
  done
  stop_pid "$api_pid"
  python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear \
    >/dev/null 2>&1 || true
  rm -rf "$tmpdir"
}
trap cleanup EXIT

cat >"${tmpdir}/control.yaml" <<EOF
sampling_interval: 1.0
congestion_threshold: 0.8
clear_margin: 0.1
total_capacity: ${CAPACITY}
minimum_bandwidth: 0
maximum_bandwidth: null
cooldown: 0
change_tolerance_bps: 1000000
idle_bps: 10000
weights:
  client-a: 1
  client-b: 1
  client-c: 1
monitor_config: ${ROOT}/config/monitor.example.yaml
qos_config: ${ROOT}/config/qos.example.yaml
EOF

echo "Recreate the topology and prove forwarding."
"${SCRIPT_DIR}/test_connectivity.sh" --recreate

export BW_CONTROLLER_CONFIG="${tmpdir}/control.yaml"
export BW_PORT="${PORT}"
export BW_HOST="127.0.0.1"
export BW_DATABASE_PATH="${tmpdir}/e2e.sqlite"
python3 -m bandwidth_manager.api >"${tmpdir}/api.log" 2>&1 &
api_pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  sleep 0.3
done
curl -sf "http://127.0.0.1:${PORT}/health" | tee "${tmpdir}/health.json"
python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["status"]=="ok"' <"${tmpdir}/health.json"

echo "Low traffic: the controller must not install ceilings."
python3 -m bandwidth_manager.enforcement --config "${ROOT}/config/qos.example.yaml" clear
low_reason=""
for _ in 1 2 3; do
  curl -s -X POST "http://127.0.0.1:${PORT}/qos/apply" >"${tmpdir}/low.json"
  low_reason="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))' <"${tmpdir}/low.json")"
  if [[ "$low_reason" == "not_congested" ]]; then
    break
  fi
  sleep 1
done
if [[ "$low_reason" != "not_congested" ]]; then
  echo "FAIL: low traffic produced ${low_reason}, expected not_congested" >&2
  cat "${tmpdir}/low.json" >&2
  exit 1
fi
python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["enforced"] is False' <"${tmpdir}/low.json"
if ip netns exec bw-gw tc qdisc show | grep -q "htb"; then
  echo "FAIL: HTB is installed while traffic is low" >&2
  exit 1
fi

start_servers() {
  local port
  server_pids=()
  for port in 5201 5202 5203; do
    ip netns exec bw-wan iperf3 -s -p "$port" >"${tmpdir}/server-${port}.log" 2>&1 &
    server_pids+=("$!")
  done
  sleep 0.5
}

stop_flows() {
  local pid
  for pid in "${flow_pids[@]:-}"; do
    stop_pid "$pid"
  done
  flow_pids=()
}

run_iperf_json() {
  local ns="$1"
  local port="$2"
  local seconds="$3"
  local out="$4"
  ip netns exec "$ns" iperf3 -J -t "$seconds" -c 10.200.0.2 -p "$port" >"$out"
}

goodput() {
  python3 -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
end = data.get("end", {})
value = end.get("sum_received", {}).get("bits_per_second")
if value is None:
    value = end.get("sum_sent", {}).get("bits_per_second")
if value is None:
    raise SystemExit("iperf3 JSON has no bits_per_second")
print(value)
' "$1"
}

echo "Measure competing uploads before tc."
start_servers
run_iperf_json bw-client-a 5201 8 "${tmpdir}/uncap-a.json" &
flow_pids+=("$!")
run_iperf_json bw-client-b 5202 8 "${tmpdir}/uncap-b.json" &
flow_pids+=("$!")
run_iperf_json bw-client-c 5203 8 "${tmpdir}/uncap-c.json" &
flow_pids+=("$!")
wait "${flow_pids[@]}" || true
flow_pids=()

echo "Compare the monitor with raw tx_bytes while client B sends."
ip netns exec bw-client-b iperf3 -c 10.200.0.2 -p 5202 -t 6 >"${tmpdir}/cross-iperf.log" 2>&1 &
flow_pids+=("$!")
sleep 1
python3 - <<'PY' >"${tmpdir}/cross.json"
import json, time
from bandwidth_manager.labcheck import throughput_bps
from bandwidth_manager.monitoring.collector import ClientEndpoint, TrafficCollector
from bandwidth_manager.monitoring.linux_collector import LinuxCounterSource
from bandwidth_manager.monitoring.linux_source import LinuxInterfaceSource
from bandwidth_manager.monitoring.service import MonitoringService

source = LinuxInterfaceSource()
endpoint = ClientEndpoint(client_id="client-b", ip_address="192.168.100.12", interface="bwv-b", namespace="bw-client-b")
service = MonitoringService(TrafficCollector(LinuxCounterSource(source)), [endpoint])
first = source.read_counters(namespace="bw-client-b", interface="bwv-b")
started = time.monotonic()
service.sample_once()
time.sleep(2)
second = source.read_counters(namespace="bw-client-b", interface="bwv-b")
elapsed = time.monotonic() - started
metrics = service.sample_once()[0]
print(json.dumps({
    "counter_bps": throughput_bps(first.tx_bytes, second.tx_bytes, elapsed),
    "monitor_bps": metrics.instantaneous_tx_bps,
}))
PY
stop_flows
python3 -m bandwidth_manager.labcheck rates-close \
  --monitor "$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/cross.json"'"))["monitor_bps"])')" \
  --counter "$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/cross.json"'"))["counter_bps"])')"

echo "Generate congestion and apply QoS through the API."
ip netns exec bw-client-a iperf3 -c 10.200.0.2 -p 5201 -t 20 >"${tmpdir}/prime-a.log" 2>&1 &
flow_pids+=("$!")
ip netns exec bw-client-b iperf3 -c 10.200.0.2 -p 5202 -t 20 >"${tmpdir}/prime-b.log" 2>&1 &
flow_pids+=("$!")
ip netns exec bw-client-c iperf3 -c 10.200.0.2 -p 5203 -t 20 >"${tmpdir}/prime-c.log" 2>&1 &
flow_pids+=("$!")
sleep 2
applied=""
for _ in 1 2 3 4 5 6 7 8; do
  code="$(curl -s -o "${tmpdir}/apply.json" -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/qos/apply")"
  applied="$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/apply.json"'")).get("reason",""))')"
  enforced="$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/apply.json"'")).get("enforced", False))')"
  if [[ "$applied" == "applied" && "$enforced" == "True" && "$code" == "200" ]]; then
    break
  fi
  sleep 1
done
if [[ "$applied" != "applied" || "$enforced" != "True" ]]; then
  echo "FAIL: API did not report a successful tc apply during congestion (${applied}, enforced=${enforced})" >&2
  cat "${tmpdir}/apply.json" >&2
  exit 1
fi
python3 -m bandwidth_manager.labcheck judge-allocation --capacity "$CAPACITY" \
  --json "$(python3 -c 'import json; print(json.dumps(json.load(open("'"${tmpdir}/apply.json"'"))["new_allocation"]))')"
if ! ip netns exec bw-gw tc class show dev bwv-gw-wan | grep -q "htb"; then
  echo "FAIL: API said enforced but bwv-gw-wan has no HTB class" >&2
  ip netns exec bw-gw tc qdisc show >&2 || true
  exit 1
fi
stop_flows

echo "Measure the same uploads after tc."
run_iperf_json bw-client-a 5201 8 "${tmpdir}/cap-a.json" &
flow_pids+=("$!")
run_iperf_json bw-client-b 5202 8 "${tmpdir}/cap-b.json" &
flow_pids+=("$!")
run_iperf_json bw-client-c 5203 8 "${tmpdir}/cap-c.json" &
flow_pids+=("$!")
wait "${flow_pids[@]}" || true
flow_pids=()

python3 - <<PY
import json, subprocess, sys
from pathlib import Path
root = Path("${tmpdir}")
alloc = json.loads((root / "apply.json").read_text(encoding="utf-8"))["new_allocation"]

def goodput(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    end = data.get("end", {})
    value = end.get("sum_received", {}).get("bits_per_second")
    if value is None:
        value = end.get("sum_sent", {}).get("bits_per_second")
    if value is None:
        raise SystemExit(f"{path} has no bits_per_second")
    return value

rows = []
for key, client_id in (("a", "client-a"), ("b", "client-b"), ("c", "client-c")):
    rows.append({
        "client_id": client_id,
        "uncapped_bps": goodput(root / f"uncap-{key}.json"),
        "capped_bps": goodput(root / f"cap-{key}.json"),
        "allocation_bps": int(alloc[client_id]),
    })
(root / "rows.json").write_text(json.dumps(rows), encoding="utf-8")
raise SystemExit(subprocess.call([
    sys.executable, "-m", "bandwidth_manager.labcheck", "judge-clients", "--json", json.dumps(rows)
]))
PY

echo "Stop the flows and require the controller to remove ceilings."
cleared=""
for _ in 1 2 3 4 5 6 7 8; do
  curl -s -X POST "http://127.0.0.1:${PORT}/qos/apply" >"${tmpdir}/clear.json"
  cleared="$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/clear.json"'")).get("reason",""))')"
  enforced="$(python3 -c 'import json; print(json.load(open("'"${tmpdir}/clear.json"'")).get("enforced", False))')"
  if [[ "$cleared" == "cleared" && "$enforced" == "True" ]]; then
    break
  fi
  if [[ "$cleared" == "not_congested" ]]; then
    break
  fi
  sleep 1
done
if ip netns exec bw-gw tc qdisc show dev bwv-gw-wan | grep -q "htb"; then
  echo "FAIL: HTB remains on bwv-gw-wan after congestion ended (${cleared})" >&2
  exit 1
fi
python3 -c 'import json,urllib.request; body=json.load(urllib.request.urlopen("http://127.0.0.1:'"${PORT}"'/allocations")); assert body["allocations"]==[]'

echo "Recovery upload must exceed the removed ceiling."
run_iperf_json bw-client-b 5202 8 "${tmpdir}/recover-b.json"
python3 - <<PY
import json, subprocess, sys
from pathlib import Path
root = Path("${tmpdir}")
rows = json.loads((root / "rows.json").read_text(encoding="utf-8"))
client_b = next(row for row in rows if row["client_id"] == "client-b")
data = json.loads((root / "recover-b.json").read_text(encoding="utf-8"))
end = data.get("end", {})
recovered = end.get("sum_received", {}).get("bits_per_second")
if recovered is None:
    recovered = end["sum_sent"]["bits_per_second"]
raise SystemExit(subprocess.call([
    sys.executable, "-m", "bandwidth_manager.labcheck", "judge-recovery",
    "--capped", str(client_b["capped_bps"]),
    "--recovered", str(recovered),
    "--allocation", str(client_b["allocation_bps"]),
]))
PY

echo "API and dashboard must agree with the cleared enforcement state."
python3 - <<PY
import json, urllib.request
base = "http://127.0.0.1:${PORT}"
clients = json.load(urllib.request.urlopen(base + "/clients"))["clients"]
assert [row["client_id"] for row in clients] == ["client-a", "client-b", "client-c"]
assert all(row["allocation_bps"] is None for row in clients)
page = urllib.request.urlopen(base + "/").read().decode()
script = urllib.request.urlopen(base + "/static/dashboard.js").read().decode()
assert "Enforced" in page and "Observed" in page and "Allocated" in page
assert "enforced === true" in script
PY
code="$(curl -s -o "${tmpdir}/missing.json" -w "%{http_code}" "http://127.0.0.1:${PORT}/clients/not-a-client")"
if [[ "$code" != "404" ]]; then
  echo "FAIL: unknown client returned HTTP ${code}" >&2
  exit 1
fi
python3 -c 'import json; body=json.load(open("'"${tmpdir}/missing.json"'")); assert body["error"]["code"]=="not_found"'
code="$(curl -s -o "${tmpdir}/bad.json" -w "%{http_code}" -H "Content-Type: application/json" -d '{"congestion_threshold": 2}' -X POST "http://127.0.0.1:${PORT}/policies")"
if [[ "$code" != "422" ]]; then
  echo "FAIL: invalid policy returned HTTP ${code}" >&2
  exit 1
fi

host_route_after="$(ip -4 route show default || true)"
if [[ "$host_route_before" != "$host_route_after" ]]; then
  echo "FAIL: host default route changed" >&2
  exit 1
fi

stop_pid "$api_pid"
api_pid=""
echo "Destroy and create the topology once more."
"${SCRIPT_DIR}/cleanup_network.sh"
"${SCRIPT_DIR}/setup_network.sh"
ip netns exec bw-client-a ping -n -q -c 2 -W 2 192.168.100.1

echo "PASS: live iperf goodput fell under tc and rose after the ceilings were removed."
