"""Adapter from LinuxInterfaceSource to the collector's CounterSource."""

from bandwidth_manager.monitoring.collector import ClientEndpoint, CounterSource
from bandwidth_manager.monitoring.linux_source import LinuxInterfaceSource
from bandwidth_manager.monitoring.models import InterfaceCounters


class LinuxCounterSource(CounterSource):
    def __init__(self, source: LinuxInterfaceSource | None = None) -> None:
        self._source = source or LinuxInterfaceSource()

    def read_counters(self, endpoint: ClientEndpoint) -> InterfaceCounters | None:
        return self._source.read_counters(namespace=endpoint.namespace, interface=endpoint.interface)
