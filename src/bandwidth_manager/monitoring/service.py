"""Periodic sampling and rate calculation on top of a TrafficCollector."""

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence

from bandwidth_manager.monitoring.calculate import (
    average_throughput_bps,
    direction_utilization_percent,
    link_utilization_percent,
    throughput_bps,
)
from bandwidth_manager.monitoring.collector import ClientEndpoint, TrafficCollector
from bandwidth_manager.monitoring.models import TrafficMetrics, TrafficSample

logger = logging.getLogger(__name__)


class MonitoringService:
    """Keep per-client baselines and emit TrafficMetrics on each sample.

    Elapsed time uses the monotonic timestamps on the samples. The first sample
    for a client establishes the baseline and does not invent a rate. A counter
    that decreases is a reset: that direction's rate is omitted, and the new
    counter becomes the next baseline.
    """

    def __init__(
        self,
        collector: TrafficCollector,
        endpoints: Sequence[ClientEndpoint],
        *,
        link_capacity_bps: int | None = None,
    ) -> None:
        if link_capacity_bps is not None and link_capacity_bps <= 0:
            raise ValueError("link_capacity_bps must be positive when set")
        self._collector = collector
        self._endpoints = list(endpoints)
        self._default_capacity = link_capacity_bps
        self._previous: dict[str, TrafficSample] = {}
        self._origin_ns: dict[str, int] = {}
        self._session_rx: dict[str, int] = {}
        self._session_tx: dict[str, int] = {}
        self._missing: set[str] = set()
        self._lock = threading.Lock()

    def sample_once(self) -> list[TrafficMetrics]:
        """One sample. Concurrent callers are serialized so baselines stay paired."""

        with self._lock:
            return self._sample_once()

    def _sample_once(self) -> list[TrafficMetrics]:
        batch = self._collector.collect(self._endpoints)
        for client_id in batch.disappeared:
            self._drop_client(client_id, reason="disappeared")
        for client_id in batch.invalid:
            if client_id in self._previous:
                logger.error("keeping the previous baseline for %s after an invalid read", client_id)

        by_id = {sample.client_id: sample for sample in batch.samples}
        metrics: list[TrafficMetrics] = []
        for endpoint in self._endpoints:
            sample = by_id.get(endpoint.client_id)
            if sample is None:
                continue
            if endpoint.client_id in self._missing:
                logger.info("interface for %s is present again", endpoint.client_id)
                self._missing.discard(endpoint.client_id)
            metrics.append(self._fold(sample, endpoint.link_capacity_bps or self._default_capacity))
        return metrics

    def iter_samples(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> Iterator[list[TrafficMetrics]]:
        """Yield sample batches forever, sleeping out the rest of each interval."""

        if interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        now = clock or time.monotonic
        pause = sleep or time.sleep
        next_tick = now()
        while True:
            yield self.sample_once()
            next_tick += interval_seconds
            delay = next_tick - now()
            if delay > 0:
                pause(delay)
            else:
                next_tick = now()

    async def watch(self, interval_seconds: float) -> AsyncIterator[list[TrafficMetrics]]:
        """Async periodic sampling. The counter read stays synchronous and short.

        asyncio is used only to wait between samples without blocking an event
        loop that will later host the API. The collector is not async.
        """

        import asyncio

        if interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            yield self.sample_once()
            next_tick += interval_seconds
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = loop.time()

    def _fold(self, sample: TrafficSample, capacity_bps: int | None) -> TrafficMetrics:
        previous = self._previous.get(sample.client_id)
        if previous is None:
            self._remember_origin(sample)
            return self._metrics(sample, capacity_bps, interval_seconds=None, rx_bps=None, tx_bps=None)

        elapsed = (sample.monotonic_ns - previous.monotonic_ns) / 1_000_000_000
        if elapsed <= 0:
            logger.error(
                "non-positive elapsed time for %s (%.6f s); not computing throughput",
                sample.client_id,
                elapsed,
            )
            return self._metrics(
                sample,
                capacity_bps,
                interval_seconds=None,
                rx_bps=None,
                tx_bps=None,
                session_rx=self._session_rx.get(sample.client_id, 0),
                session_tx=self._session_tx.get(sample.client_id, 0),
                average_from_origin=False,
            )

        rx_reset = sample.rx_bytes < previous.rx_bytes
        tx_reset = sample.tx_bytes < previous.tx_bytes
        if rx_reset or tx_reset:
            logger.error(
                "counter reset for %s on %s (rx %s -> %s, tx %s -> %s)",
                sample.client_id,
                sample.interface,
                previous.rx_bytes,
                sample.rx_bytes,
                previous.tx_bytes,
                sample.tx_bytes,
            )

        rx_bps = None if rx_reset else throughput_bps(sample.rx_bytes, previous.rx_bytes, elapsed)
        tx_bps = None if tx_reset else throughput_bps(sample.tx_bytes, previous.tx_bytes, elapsed)
        if not rx_reset:
            self._session_rx[sample.client_id] += sample.rx_bytes - previous.rx_bytes
        if not tx_reset:
            self._session_tx[sample.client_id] += sample.tx_bytes - previous.tx_bytes
        self._previous[sample.client_id] = sample
        return self._metrics(sample, capacity_bps, interval_seconds=elapsed, rx_bps=rx_bps, tx_bps=tx_bps)

    def _remember_origin(self, sample: TrafficSample) -> None:
        self._previous[sample.client_id] = sample
        self._origin_ns[sample.client_id] = sample.monotonic_ns
        self._session_rx[sample.client_id] = 0
        self._session_tx[sample.client_id] = 0

    def _drop_client(self, client_id: str, *, reason: str) -> None:
        had_baseline = client_id in self._previous
        self._previous.pop(client_id, None)
        self._origin_ns.pop(client_id, None)
        self._session_rx.pop(client_id, None)
        self._session_tx.pop(client_id, None)
        if client_id not in self._missing:
            if had_baseline or reason == "disappeared":
                logger.error("dropping baseline for %s (%s)", client_id, reason)
            self._missing.add(client_id)

    def _metrics(
        self,
        sample: TrafficSample,
        capacity_bps: int | None,
        *,
        interval_seconds: float | None,
        rx_bps: float | None,
        tx_bps: float | None,
        session_rx: int | None = None,
        session_tx: int | None = None,
        average_from_origin: bool = True,
    ) -> TrafficMetrics:
        session_rx_bytes = self._session_rx[sample.client_id] if session_rx is None else session_rx
        session_tx_bytes = self._session_tx[sample.client_id] if session_tx is None else session_tx
        average_rx = None
        average_tx = None
        if average_from_origin:
            origin = self._origin_ns.get(sample.client_id)
            if origin is not None:
                window = (sample.monotonic_ns - origin) / 1_000_000_000
                average_rx = average_throughput_bps(session_rx_bytes, window)
                average_tx = average_throughput_bps(session_tx_bytes, window)
        return TrafficMetrics(
            client_id=sample.client_id,
            ip_address=sample.ip_address,
            interface=sample.interface,
            timestamp=sample.timestamp,
            rx_bytes=sample.rx_bytes,
            tx_bytes=sample.tx_bytes,
            rx_packets=sample.rx_packets,
            tx_packets=sample.tx_packets,
            total_bytes=sample.rx_bytes + sample.tx_bytes,
            interval_seconds=interval_seconds,
            instantaneous_rx_bps=rx_bps,
            instantaneous_tx_bps=tx_bps,
            average_rx_bps=average_rx,
            average_tx_bps=average_tx,
            rx_utilization_percent=direction_utilization_percent(rx_bps, capacity_bps),
            tx_utilization_percent=direction_utilization_percent(tx_bps, capacity_bps),
            utilization_percent=link_utilization_percent(rx_bps, tx_bps, capacity_bps),
            session_rx_bytes=session_rx_bytes,
            session_tx_bytes=session_tx_bytes,
        )
