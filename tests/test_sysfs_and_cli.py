"""Sysfs parsing, namespace command construction, and the live-table formatter."""

import subprocess
from pathlib import Path

import pytest

from bandwidth_manager.monitoring.cli import format_table
from bandwidth_manager.monitoring.config import load_monitor_config
from bandwidth_manager.monitoring.linux_source import LinuxInterfaceSource, sysfs_cat_argv
from bandwidth_manager.monitoring.models import TrafficMetrics
from bandwidth_manager.monitoring.sysfs import InvalidMeasurement, SysfsStatisticsReader
from datetime import UTC, datetime


def _write_stats(root: Path, interface: str, rx: str, tx: str, rx_packets: str, tx_packets: str) -> None:
    directory = root / "class" / "net" / interface / "statistics"
    directory.mkdir(parents=True)
    (directory / "rx_bytes").write_text(rx, encoding="ascii")
    (directory / "tx_bytes").write_text(tx, encoding="ascii")
    (directory / "rx_packets").write_text(rx_packets, encoding="ascii")
    (directory / "tx_packets").write_text(tx_packets, encoding="ascii")


def test_sysfs_reader_uses_the_statistics_files(tmp_path: Path):
    _write_stats(tmp_path, "bwv-a", "1000\n", "40\n", "7\n", "2\n")
    counters = SysfsStatisticsReader(tmp_path).read("bwv-a")
    assert counters is not None
    assert counters.rx_bytes == 1000
    assert counters.tx_bytes == 40
    assert counters.rx_packets == 7
    assert counters.tx_packets == 2


def test_missing_interface_is_not_reported_as_zero(tmp_path: Path):
    assert SysfsStatisticsReader(tmp_path).read("bwv-a") is None


def test_garbage_counter_file_is_invalid(tmp_path: Path):
    _write_stats(tmp_path, "bwv-a", "nope\n", "1\n", "1\n", "1\n")
    with pytest.raises(InvalidMeasurement):
        SysfsStatisticsReader(tmp_path).read("bwv-a")


def test_interface_name_cannot_escape_the_sysfs_tree(tmp_path: Path):
    with pytest.raises(ValueError):
        SysfsStatisticsReader(tmp_path).read("../secrets")


def test_namespace_read_builds_a_fixed_argv_and_parses_cat_output():
    argv = sysfs_cat_argv("bw-client-a", "bwv-a")
    assert argv == [
        "ip",
        "netns",
        "exec",
        "bw-client-a",
        "cat",
        "/sys/class/net/bwv-a/statistics/rx_bytes",
        "/sys/class/net/bwv-a/statistics/tx_bytes",
        "/sys/class/net/bwv-a/statistics/rx_packets",
        "/sys/class/net/bwv-a/statistics/tx_packets",
    ]
    assert "shell" not in argv

    def runner(command):
        assert list(command) == argv
        return subprocess.CompletedProcess(command, 0, stdout="8\n3\n2\n1\n", stderr="")

    counters = LinuxInterfaceSource(runner=runner).read_counters(
        namespace="bw-client-a",
        interface="bwv-a",
    )
    assert counters is not None
    assert (counters.rx_bytes, counters.tx_bytes, counters.rx_packets, counters.tx_packets) == (8, 3, 2, 1)


def test_failed_namespace_read_means_the_interface_is_gone():
    def runner(command):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such file")

    counters = LinuxInterfaceSource(runner=runner).read_counters(
        namespace="bw-client-b",
        interface="bwv-b",
    )
    assert counters is None


def test_namespace_command_failure_is_not_treated_as_zero_traffic():
    def runner(command):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")

    with pytest.raises(InvalidMeasurement):
        LinuxInterfaceSource(runner=runner).read_counters(
            namespace="bw-client-b",
            interface="bwv-b",
        )


@pytest.mark.parametrize(
    ("namespace", "interface"),
    [
        ("bw-client-a", "bwv-a;rm"),
        ("../bw-client-a", "bwv-a"),
        ("bw client", "bwv-a"),
        ("bw-client-a", "eth0/../../x"),
    ],
)
def test_namespace_and_interface_names_are_rejected_before_a_command_is_built(namespace, interface):
    with pytest.raises(ValueError):
        sysfs_cat_argv(namespace, interface)


def test_example_config_matches_the_lab_clients():
    config = load_monitor_config(Path("config/monitor.example.yaml"))
    assert config.link_capacity_bps is None
    assert [(client.id, str(client.ipv4), client.namespace, client.interface) for client in config.clients] == [
        ("client-a", "192.168.100.11", "bw-client-a", "bwv-a"),
        ("client-b", "192.168.100.12", "bw-client-b", "bwv-b"),
        ("client-c", "192.168.100.13", "bw-client-c", "bwv-c"),
    ]


def test_table_prints_a_dash_when_the_rate_is_unknown():
    row = TrafficMetrics(
        client_id="client-a",
        ip_address="192.168.100.11",
        interface="bwv-a",
        timestamp=datetime(2026, 10, 6, tzinfo=UTC),
        rx_bytes=1000,
        tx_bytes=250,
        rx_packets=1,
        tx_packets=1,
        total_bytes=1250,
    )
    rendered = format_table([row])
    assert "client-a" in rendered
    assert "192.168.100.11" in rendered
    assert "1250" in rendered
    assert "-" in rendered
    assert "0.000" not in rendered
