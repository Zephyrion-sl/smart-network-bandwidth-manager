"""Weighted max-min allocation.

The steps below are the whole algorithm. Nothing else in the project is
allowed to invent a different split.

1. Reject inputs that are not usable as bandwidth or weights.
2. A client with no demand, or a ceiling of zero, is allocated 0.
3. Each remaining client's ceiling is min(demand, max_bandwidth).
4. Each remaining client's floor is min(min_bandwidth, ceiling). A minimum
   above the client's demand or maximum is not given: that bandwidth was not
   requested, or the maximum forbids it.
5. If the floors fit in total_capacity, start each client at that floor.
   If they do not fit, scale the floors by capacity / sum(floors) so the
   scaled floors sum to total_capacity. That is the insufficient-capacity case.
6. Give any capacity still left by weighted water-filling. Clients who have
   reached their ceiling drop out. The others receive shares proportional to
   weight, never more than their remaining room.
7. Integer bits per second only. A leftover bit goes to the lowest client id
   that still has room.

The same inputs always produce the same allocations. Input order does not matter.
"""

from collections.abc import Sequence

from bandwidth_manager.allocation.models import Allocation, AllocationResult, ClientRequest


def allocate(total_capacity: int, clients: Sequence[ClientRequest]) -> AllocationResult:
    _validate_capacity(total_capacity)
    requests = _validate_clients(clients)
    ceilings = {client.id: _ceiling(client) for client in requests}
    floors = {client.id: min(client.min_bandwidth, ceilings[client.id]) for client in requests}

    guarantees_scaled = sum(floors.values()) > total_capacity
    if guarantees_scaled:
        allocated = _scale_to_capacity(floors, total_capacity)
    else:
        allocated = dict(floors)

    _water_fill(allocated, ceilings, requests, total_capacity)
    _check_result(allocated, ceilings, total_capacity)

    ordered = tuple(
        Allocation(client_id=client_id, allocated_bandwidth=allocated[client_id])
        for client_id in sorted(allocated)
    )
    return AllocationResult(allocations=ordered, guarantees_scaled=guarantees_scaled)


def _validate_capacity(total_capacity: int) -> None:
    if not isinstance(total_capacity, int) or isinstance(total_capacity, bool):
        raise TypeError("total_capacity must be an int")
    if total_capacity < 0:
        raise ValueError("total_capacity must be >= 0")


def _validate_clients(clients: Sequence[ClientRequest]) -> tuple[ClientRequest, ...]:
    seen: set[str] = set()
    for client in clients:
        if not isinstance(client.id, str) or client.id == "":
            raise ValueError("client id must be a non-empty string")
        if client.id in seen:
            raise ValueError(f"duplicate client id: {client.id}")
        seen.add(client.id)
        _require_int("demand", client.demand, minimum=0)
        _require_int("weight", client.weight, minimum=1)
        _require_int("min_bandwidth", client.min_bandwidth, minimum=0)
        if client.max_bandwidth is not None:
            _require_int("max_bandwidth", client.max_bandwidth, minimum=0)
    return tuple(clients)


def _require_int(name: str, value: int, *, minimum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


def _ceiling(client: ClientRequest) -> int:
    ceiling = client.demand
    if client.max_bandwidth is not None:
        ceiling = min(ceiling, client.max_bandwidth)
    return ceiling


def _scale_to_capacity(floors: dict[str, int], total_capacity: int) -> dict[str, int]:
    """Shrink floors that do not fit. Proportions follow the floors, not the weights."""

    total_floors = sum(floors.values())
    allocated = {
        client_id: (total_capacity * floor) // total_floors
        for client_id, floor in floors.items()
    }
    remaining = total_capacity - sum(allocated.values())
    # Each division drops less than one unit, so the leftover is smaller than
    # the number of clients. Walk ids until the leftover is gone.
    while remaining > 0:
        progressed = False
        for client_id in sorted(floors):
            if remaining == 0:
                break
            if allocated[client_id] < floors[client_id]:
                allocated[client_id] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return allocated


def _water_fill(
    allocated: dict[str, int],
    ceilings: dict[str, int],
    requests: Sequence[ClientRequest],
    total_capacity: int,
) -> None:
    weights = {client.id: client.weight for client in requests}
    remaining = total_capacity - sum(allocated.values())

    while remaining > 0:
        unsatisfied = [
            client_id
            for client_id in sorted(allocated)
            if allocated[client_id] < ceilings[client_id]
        ]
        if not unsatisfied:
            break

        weight_sum = sum(weights[client_id] for client_id in unsatisfied)
        offers: dict[str, int] = {}
        granted = 0
        for client_id in unsatisfied:
            room = ceilings[client_id] - allocated[client_id]
            share = (remaining * weights[client_id]) // weight_sum
            offer = min(room, share)
            offers[client_id] = offer
            granted += offer

        if granted == 0:
            progressed = False
            for client_id in unsatisfied:
                if remaining == 0:
                    break
                if allocated[client_id] < ceilings[client_id]:
                    allocated[client_id] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                break
            continue

        for client_id, offer in offers.items():
            allocated[client_id] += offer
        remaining -= granted


def _check_result(allocated: dict[str, int], ceilings: dict[str, int], total_capacity: int) -> None:
    total = sum(allocated.values())
    if total > total_capacity:
        raise RuntimeError(f"allocation sum {total} exceeds capacity {total_capacity}")
    for client_id, amount in allocated.items():
        if amount < 0 or amount > ceilings[client_id]:
            raise RuntimeError(f"allocation for {client_id} is outside its feasible range")
