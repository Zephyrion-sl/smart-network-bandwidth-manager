"""Pure rate math. Inputs are counters and a duration, never a clock or a socket."""


def throughput_bps(current_bytes: int, previous_bytes: int, elapsed_seconds: float) -> float | None:
    """Bits per second from a counter delta.

    Returns None when the interval cannot support a rate: non-positive elapsed
    time, a counter that went backwards, or a negative counter. Callers log
    those cases. This function does not substitute zero.
    """

    if elapsed_seconds <= 0:
        return None
    if current_bytes < 0 or previous_bytes < 0:
        return None
    if current_bytes < previous_bytes:
        return None
    return (current_bytes - previous_bytes) * 8 / elapsed_seconds


def average_throughput_bps(transferred_bytes: int, elapsed_seconds: float) -> float | None:
    """Mean bits per second over the whole observation window."""

    if elapsed_seconds <= 0 or transferred_bytes < 0:
        return None
    return transferred_bytes * 8 / elapsed_seconds


def direction_utilization_percent(bps: float | None, capacity_bps: int | None) -> float | None:
    """Percent of a configured link capacity used by one direction.

    Values above 100 are kept. Clamping would hide a rate that exceeded the
    configured bottleneck. None means the rate or the capacity is unknown.
    """

    if bps is None or capacity_bps is None or capacity_bps <= 0:
        return None
    return bps / capacity_bps * 100.0


def link_utilization_percent(
    rx_bps: float | None,
    tx_bps: float | None,
    capacity_bps: int | None,
) -> float | None:
    """Full-duplex utilization: the busier direction divided by capacity.

    A veth can carry receive and transmit at the same time, so the directions
    are not added. Both rates must be known. A counter reset in one direction
    makes the link figure unknown for that interval.
    """

    if rx_bps is None or tx_bps is None or capacity_bps is None or capacity_bps <= 0:
        return None
    return max(rx_bps, tx_bps) / capacity_bps * 100.0
