"""Baseline rules: first sample, resets, elapsed time, and independent clients."""

import asyncio
import threading

import pytest

from bandwidth_manager.monitoring.collector import TrafficCollector
from bandwidth_manager.monitoring.service import MonitoringService
from bandwidth_manager.monitoring.sysfs import InvalidMeasurement
from tests.support import ManualClock, ScriptedSource, counters, endpoint, service_for


def test_first_sample_has_counters_and_no_rate():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for({"client-a": [counters(1_000, 250, 4, 1)]}, [client], clock)

    metrics = service.sample_once()

    assert len(metrics) == 1
    row = metrics[0]
    assert row.rx_bytes == 1_000
    assert row.tx_bytes == 250
    assert row.rx_packets == 4
    assert row.tx_packets == 1
    assert row.total_bytes == 1_250
    assert row.instantaneous_rx_bps is None
    assert row.instantaneous_tx_bps is None
    assert row.average_rx_bps is None
    assert row.utilization_percent is None
    assert row.session_rx_bytes == 0
    assert row.interval_seconds is None


def test_second_sample_uses_counter_delta_over_monotonic_time():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {"client-a": [counters(1_000, 100), counters(6_000, 350)]},
        [client],
        clock,
        capacity=10_000_000,
    )

    service.sample_once()
    clock.ns += 2_000_000_000
    metrics = service.sample_once()[0]

    assert metrics.interval_seconds == pytest.approx(2.0)
    assert metrics.instantaneous_rx_bps == pytest.approx(20_000.0)
    assert metrics.instantaneous_tx_bps == pytest.approx(1_000.0)
    assert metrics.average_rx_bps == pytest.approx(20_000.0)
    assert metrics.rx_utilization_percent == pytest.approx(0.2)
    assert metrics.utilization_percent == pytest.approx(0.2)
    assert metrics.session_rx_bytes == 5_000
    assert metrics.total_bytes == 6_350


def test_zero_elapsed_time_does_not_advance_the_baseline(caplog):
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {"client-a": [counters(100), counters(150), counters(150)]},
        [client],
        clock,
    )

    service.sample_once()
    with caplog.at_level("ERROR"):
        stalled = service.sample_once()[0]
    assert stalled.instantaneous_rx_bps is None
    assert "non-positive elapsed time" in caplog.text

    clock.ns += 2_000_000_000
    resumed = service.sample_once()[0]
    # The bytes that arrived during the zero-length interval are counted once.
    assert resumed.instantaneous_rx_bps == pytest.approx(200.0)
    assert resumed.session_rx_bytes == 50


def test_counter_reset_does_not_produce_negative_throughput(caplog):
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {
            "client-a": [
                counters(1_000, 100),
                counters(5_000, 100),
                counters(100, 100),
                counters(1_100, 100),
            ]
        },
        [client],
        clock,
    )

    service.sample_once()
    clock.ns += 1_000_000_000
    service.sample_once()
    clock.ns += 1_000_000_000
    with caplog.at_level("ERROR"):
        reset = service.sample_once()[0]
    assert reset.instantaneous_rx_bps is None
    assert reset.instantaneous_tx_bps == pytest.approx(0.0)
    assert reset.utilization_percent is None
    assert reset.session_rx_bytes == 4_000
    assert "counter reset" in caplog.text

    clock.ns += 1_000_000_000
    recovered = service.sample_once()[0]
    assert recovered.instantaneous_rx_bps == pytest.approx(8_000.0)
    assert recovered.session_rx_bytes == 5_000


def test_multiple_clients_are_measured_independently():
    clock = ManualClock()
    clients = [
        endpoint("client-a", "192.168.100.11", "bwv-a"),
        endpoint("client-b", "192.168.100.12", "bwv-b"),
        endpoint("client-c", "192.168.100.13", "bwv-c"),
    ]
    service = service_for(
        {
            "client-a": [counters(0), counters(1_000)],
            "client-b": [counters(0), counters(500)],
            "client-c": [counters(50), counters(50)],
        },
        clients,
        clock,
        capacity=8_000,
    )

    service.sample_once()
    clock.ns += 1_000_000_000
    rows = {row.client_id: row for row in service.sample_once()}

    assert rows["client-a"].instantaneous_rx_bps == pytest.approx(8_000.0)
    assert rows["client-b"].instantaneous_rx_bps == pytest.approx(4_000.0)
    assert rows["client-c"].instantaneous_rx_bps == pytest.approx(0.0)
    assert rows["client-a"].utilization_percent == pytest.approx(100.0)
    assert rows["client-b"].utilization_percent == pytest.approx(50.0)
    assert rows["client-a"].session_rx_bytes == 1_000
    assert rows["client-b"].session_rx_bytes == 500


def test_disappeared_interface_is_a_new_baseline_when_it_returns(caplog):
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {"client-a": [counters(9_000), None, counters(10)]},
        [client],
        clock,
    )

    service.sample_once()
    clock.ns += 1_000_000_000
    with caplog.at_level("ERROR"):
        assert service.sample_once() == []
    assert "disappeared" in caplog.text

    clock.ns += 1_000_000_000
    returned = service.sample_once()[0]
    assert returned.instantaneous_rx_bps is None
    assert returned.rx_bytes == 10
    assert returned.session_rx_bytes == 0


def test_invalid_measurement_keeps_the_previous_baseline():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {
            "client-a": [
                counters(1_000),
                InvalidMeasurement("rx_bytes is not a counter"),
                counters(2_000),
            ]
        },
        [client],
        clock,
    )

    service.sample_once()
    clock.ns += 1_000_000_000
    assert service.sample_once() == []
    clock.ns += 1_000_000_000
    resumed = service.sample_once()[0]
    assert resumed.instantaneous_rx_bps == pytest.approx(4_000.0)
    assert resumed.interval_seconds == pytest.approx(2.0)


def test_periodic_sampler_waits_out_the_interval():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {"client-a": [counters(1), counters(1), counters(1)]},
        [client],
        clock,
    )

    class Scheduler:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.sleeps.append(delay)
            self.now += delay

    scheduler = Scheduler()
    stream = service.iter_samples(1.0, clock=scheduler.monotonic, sleep=scheduler.sleep)
    batches = [next(stream) for _ in range(3)]

    assert len(batches) == 3
    assert scheduler.sleeps == [1.0, 1.0]


def test_async_watch_yields_successive_samples():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    service = service_for(
        {"client-a": [counters(1), counters(1)]},
        [client],
        clock,
    )

    async def collect_two():
        stream = service.watch(0.01)
        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(collect_two())
    assert first[0].rx_bytes == 1
    assert second[0].rx_bytes == 1


def test_overlapping_samples_consume_each_counter_once():
    clock = ManualClock()
    client = endpoint("client-a", "192.168.100.11", "bwv-a")
    series = [counters(index * 1000) for index in range(40)]
    source = ScriptedSource({"client-a": series})
    service = MonitoringService(
        TrafficCollector(source, monotonic_ns=clock.monotonic_ns, wall_time=clock.wall),
        [client],
    )
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(10):
                service.sample_once()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert source.index["client-a"] == 40
