"""Turn interface counter reads into timestamped TrafficSample values."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bandwidth_manager.monitoring.models import TrafficSample
from bandwidth_manager.monitoring.sysfs import InvalidMeasurement
from bandwidth_manager.monitoring.validate import validate_client_id, validate_interface, validate_namespace

logger = logging.getLogger(__name__)


class ClientEndpoint(BaseModel):
    """Where to read one client's counters. This is configuration, not a measurement."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    ip_address: str
    interface: str
    namespace: str | None = None
    link_capacity_bps: int | None = Field(default=None, gt=0)

    @field_validator("client_id")
    @classmethod
    def _client_id(cls, value: str) -> str:
        return validate_client_id(value)

    @field_validator("interface")
    @classmethod
    def _interface(cls, value: str) -> str:
        return validate_interface(value)

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_namespace(value)


class CounterSource(Protocol):
    """Reads kernel counters. Implementations must not invent byte values."""

    def read_counters(self, endpoint: ClientEndpoint):
        """Return counters, or None if the interface is gone."""


@dataclass(frozen=True)
class CollectionBatch:
    samples: list[TrafficSample]
    disappeared: frozenset[str]
    invalid: frozenset[str]


class TrafficCollector:
    """Collect one sample per configured client.

    The collector does not compute throughput and does not know about HTTP.
    """

    def __init__(
        self,
        source: CounterSource,
        *,
        monotonic_ns: Callable[[], int] | None = None,
        wall_time: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._monotonic_ns = monotonic_ns or _monotonic_ns
        self._wall_time = wall_time or (lambda: datetime.now(UTC))

    def collect(self, endpoints: Sequence[ClientEndpoint]) -> CollectionBatch:
        monotonic_ns = self._monotonic_ns()
        timestamp = self._wall_time()
        samples: list[TrafficSample] = []
        disappeared: set[str] = set()
        invalid: set[str] = set()

        for endpoint in endpoints:
            try:
                counters = self._source.read_counters(endpoint)
            except InvalidMeasurement as exc:
                logger.error("invalid measurement for %s: %s", endpoint.client_id, exc)
                invalid.add(endpoint.client_id)
                continue
            except Exception:
                logger.exception("collector failed for %s", endpoint.client_id)
                invalid.add(endpoint.client_id)
                continue

            if counters is None:
                logger.error(
                    "interface %s for %s disappeared",
                    endpoint.interface,
                    endpoint.client_id,
                )
                disappeared.add(endpoint.client_id)
                continue

            samples.append(
                TrafficSample(
                    client_id=endpoint.client_id,
                    ip_address=endpoint.ip_address,
                    interface=endpoint.interface,
                    rx_bytes=counters.rx_bytes,
                    tx_bytes=counters.tx_bytes,
                    rx_packets=counters.rx_packets,
                    tx_packets=counters.tx_packets,
                    timestamp=timestamp,
                    monotonic_ns=monotonic_ns,
                )
            )

        return CollectionBatch(
            samples=samples,
            disappeared=frozenset(disappeared),
            invalid=frozenset(invalid),
        )


def _monotonic_ns() -> int:
    import time

    return time.monotonic_ns()
