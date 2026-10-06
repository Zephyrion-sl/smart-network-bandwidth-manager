"""Deterministic bandwidth allocation and congestion detection."""

from bandwidth_manager.allocation.allocator import allocate
from bandwidth_manager.allocation.congestion import detect_congestion, utilization
from bandwidth_manager.allocation.models import (
    Allocation,
    AllocationResult,
    ClientRequest,
    CongestionDecision,
)

__all__ = [
    "Allocation",
    "AllocationResult",
    "ClientRequest",
    "CongestionDecision",
    "allocate",
    "detect_congestion",
    "utilization",
]
