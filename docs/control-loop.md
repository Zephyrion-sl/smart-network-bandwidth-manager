# Control loop

The loop does not replace monitoring, allocation, congestion detection, or HTB. Each tick calls those pieces in order and writes one decision record.

```text
sample the client veth counters
        ↓
rates from byte deltas and monotonic time
        ↓
utilization = sum(client loads) / total_capacity
        ↓
hysteresis updates congestion_state
        ↓
if congested: weighted max-min allocation
if not:       desired allocation is empty
        ↓
compare with the allocation last installed
        ↓
tc only when the difference is material and cooldown has elapsed
        ↓
log the decision
        ↓
wait sampling_interval and repeat
```

Client load is the busier of that sample's receive and transmit rates. The first sample has no previous counter, so the tick logs `insufficient_samples` and does not touch `tc`.

## When the link is congested

`detect_congestion` is unchanged: congested means `used / capacity >= threshold`.

The loop remembers the previous answer and applies a second, lower threshold:

| Current state | Becomes congested when | Becomes clear when |
|---|---|---|
| Clear | utilization >= `congestion_threshold` | |
| Congested | | utilization < `congestion_threshold - clear_margin` |

With the example config, limits turn on at 80% and turn off only below 70%. A reading of 75% after a congested reading keeps the limits. That is the hysteresis that blocks `limit → remove → limit → remove` when traffic sits on the threshold.

While congested, each client's demand is its measured load. Loads at or below `idle_bps` demand nothing. `allocate()` then applies `minimum_bandwidth`, `maximum_bandwidth`, and `weights`. A heavy client whose demand is above the fair share is cut back. A light client keeps its smaller demand. That is the redistribution.

While clear, the desired allocation is empty: no HTB ceilings.

## When tc runs

`tc` runs only if both of these are true:

1. Some client differs from the installed share by more than `change_tolerance_bps`. Going from no limits to a share, or back to no limits, counts.
2. `cooldown` seconds have passed since the last successful `tc` change.

Otherwise the reason is `unchanged` or `cooldown` and the executor is not called. `QoSManager` is still what installs or deletes qdiscs when a change is allowed. A failed command logs `enforcement_failed` and leaves the previous share in place. Dry-run QoS never counts as applied.

## Decision record

Every tick logs one JSON object:

| Field | Meaning |
|---|---|
| `timestamp` | Wall time of the decision |
| `observed_utilization` | `used / total_capacity`, or null when there is no rate yet |
| `congestion_state` | Hysteresis result |
| `previous_allocation` | Ceilings currently installed, in bits per second |
| `new_allocation` | Ceilings the policy wants now |
| `reason` | Why tc did or did not run |
| `applied` | True only when Linux accepted the change |

Reasons: `insufficient_samples`, `not_congested`, `unchanged`, `cooldown`, `applied`, `cleared`, `enforcement_failed`.

## Parameters

`config/controller.example.yaml`:

| Parameter | Example | Role |
|---|---|---|
| `sampling_interval` | 1 second | Time between ticks |
| `congestion_threshold` | 0.8 | Enter congestion |
| `clear_margin` | 0.1 | Leave congestion this far below the enter threshold |
| `total_capacity` | 100 Mbps | Denominator and pool the allocator shares |
| `minimum_bandwidth` | 0 | Floor passed into the allocator |
| `maximum_bandwidth` | null | Optional cap passed into the allocator |
| `weights` | 1, 1, 1 | Per-client fairness weights |
| `cooldown` | 10 seconds | Minimum gap between tc changes |
| `change_tolerance_bps` | 1 Mbps | Ignore smaller share edits |

## What the lab integration shows

The integration test uses clients A, B, and C, the gateway namespace `bw-gw`, and the veth names from the topology. It feeds counter deltas and records the `tc` argument arrays.

1. Normal traffic at 3 Mbps of a 30 Mbps pool stays clear and runs no `tc`.
2. Client B at 40 Mbps plus 1 Mbps from A and C crosses 80%. `congestion_state` becomes true.
3. The new share is 1 Mbps, 28 Mbps, and 1 Mbps. Client B is below the 40 Mbps it was sending.
4. The recorded commands are `ip netns exec bw-gw tc ...` on `bwv-gw-b` and `bwv-gw-wan`, with `rate 28000000bit ceil 28000000bit`.
5. The same load on the next tick does not run `tc` again.
6. After utilization collapses, the limits stay for the cooldown, including across the region between 70% and 80%. When the cooldown ends and utilization is still under 70%, the root qdiscs are deleted.

A separate tick sequence at 79%, then 81%, then 75% installs one ceiling and does not delete it when the reading falls back near the threshold.

On a Linux host with the namespace topology up:

```bash
sudo python -m bandwidth_manager.controller --config config/controller.example.yaml
```

`--dry-run` logs decisions without installing qdiscs. `--iterations N` stops after N ticks.
