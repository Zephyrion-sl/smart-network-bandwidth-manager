"""Scripted counter source for service tests. Not used by the monitor CLI."""

from datetime import UTC, datetime

from bandwidth_manager.monitoring.collector import ClientEndpoint, CounterSource, TrafficCollector
from bandwidth_manager.monitoring.models import InterfaceCounters
from bandwidth_manager.monitoring.service import MonitoringService


class ScriptedSource(CounterSource):
    def __init__(self, series: dict[str, list[InterfaceCounters | None | Exception]]) -> None:
        self.series = series
        self.index = {client_id: 0 for client_id in series}

    def read_counters(self, endpoint: ClientEndpoint):
        values = self.series[endpoint.client_id]
        cursor = self.index[endpoint.client_id]
        self.index[endpoint.client_id] = cursor + 1
        value = values[cursor]
        if isinstance(value, Exception):
            raise value
        return value


class ManualClock:
    def __init__(self) -> None:
        self.ns = 0
        self.stamp = datetime(2026, 10, 6, tzinfo=UTC)

    def monotonic_ns(self) -> int:
        return self.ns

    def wall(self) -> datetime:
        return self.stamp


def counters(rx: int, tx: int = 0, rx_packets: int = 0, tx_packets: int = 0) -> InterfaceCounters:
    return InterfaceCounters(rx_bytes=rx, tx_bytes=tx, rx_packets=rx_packets, tx_packets=tx_packets)


def endpoint(client_id: str, ip: str, interface: str) -> ClientEndpoint:
    return ClientEndpoint(client_id=client_id, ip_address=ip, interface=interface, namespace=None)


def service_for(
    series: dict[str, list],
    endpoints: list[ClientEndpoint],
    clock: ManualClock,
    *,
    capacity: int | None = None,
) -> MonitoringService:
    collector = TrafficCollector(
        ScriptedSource(series),
        monotonic_ns=clock.monotonic_ns,
        wall_time=clock.wall,
    )
    return MonitoringService(collector, endpoints, link_capacity_bps=capacity)
