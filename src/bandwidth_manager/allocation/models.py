"""Inputs and outputs for bandwidth allocation and congestion detection.

Bandwidth values are integer bits per second. There is no clock, no socket,
and no Linux traffic-control type in this module.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientRequest:
    """One client's claim on the shared capacity.

    demand is how much the client currently wants. weight is a positive
    fairness weight. min_bandwidth is the floor to honor when the client wants
    at least that much and the floors fit. max_bandwidth is an optional cap.
    None means the only caps are demand and the shared capacity.
    """

    id: str
    demand: int
    weight: int = 1
    min_bandwidth: int = 0
    max_bandwidth: int | None = None


@dataclass(frozen=True)
class Allocation:
    client_id: str
    allocated_bandwidth: int


@dataclass(frozen=True)
class AllocationResult:
    """Allocations in client-id order, plus whether minimums had to be scaled."""

    allocations: tuple[Allocation, ...]
    guarantees_scaled: bool


@dataclass(frozen=True)
class CongestionDecision:
    utilization: float
    threshold: float
    congested: bool
