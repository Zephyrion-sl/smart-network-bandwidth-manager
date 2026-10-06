"""Pure control policy: hysteresis, demand, and whether tc should change.

The allocator, the congestion comparison, and tc stay in their own modules.
This file only decides when those results are allowed to reach enforcement.
"""

from collections.abc import Mapping

from bandwidth_manager.allocation.congestion import detect_congestion
from bandwidth_manager.allocation.models import ClientRequest


def next_congested(
    currently_congested: bool,
    total_used_bandwidth: int,
    total_capacity: int,
    enter_threshold: float,
    clear_margin: float,
) -> bool:
    """Enter at the threshold. Leave only after utilization falls by clear_margin.

    A sample that wobbles just under the enter threshold does not remove limits.
    detect_congestion is the comparison; the margin picks which threshold is used.
    """

    if clear_margin < 0 or clear_margin >= enter_threshold:
        raise ValueError("clear_margin must be >= 0 and smaller than the enter threshold")
    if currently_congested:
        return detect_congestion(
            total_used_bandwidth,
            total_capacity,
            enter_threshold - clear_margin,
        ).congested
    return detect_congestion(total_used_bandwidth, total_capacity, enter_threshold).congested


def measured_load_bps(rx_bps: float | None, tx_bps: float | None) -> int | None:
    """Client load is the busier direction. None means this interval has no rate yet."""

    known = [rate for rate in (rx_bps, tx_bps) if rate is not None]
    if not known:
        return None
    return int(round(max(known)))


def client_requests(
    loads_bps: Mapping[str, int],
    *,
    weights: Mapping[str, int],
    minimum_bandwidth: int,
    maximum_bandwidth: int | None,
    idle_bps: int,
    minimum_overrides: Mapping[str, int] | None = None,
    maximum_overrides: Mapping[str, int | None] | None = None,
) -> list[ClientRequest]:
    """Demand is the measured load. Idle clients demand nothing, so they get nothing.

    Overrides replace the shared minimum or maximum for that client only.
    Missing keys keep the shared value. An override of None means no maximum.
    """

    minimum_overrides = minimum_overrides or {}
    maximum_overrides = maximum_overrides or {}
    requests: list[ClientRequest] = []
    for client_id in sorted(loads_bps):
        load = loads_bps[client_id]
        demand = 0 if load <= idle_bps else load
        if client_id in maximum_overrides:
            maximum = maximum_overrides[client_id]
        else:
            maximum = maximum_bandwidth
        requests.append(
            ClientRequest(
                id=client_id,
                demand=demand,
                weight=weights[client_id],
                min_bandwidth=minimum_overrides.get(client_id, minimum_bandwidth),
                max_bandwidth=maximum,
            )
        )
    return requests


def materially_different(
    installed: Mapping[str, int],
    desired: Mapping[str, int],
    tolerance_bps: int,
) -> bool:
    """True when some client's share differs by more than tolerance_bps."""

    if tolerance_bps < 0:
        raise ValueError("tolerance_bps must be >= 0")
    keys = set(installed) | set(desired)
    return any(abs(installed.get(key, 0) - desired.get(key, 0)) > tolerance_bps for key in keys)


def cooldown_active(now: float, last_change: float | None, cooldown: float) -> bool:
    if cooldown < 0:
        raise ValueError("cooldown must be >= 0")
    if last_change is None:
        return False
    return (now - last_change) < cooldown


def decide(
    installed: Mapping[str, int],
    desired: Mapping[str, int],
    *,
    congested: bool,
    cooldown: bool,
    tolerance_bps: int,
) -> tuple[str, bool]:
    """Return (reason, should_touch_tc).

    should_touch_tc is false for an unchanged share and during cooldown.
    That is what stops limit/remove/limit/remove while utilization hovers
    near the threshold.
    """

    if not materially_different(installed, desired, tolerance_bps):
        if not congested and not installed:
            return "not_congested", False
        return "unchanged", False
    if cooldown:
        return "cooldown", False
    if not desired:
        return "cleared", True
    return "applied", True
