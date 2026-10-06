"""Rate math. These tests use counter values, not a network."""

import pytest

from bandwidth_manager.monitoring.calculate import (
    average_throughput_bps,
    direction_utilization_percent,
    link_utilization_percent,
    throughput_bps,
)


def test_throughput_from_byte_delta_and_elapsed_time():
    # (6000 - 1000) * 8 / 2 = 20000 bps
    assert throughput_bps(6000, 1000, 2.0) == pytest.approx(20_000.0)


def test_zero_byte_delta_is_a_real_zero_rate():
    assert throughput_bps(100, 100, 1.0) == 0.0


@pytest.mark.parametrize("elapsed", [0.0, -0.5])
def test_zero_or_negative_elapsed_time_has_no_throughput(elapsed):
    assert throughput_bps(100, 50, elapsed) is None


def test_counter_reset_has_no_throughput():
    assert throughput_bps(10, 5000, 1.0) is None


def test_negative_counter_is_rejected():
    assert throughput_bps(-1, 0, 1.0) is None
    assert throughput_bps(10, -1, 1.0) is None


def test_average_throughput_over_the_observation_window():
    assert average_throughput_bps(1_000, 2.0) == pytest.approx(4_000.0)


def test_average_throughput_needs_a_positive_window():
    assert average_throughput_bps(1_000, 0.0) is None


def test_utilization_uses_configured_capacity():
    assert direction_utilization_percent(5_000_000, 10_000_000) == pytest.approx(50.0)
    assert direction_utilization_percent(1_000_000, 10_000_000) == pytest.approx(10.0)
    # Full duplex: the busier direction, not the sum.
    assert link_utilization_percent(5_000_000, 1_000_000, 10_000_000) == pytest.approx(50.0)


def test_utilization_may_exceed_100_percent():
    assert direction_utilization_percent(15_000_000, 10_000_000) == pytest.approx(150.0)


def test_utilization_is_unknown_without_a_rate_or_capacity():
    assert direction_utilization_percent(None, 10_000_000) is None
    assert direction_utilization_percent(1_000, None) is None
    assert direction_utilization_percent(1_000, 0) is None
    assert link_utilization_percent(None, 1_000, 10_000_000) is None
    assert link_utilization_percent(1_000, None, 10_000_000) is None
