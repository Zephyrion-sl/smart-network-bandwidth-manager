"""API-ready monitoring records. No web framework types live here."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class InterfaceCounters(BaseModel):
    """One read of the kernel's per-interface statistics files."""

    model_config = ConfigDict(frozen=True)

    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)
    rx_packets: int = Field(ge=0)
    tx_packets: int = Field(ge=0)


class TrafficSample(BaseModel):
    """Counters observed for one client at one instant.

    rx_* is what this interface received. On the client veth that is traffic
    arriving at the client. tx_* is what the client sent.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    ip_address: str
    interface: str
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)
    rx_packets: int = Field(ge=0)
    tx_packets: int = Field(ge=0)
    timestamp: datetime
    monotonic_ns: int


class TrafficMetrics(BaseModel):
    """Rates derived from two TrafficSample values, plus the latest counters."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    ip_address: str
    interface: str
    timestamp: datetime
    rx_bytes: int = Field(ge=0)
    tx_bytes: int = Field(ge=0)
    rx_packets: int = Field(ge=0)
    tx_packets: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    interval_seconds: float | None = None
    instantaneous_rx_bps: float | None = None
    instantaneous_tx_bps: float | None = None
    average_rx_bps: float | None = None
    average_tx_bps: float | None = None
    rx_utilization_percent: float | None = None
    tx_utilization_percent: float | None = None
    utilization_percent: float | None = None
    session_rx_bytes: int = Field(default=0, ge=0)
    session_tx_bytes: int = Field(default=0, ge=0)

    def api_dict(self) -> dict[str, object]:
        """JSON-ready payload for a future API. This package does not serve it."""

        return self.model_dump(mode="json")
