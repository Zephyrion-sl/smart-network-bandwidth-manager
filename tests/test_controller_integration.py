"""Control loop over the real monitor, allocator, congestion check, and tc builder.

Counter values stand in for the lab veth statistics. The tc runner records
commands for bw-gw and does not touch a host interface. Live iperf is not
required for these decisions.
"""

import subprocess
from pathlib import Path

from bandwidth_manager.controller.controller import BandwidthController, ControlConfig
from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.manager import manager_from_config
from bandwidth_manager.monitoring.collector import TrafficCollector
from bandwidth_manager.monitoring.service import MonitoringService
from tests.support import ManualClock, ScriptedSource, counters, endpoint

CAPACITY = 30_000_000
MBPS = 1_000_000


def _bytes(bps: int) -> int:
    return bps // 8


def _lab_stack(series: dict[str, list], config: ControlConfig):
    clock = ManualClock()
    clients = [
        ("client-a", "192.168.100.11", "bwv-a"),
        ("client-b", "192.168.100.12", "bwv-b"),
        ("client-c", "192.168.100.13", "bwv-c"),
    ]
    endpoints = [endpoint(client_id, ip, interface) for client_id, ip, interface in clients if client_id in series]
    monitor = MonitoringService(
        TrafficCollector(ScriptedSource(series), monotonic_ns=clock.monotonic_ns, wall_time=clock.wall),
        endpoints,
    )
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    qos = manager_from_config(load_qos_config(Path("config/qos.example.yaml")), runner=runner, dry_run=False)
    scheduler = {"now": 0.0}
    controller = BandwidthController(
        config,
        monitor,
        qos,
        monotonic=lambda: scheduler["now"],
        wall_time=clock.wall,
    )
    return controller, clock, scheduler, calls


def _config(**overrides) -> ControlConfig:
    values = {
        "sampling_interval": 1.0,
        "congestion_threshold": 0.8,
        "total_capacity": CAPACITY,
        "minimum_bandwidth": 0,
        "maximum_bandwidth": None,
        "weights": {"client-a": 1, "client-b": 1, "client-c": 1},
        "cooldown": 10.0,
        "clear_margin": 0.1,
        "change_tolerance_bps": 1_000_000,
        "idle_bps": 10_000,
    }
    values.update(overrides)
    return ControlConfig(**values)


def _cumulative(steps: list[tuple[int, int, int]]) -> dict[str, list]:
    totals = {"client-a": 0, "client-b": 0, "client-c": 0}
    series = {client_id: [counters(0, 0)] for client_id in totals}
    for a_bps, b_bps, c_bps in steps:
        totals["client-a"] += _bytes(a_bps)
        totals["client-b"] += _bytes(b_bps)
        totals["client-c"] += _bytes(c_bps)
        for client_id in totals:
            series[client_id].append(counters(0, totals[client_id]))
    return series


def test_control_loop_on_the_lab_clients(caplog):
    # One second per sample. Loads are tx bits per second on the client veth.
    series = _cumulative(
        [
            (1 * MBPS, 1 * MBPS, 1 * MBPS),  # normal: 3 Mbps of 30
            (1 * MBPS, 40 * MBPS, 1 * MBPS),  # congested, client-b is heavy
            (1 * MBPS, 40 * MBPS, 1 * MBPS),  # same share, must not reinstall tc
            (0, 0, 0),  # utilization collapses, cooldown still holds the limits
            (0, 0, 0),  # cooldown has expired, limits come off
        ]
    )
    controller, clock, scheduler, calls = _lab_stack(series, _config())

    def tick_at(second: float):
        clock.ns += 1_000_000_000
        scheduler["now"] = second
        return controller.tick()

    with caplog.at_level("INFO"):
        baseline = controller.tick()
        normal = tick_at(1)
        after_normal = len(calls)
        congested = tick_at(2)
        after_apply = len(calls)
        held = tick_at(3)
        after_hold = len(calls)
        cooling = tick_at(4)
        after_cooldown = len(calls)
        scheduler["now"] = 12
        clock.ns += 1_000_000_000
        recovered = controller.tick()

    assert baseline.reason == "insufficient_samples"
    assert baseline.applied is False

    assert normal.reason == "not_congested"
    assert normal.congestion_state is False
    assert normal.observed_utilization == 0.1
    assert after_normal == 0

    assert congested.reason == "applied"
    assert congested.congestion_state is True
    assert congested.applied is True
    assert congested.previous_allocation == {}
    # client-b measured 40 Mbps and is cut back; a and c keep 1 Mbps.
    assert congested.new_allocation == {
        "client-a": 1_000_000,
        "client-b": 28_000_000,
        "client-c": 1_000_000,
    }
    enforced = [" ".join(command) for command in calls]
    assert any(command.startswith("ip netns exec bw-gw tc ") and "dev bwv-gw-b" in command for command in enforced)
    assert any("rate 28000000bit ceil 28000000bit" in command for command in enforced)
    assert any("match ip src 192.168.100.12/32" in command for command in enforced)
    assert all("dev eth0" not in command for command in enforced)
    assert held.reason == "unchanged"
    assert held.applied is False
    assert after_hold == after_apply

    assert cooling.reason == "cooldown"
    assert cooling.congestion_state is False
    assert cooling.applied is False
    assert after_cooldown == after_apply

    assert recovered.reason == "cleared"
    assert recovered.applied is True
    assert recovered.new_allocation == {}
    assert any("qdisc" in command and "del" in command for command in calls[after_apply:])
    assert "observed_utilization" in caplog.text
    assert "congestion_state" in caplog.text


def test_threshold_wobble_does_not_install_and_remove_limits():
    series = _cumulative(
        [
            (0, 79 * MBPS, 0),
            (0, 81 * MBPS, 0),
            (0, 75 * MBPS, 0),
        ]
    )
    controller, clock, scheduler, calls = _lab_stack(
        series,
        _config(total_capacity=100 * MBPS, cooldown=30, change_tolerance_bps=1_000_000),
    )

    controller.tick()
    clock.ns += 1_000_000_000
    scheduler["now"] = 1
    below = controller.tick()
    clock.ns += 1_000_000_000
    scheduler["now"] = 2
    entered = controller.tick()
    clock.ns += 1_000_000_000
    scheduler["now"] = 3
    dipped = controller.tick()

    assert below.reason == "not_congested"
    assert below.observed_utilization == 0.79
    assert entered.reason == "applied"
    assert entered.congestion_state is True
    assert dipped.congestion_state is True
    assert dipped.reason == "cooldown"
    assert dipped.applied is False
    text = "\n".join(" ".join(command) for command in calls)
    assert "75000000bit" not in text
    assert "qdisc del" not in text
    assert "81000000bit" in text
