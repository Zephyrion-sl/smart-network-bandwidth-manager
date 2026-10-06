"""HTTP request and response models. No tc or counter math lives here."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bandwidth_manager.monitoring.validate import validate_client_id


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorInfo


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    dry_run: bool


class CurrentBandwidth(BaseModel):
    rx_bps: float | None = None
    tx_bps: float | None = None
    utilization_percent: float | None = None


class UsageBucket(BaseModel):
    client_id: str
    bucket_start: datetime
    rx_bytes: int
    tx_bytes: int
    samples: int
    avg_rx_bps: float | None
    avg_tx_bps: float | None


class ClientPolicyView(BaseModel):
    weight: int
    min_bandwidth: int | None
    max_bandwidth: int | None


class ClientView(BaseModel):
    client_id: str
    ip: str
    mac: str | None
    interface: str
    namespace: str | None
    connection_status: Literal["unknown", "online", "offline"]
    current_bandwidth: CurrentBandwidth
    allocation_bps: int | None
    policy: ClientPolicyView
    usage_bytes: int


class ClientDetail(ClientView):
    usage: list[UsageBucket]


class ClientList(BaseModel):
    clients: list[ClientView]


class MetricSample(BaseModel):
    client_id: str
    ip: str
    interface: str
    rx_bps: float | None
    tx_bps: float | None
    utilization_percent: float | None
    interval_seconds: float | None
    total_bytes: int


class MetricsResponse(BaseModel):
    sampled_at: datetime
    clients: list[MetricSample]
    usage: list[UsageBucket]


class AllocationItem(BaseModel):
    client_id: str
    allocated_bandwidth: int


class AllocationDecision(BaseModel):
    decided_at: datetime
    reason: str
    applied: bool
    observed_utilization: float | None
    congested: bool
    allocation: dict[str, int]


class AllocationsResponse(BaseModel):
    congested: bool
    observed_utilization: float | None
    allocations: list[AllocationItem]
    history: list[AllocationDecision]


class ClientPolicyModel(BaseModel):
    client_id: str
    weight: int
    min_bandwidth: int | None
    max_bandwidth: int | None


class PolicyResponse(BaseModel):
    congestion_threshold: float
    clear_margin: float
    total_capacity: int
    minimum_bandwidth: int
    maximum_bandwidth: int | None
    cooldown: float
    change_tolerance_bps: int
    sampling_interval: float
    idle_bps: int
    clients: list[ClientPolicyModel]


class PolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    congestion_threshold: float = Field(gt=0, le=1)
    clear_margin: float = Field(ge=0)
    total_capacity: int = Field(gt=0)
    minimum_bandwidth: int = Field(ge=0)
    maximum_bandwidth: int | None = Field(default=None, gt=0)
    cooldown: float = Field(ge=0)
    change_tolerance_bps: int = Field(ge=0)
    weights: dict[str, int]

    @field_validator("weights")
    @classmethod
    def _weights(cls, value: dict[str, int]) -> dict[str, int]:
        if not value:
            raise ValueError("weights must name at least one client")
        for client_id, weight in value.items():
            validate_client_id(client_id)
            if weight < 1:
                raise ValueError(f"weight for {client_id} must be >= 1")
        return value

    @model_validator(mode="after")
    def _bounds(self) -> "PolicyWrite":
        if self.clear_margin >= self.congestion_threshold:
            raise ValueError("clear_margin must be smaller than congestion_threshold")
        if self.maximum_bandwidth is not None and self.minimum_bandwidth > self.maximum_bandwidth:
            raise ValueError("minimum_bandwidth exceeds maximum_bandwidth")
        return self


class ClientPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: int = Field(ge=1)
    min_bandwidth: int | None = Field(default=None, ge=0)
    max_bandwidth: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _bounds(self) -> "ClientPolicyWrite":
        if (
            self.min_bandwidth is not None
            and self.max_bandwidth is not None
            and self.min_bandwidth > self.max_bandwidth
        ):
            raise ValueError("min_bandwidth exceeds max_bandwidth")
        return self


class QosActionResponse(BaseModel):
    reason: str
    applied: bool
    enforced: bool
    dry_run: bool
    congestion_state: bool
    observed_utilization: float | None
    previous_allocation: dict[str, int]
    new_allocation: dict[str, int]
    errors: list[str]
    commands: list[str]


class EventItem(BaseModel):
    id: int
    timestamp: datetime
    kind: str
    message: str
    details: dict[str, Any]


class EventList(BaseModel):
    events: list[EventItem]
