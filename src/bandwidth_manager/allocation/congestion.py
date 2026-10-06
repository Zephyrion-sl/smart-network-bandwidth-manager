"""Congestion is a comparison of measured use with a threshold.

utilization = total_used_bandwidth / total_capacity

congested is true when utilization is greater than or equal to the threshold.
This function does not look at allocations and does not smooth the signal.
"""

from bandwidth_manager.allocation.models import CongestionDecision


def utilization(total_used_bandwidth: int, total_capacity: int) -> float:
    _require_used(total_used_bandwidth)
    _require_capacity(total_capacity)
    return total_used_bandwidth / total_capacity


def detect_congestion(
    total_used_bandwidth: int,
    total_capacity: int,
    threshold: float,
) -> CongestionDecision:
    ratio = utilization(total_used_bandwidth, total_capacity)
    _require_threshold(threshold)
    return CongestionDecision(
        utilization=ratio,
        threshold=threshold,
        congested=ratio >= threshold,
    )


def _require_used(total_used_bandwidth: int) -> None:
    if not isinstance(total_used_bandwidth, int) or isinstance(total_used_bandwidth, bool):
        raise TypeError("total_used_bandwidth must be an int")
    if total_used_bandwidth < 0:
        raise ValueError("total_used_bandwidth must be >= 0")


def _require_capacity(total_capacity: int) -> None:
    if not isinstance(total_capacity, int) or isinstance(total_capacity, bool):
        raise TypeError("total_capacity must be an int")
    if total_capacity <= 0:
        raise ValueError("total_capacity must be > 0 to compute utilization")


def _require_threshold(threshold: float) -> None:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise TypeError("threshold must be a real number")
    if threshold != threshold or threshold < 0 or threshold > 1:
        raise ValueError("threshold must be between 0 and 1 inclusive")
