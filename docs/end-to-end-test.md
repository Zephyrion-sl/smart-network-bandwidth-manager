# End-to-end test

**Status: enforcement is not verified. The project is not complete.**

The pass condition is a change in iperf3 goodput on the namespace topology: three clients compete, `tc` is installed through the API, goodput falls toward the allocation, then rises after the ceilings are removed. Unit tests, scripted counters, and a dashboard fed by those counters do not meet that condition.

## Topology

Namespaces and addresses from `scripts/lib/topology.sh`:

| Namespace | Role | Address |
|---|---|---|
| `bw-gw` | gateway, IPv4 forwarding on | LAN `192.168.100.1`, WAN `10.200.0.1` |
| `bw-wan` | upstream | `10.200.0.2` |
| `bw-client-a` | client | `192.168.100.11` on `bwv-a` |
| `bw-client-b` | client | `192.168.100.12` on `bwv-b` |
| `bw-client-c` | client | `192.168.100.13` on `bwv-c` |

Uploads are shaped on `bwv-gw-wan` inside `bw-gw` (HTB, match source IP). The host default route and host firewall are not part of the lab. There is no NAT: `bw-wan` has a static route back to `192.168.100.0/24`.

Total capacity in this procedure is **100 Mbps** (`100000000` bits per second). Congestion starts at 80% and clears only below 70%.

## Commands

On a Linux host with root, `iproute2`, `iperf3`, `iputils-ping`, `curl`, and Python 3.11+:

```text
sudo ./scripts/test_end_to_end.sh
```

That script is the demonstration. It exits **2** and prints `ENFORCEMENT NOT VERIFIED` when `ip`, `tc`, or `iperf3` is missing. It exits **0** only after the iperf3 checks below pass. Exit **1** is a failed live check.

Supporting commands, which are not a substitute for the script:

```text
sudo ./scripts/test_connectivity.sh --recreate
sudo ./scripts/test_qos.sh
sudo ./scripts/test_controller.sh
python -m pytest -q
```

`test_controller.sh` only checks controller log reasons. It does not measure goodput.

## Test procedure

`scripts/test_end_to_end.sh` does the following, in order:

1. Recreate the namespaces twice (`test_connectivity.sh --recreate`) and require pings both ways plus forwarding.
2. Start the FastAPI process against a 100 Mbps policy with cooldown 0.
3. With no iperf running, `POST /qos/apply` until the reason is `not_congested`, and require `enforced` false and no HTB qdisc.
4. Run three simultaneous iperf3 uploads (client to `10.200.0.2`, ports 5201–5203) and record uncapped goodput.
5. While client B is sending, compare `MonitoringService` upload bits per second with `(tx_bytes delta) * 8 / elapsed` from `/sys/class/net/bwv-b/statistics/tx_bytes`. They must agree within 25%.
6. Run the three uploads again, `POST /qos/apply` until the body says `reason=applied` and `enforced=true`, and require an HTB class on `bwv-gw-wan`.
7. Stop those flows, leave `tc` in place, and measure the three uploads again.
8. Require the allocation sum to be within 100 Mbps, and at least one fast sender’s capped goodput to land between 50% and 130% of its allocation and below 70% of its uncapped goodput.
9. Stop traffic and `POST /qos/apply` until HTB is gone from `bwv-gw-wan`. `GET /allocations` must be empty, so the dashboard’s enforced column would say **not enforced**.
10. Measure client B again. Goodput must rise above both the capped result and the removed allocation.
11. `GET /`, `GET /clients`, `GET /clients/not-a-client` (404), and an invalid `POST /policies` (422).
12. Require the host default route to be unchanged, then destroy and create the topology once more and ping the gateway.

## Expected results

| Step | Expected |
|---|---|
| Routing | Each client pings `192.168.100.1` and `10.200.0.2`; the return ping works; forwarding is 1 |
| Low traffic | `not_congested`, `enforced: false`, no HTB |
| Uncapped iperf | Sum of goodput high enough that a 100 Mbps share is visible (at least 1.5× the later allocation on a judged client) |
| Monitor vs sysfs | Within 25% |
| Apply | HTTP 200, `enforced: true`, shares sum to at most 100 Mbps, `tc class show dev bwv-gw-wan` contains HTB |
| Capped iperf | Goodput moves down toward the allocation |
| Recovery | HTB gone, API allocations empty, client B goodput moves back up |
| API errors | 404 `not_found`, 422 `validation_error` |
| Recreate | Second setup pings the gateway |

## Actual results

Measured on this workstation (Windows 10, Python 3.12.5) on 6 October 2026.

| Tool | Result |
|---|---|
| `bash` | missing |
| `ip` | missing |
| `tc` | missing |
| `iperf3` | missing |
| `wsl echo ok` | exit 1, WSL usage text, no distribution |

`sudo ./scripts/test_end_to_end.sh` was **not executed**. No namespace was created, no packet was counted, and no HTB qdisc was installed.

| Check | Result |
|---|---|
| 1. Gateway routes traffic | Not run |
| 2. Monitoring sees actual traffic | Not run |
| 3. Throughput formula | Unit tests of the byte-delta formula pass. The live sysfs comparison was not run |
| 4. Congestion detection | Unit tests pass. Not observed on live traffic |
| 5. Allocation sums stay within capacity | Unit tests pass. Not observed on a live apply |
| 6. `tc` changes throughput | **Not verified** |
| 7. API reports reality | API tests use scripted counters. They do not prove a live `tc` result |
| 8. Dashboard reflects backend state | A browser session showed the page tracking scripted API fields, including **not enforced** before a successful apply flag. That session did not use the Linux topology |
| 9. Errors | `tests/test_api.py` covers 404, 422, and a failed `tc` runner (HTTP 409, `enforced: false`) |
| 10. Topology can be recreated | Not run |

`python -m pytest -q tests/test_labcheck.py` — 9 passed. Those tests lock the pass/fail rules (goodput must fall, a slow link is inconclusive, recovery must rise). They do not generate traffic.

**Verdict: do not mark the project complete.**

## Known limitations

- Proof requires a Linux host as root. This Windows PC cannot supply it until WSL or another Linux environment is installed, which was not done here because it needs an administrator install and a new distribution.
- iperf3 goodput is accepted inside 50–130% of the allocation and must be under 70% of the uncapped rate. The script does not claim an exact Mbps match.
- Shaping is egress only. This procedure measures uploads. Download shaping is covered by `scripts/test_qos.sh` when that script is actually run; it has not been run here either.
- The dashboard’s enforced column uses `tc -json class show` when that command returns JSON. If the command is missing or the output is not JSON, the column falls back to the share this process last installed. A SQLite row from a previous process is not shown as enforced.
- `test_controller.sh` can report a log line `applied` without proving the client’s goodput changed. Use `test_end_to_end.sh` for that proof.
