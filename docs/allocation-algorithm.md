# Allocation algorithm

This is weighted max-min fairness over integer bits per second. `allocate()` in `src/bandwidth_manager/allocation/allocator.py` is the whole decision. It does not read counters, call Linux `tc`, or depend on FastAPI. Congestion is a separate comparison in `congestion.py`.

## Inputs

| Symbol | Meaning |
|---|---|
| `C` | `total_capacity`, bits per second, `C >= 0` |
| `d_i` | demand of client `i`, `d_i >= 0` |
| `w_i` | positive integer weight |
| `m_i` | minimum bandwidth, `m_i >= 0` |
| `M_i` | optional maximum. Missing means no maximum |

Clients are identified by id. The same set of clients always yields the same numbers. The result is returned sorted by id.

## Ceilings and floors

A client is not given bandwidth it did not ask for, and it is not given more than its maximum:

```text
ceiling_i = d_i                      if M_i is absent
ceiling_i = min(d_i, M_i)            if M_i is present
floor_i   = min(m_i, ceiling_i)
```

Consequences:

- Zero demand makes `ceiling_i = 0` and `floor_i = 0`, so the allocation is 0. An idle client does not reserve its minimum.
- A positive demand below the minimum is served at the demand. The minimum would otherwise exceed what the client asked for.
- A minimum above the maximum is cut down to the maximum. The maximum is the hard cap.

## Step 1: minimums

Let `F = sum(floor_i)`.

If `F <= C`, every client starts at `floor_i`. The minimums are honored.

If `F > C`, the minimums do not fit. This is the capacity-shortage case for guarantees. Scale by the floors, not by the weights, because there is no surplus left for weights to share:

```text
a_i = floor(C * floor_i / F)
```

Integer division drops a remainder `R = C - sum(a_i)`, and `R` is smaller than the number of clients. Give one extra bit at a time to the lowest client ids that are still below their original floor, until `R` is 0 or nobody has room. Then `sum(a_i) = C` when every scaled floor had room for its remainder, which it does while the original floors were the quantities being scaled. `guarantees_scaled` is true. A client may finish below `m_i`.

## Step 2: weighted water-filling

If step 1 used the whole capacity, stop. Otherwise let `R = C - sum(a_i)` be the free capacity.

While `R > 0` and at least one client has `a_i < ceiling_i`:

1. Let `U` be the clients still under their ceiling, sorted by id.
2. Let `W = sum(w_i)` for `i` in `U`.
3. Offer each client

```text
offer_i = min(ceiling_i - a_i, floor(R * w_i / W))
```

4. If the offers sum to a positive `G`, add them to `a_i` and set `R = R - G`. Clients who hit their ceiling leave `U` on the next pass, so bandwidth they could not take is offered to the others.
5. If every offer is 0, `R` is smaller than a weighted share. Give one bit to each client in `U` that still has room, lowest id first. Repeat until `R` is 0 or every remaining client is at its ceiling.

No client is pushed past `ceiling_i`. The loop ends because `R` decreases whenever a bit is given out.

## What the result guarantees

For every run:

```text
sum(a_i) <= C
0 <= a_i <= ceiling_i
```

When `F <= C` and `floor_i = m_i` (the client asked for at least the minimum, and the maximum allows it):

```text
a_i >= m_i
```

When every ceiling is at least the weighted share, minimums are 0, and the division is exact, weights come through directly. Two backlogged clients and `C` bits:

```text
a_1 / a_2 = w_1 / w_2
```

A remainder of a few bits goes to the lowest ids, so the ratio can be off by less than one bit per client. That is the integer rule, not a second policy.

When `sum(d_i) <= C` and no maximum is below a demand, every client receives exactly `d_i`. Unused capacity stays unallocated. The allocator does not inflate a small demand up to a fair share of the whole link.

## Worked examples

Equal clients. `C = 100`, both demands 100, both weights 1, minimums 0.

```text
floors 0 + 0
offers: floor(100 * 1 / 2) = 50 each
a = 50, 50
```

Unequal weights. `C = 100`, demands 1000 and 1000, weights 2 and 1, ids `a` then `b`.

```text
floor(100 * 2 / 3) = 66
floor(100 * 1 / 3) = 33
remainder 1 goes to a
a = 67, b = 33
```

One heavy consumer. `C = 100`, client `light` demand 10, client `heavy` demand 1000, weights 1.

```text
first offers are 50 and 50, but light only has room for 10
light = 10, heavy = 50, remainder 40
heavy takes the rest
light = 10, heavy = 90
```

One idle client. `C = 100`, `idle` demand 0, `busy` demand 1000.

```text
idle ceiling is 0, so idle = 0
busy receives 100
```

Demand below capacity. `C = 100`, demands 10 and 20.

```text
ceilings 10 and 20, floors 0
both reach their ceilings and stop
allocations 10 and 20, sum 30
```

Demand above capacity. `C = 50`, demands 100 and 100, weights 1.

```text
allocations 25 and 25
```

Minimums that fit. `C = 100`, minimums 30 and 30, demands 100, weights 1 and 3, ids `a` weight 1 and `b` weight 3.

```text
start at 30 and 30, remainder 40
floor(40 * 1 / 4) = 10
floor(40 * 3 / 4) = 30
a = 40, b = 60
```

Minimums that do not fit. `C = 90`, minimums 60 and 60, demands 100.

```text
F = 120 > 90
a_i = floor(90 * 60 / 120) = 45
guarantees_scaled = true
allocations 45 and 45
```

Maximum. `C = 100`, both demands 100, weights 1, client `a` maximum 10.

```text
a ceiling is 10
a = 10, b = 90
```

## Congestion

Congestion does not call the allocator. Measured use is an input:

```text
utilization = total_used_bandwidth / total_capacity
congested   = utilization >= threshold
```

`total_capacity` must be positive. `threshold` is a real number from 0 through 1. Utilization may be greater than 1 when measured use exceeds the configured capacity; the comparison still holds, so the link is congested for any threshold in range.

A later control loop can refuse to reallocate until the signal stays over the threshold. That memory is not part of this function. Given the same three numbers, the decision is the same every time.
