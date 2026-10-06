"""Congestion is utilization compared with a threshold."""

import pytest

from bandwidth_manager.allocation import detect_congestion, utilization


def test_utilization_is_used_bandwidth_over_capacity():
    assert utilization(25, 100) == pytest.approx(0.25)
    assert utilization(0, 100) == 0.0
    assert utilization(150, 100) == pytest.approx(1.5)


def test_below_the_threshold_is_not_congested():
    decision = detect_congestion(79, 100, 0.8)
    assert decision.utilization == pytest.approx(0.79)
    assert decision.congested is False


def test_exactly_the_threshold_is_congested():
    decision = detect_congestion(8, 10, 0.8)
    assert decision.utilization == pytest.approx(0.8)
    assert decision.congested is True


def test_above_the_threshold_is_congested():
    decision = detect_congestion(90, 100, 0.8)
    assert decision.congested is True


def test_zero_use_is_not_congested_for_a_positive_threshold():
    assert detect_congestion(0, 100, 0.8).congested is False


def test_use_above_capacity_is_congested():
    decision = detect_congestion(120, 100, 1.0)
    assert decision.utilization == pytest.approx(1.2)
    assert decision.congested is True


def test_full_capacity_meets_a_threshold_of_one():
    assert detect_congestion(100, 100, 1.0).congested is True
    assert detect_congestion(99, 100, 1.0).congested is False


def test_threshold_of_zero_marks_an_idle_link_congested():
    assert detect_congestion(0, 100, 0).congested is True


def test_the_same_measurement_is_deterministic():
    assert detect_congestion(40, 50, 0.5) == detect_congestion(40, 50, 0.5)


@pytest.mark.parametrize(
    ("used", "capacity", "threshold"),
    [
        (-1, 100, 0.8),
        (10, 0, 0.8),
        (10, -5, 0.8),
        (10, 100, -0.1),
        (10, 100, 1.1),
        (10, 100, float("nan")),
    ],
)
def test_congestion_inputs_are_rejected(used, capacity, threshold):
    with pytest.raises((ValueError, TypeError)):
        detect_congestion(used, capacity, threshold)
