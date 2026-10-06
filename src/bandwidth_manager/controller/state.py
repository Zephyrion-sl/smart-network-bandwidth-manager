"""Memory the control loop keeps between samples."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DecisionRecord:
    timestamp: datetime
    observed_utilization: float | None
    congestion_state: bool
    previous_allocation: dict[str, int]
    new_allocation: dict[str, int]
    reason: str
    applied: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "observed_utilization": self.observed_utilization,
            "congestion_state": self.congestion_state,
            "previous_allocation": dict(self.previous_allocation),
            "new_allocation": dict(self.new_allocation),
            "reason": self.reason,
            "applied": self.applied,
        }


@dataclass
class ControlState:
    congested: bool = False
    allocation: dict[str, int] = field(default_factory=dict)
    last_change_monotonic: float | None = None
    decisions: list[DecisionRecord] = field(default_factory=list)
