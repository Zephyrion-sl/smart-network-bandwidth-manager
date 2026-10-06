"""HTB command construction and the enforcement safety checks. No live tc."""

import json
import subprocess
from pathlib import Path

import pytest

from bandwidth_manager.allocation import Allocation
from bandwidth_manager.enforcement.commands import TcCommandBuilder
from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.executor import SafetyError, TcExecutor, run_command
from bandwidth_manager.enforcement.manager import manager_from_config
from bandwidth_manager.enforcement.models import TcCommand


def _config():
    return load_qos_config(Path("config/qos.example.yaml"))


def _runner_factory():
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    return calls, runner


def test_example_config_allowlist_is_only_the_lab_veths():
    config = _config()
    assert config.namespace == "bw-gw"
    assert config.allowlist() == frozenset({"bwv-gw-a", "bwv-gw-b", "bwv-gw-c", "bwv-gw-wan"})


def test_twenty_megabits_is_encoded_as_tc_bits():
    calls, runner = _runner_factory()
    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    result = manager.apply_limit("client-b", 20_000_000)

    assert result.enforced is True
    text = [" ".join(command) for command in result.commands]
    assert any("dev bwv-gw-b" in command and "rate 20000000bit ceil 20000000bit" in command for command in text)
    assert any("match ip dst 192.168.100.12/32 flowid 1:20" in command for command in text)
    assert any("dev bwv-gw-wan" in command and "match ip src 192.168.100.12/32 flowid 1:20" in command for command in text)
    assert all(command.startswith("ip netns exec bw-gw tc ") for command in text)
    assert calls


def test_allocation_installs_each_client_once():
    _calls, runner = _runner_factory()
    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    result = manager.apply_allocation(
        [
            Allocation("client-a", 20_000_000),
            Allocation("client-b", 50_000_000),
            Allocation("client-c", 30_000_000),
        ]
    )
    text = "\n".join(" ".join(command) for command in result.commands)
    assert result.enforced is True
    assert "rate 20000000bit ceil 20000000bit" in text
    assert "rate 50000000bit ceil 50000000bit" in text
    assert "rate 30000000bit ceil 30000000bit" in text
    assert "rate 101000000bit ceil 101000000bit" in text
    assert text.count("flowid 1:10") == 2
    assert text.count("flowid 1:20") == 2
    assert text.count("flowid 1:30") == 2
    assert "dev eth0" not in text


def test_applying_the_same_policy_twice_does_not_add_rules():
    calls, runner = _runner_factory()
    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    first = manager.apply_limit("client-a", 20_000_000)
    second = manager.apply_limit("client-a", 20_000_000)
    assert first.commands == second.commands
    midpoint = len(calls) // 2
    assert calls[:midpoint] == calls[midpoint:]
    wan_filters = [command for command in second.commands if "bwv-gw-wan" in command and "filter" in command]
    assert len(wan_filters) == 1


def test_remove_limit_drops_that_client_and_clear_deletes_root_qdiscs():
    calls, runner = _runner_factory()
    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    manager.apply_allocation({"client-a": 20_000_000, "client-b": 50_000_000})
    removed = manager.remove_limit("client-a")
    text = "\n".join(" ".join(command) for command in removed.commands)
    assert "192.168.100.11" not in text
    assert "192.168.100.12" in text

    def missing(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 2, "", "RTNETLINK answers: No such file or directory")

    manager._executor._runner = missing  # noqa: SLF001 — replace the runner for the cleanup response
    cleared = manager.clear_all_rules()
    assert cleared.enforced is True
    assert [command[4:9] for command in cleared.commands] == [
        ("tc", "qdisc", "del", "dev", interface)
        for interface in ("bwv-gw-a", "bwv-gw-b", "bwv-gw-c", "bwv-gw-wan")
    ]


def test_dry_run_does_not_execute_or_claim_enforcement():
    def fail_if_called(_argv):
        raise AssertionError("dry-run executed tc")

    manager = manager_from_config(_config(), runner=fail_if_called, dry_run=True)
    result = manager.apply_limit("client-c", 30_000_000)
    assert result.dry_run is True
    assert result.enforced is False
    assert result.commands


def test_executor_rejects_a_host_interface_and_shell_syntax():
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    executor = TcExecutor(allowlist=frozenset({"bwv-gw-a"}), namespace="bw-gw", runner=runner)
    with pytest.raises(SafetyError):
        executor.execute(
            [TcCommand(argv=("ip", "netns", "exec", "bw-gw", "tc", "qdisc", "del", "dev", "eth0", "root"))]
        )
    with pytest.raises(SafetyError):
        executor.execute([TcCommand(argv=("ip", "netns", "exec", "bw-gw", "tc", "qdisc", "del", "dev", "bwv-gw-a;rm", "root"))])
    with pytest.raises(SafetyError):
        executor.execute([TcCommand(argv=("tc", "qdisc", "del", "dev", "bwv-gw-a", "root"))])
    assert calls == []


def test_failed_apply_is_not_enforced_and_rolls_back():
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if any(arg == "50000000bit" for arg in argv):
            return subprocess.CompletedProcess(list(argv), 2, "", "RTNETLINK answers: Invalid argument")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    first = manager.apply_limit("client-a", 20_000_000)
    assert first.enforced is True
    second = manager.apply_limit("client-b", 50_000_000)
    assert second.enforced is False
    assert second.rolled_back is True
    assert any("del" in command and "qdisc" in command for command in calls)
    assert calls[-len(first.commands) :] == [list(command) for command in first.commands]


def test_builder_plan_is_deterministic():
    config = _config()
    builder = TcCommandBuilder(
        namespace=config.namespace,
        client_addresses={client.id: client.ipv4 for client in config.clients},
        download_interface={client.id: client.download_interface for client in config.clients},
        upload_interface={client.id: client.upload_interface for client in config.clients},
        class_minor={client.id: client.class_minor for client in config.clients},
        default_class_bps=config.default_class_bps,
    )
    policy = {"client-c": 30_000_000, "client-a": 20_000_000}
    assert builder.build(policy) == builder.build(dict(reversed(list(policy.items()))))


@pytest.mark.parametrize("bandwidth", [0, -1, 10_000_000_001, True])
def test_bandwidth_is_rejected(bandwidth):
    manager = manager_from_config(_config(), runner=lambda _argv: None, dry_run=True)
    with pytest.raises((TypeError, ValueError)):
        manager.apply_limit("client-a", bandwidth)


def test_unknown_client_is_rejected_before_tc():
    manager = manager_from_config(_config(), runner=lambda _argv: None, dry_run=True)
    with pytest.raises(ValueError):
        manager.apply_limit("eth0", 20_000_000)


def test_class_show_json_reports_the_kernel_rate():
    payload = json.dumps(
        [
            {"kind": "htb", "handle": "1:1", "options": {"rate": 100_000_000}},
            {"kind": "htb", "handle": "1:20", "options": {"rate": "28Mbit"}},
            {"kind": "htb", "handle": "1:999", "options": {"rate": 1_000_000}},
        ]
    )

    def runner(argv):
        stdout = payload if "show" in argv else ""
        return subprocess.CompletedProcess(list(argv), 0, stdout, "")

    manager = manager_from_config(_config(), runner=runner, dry_run=False)
    assert manager.read_installed() == {"client-b": 28_000_000}


def test_unparseable_class_show_does_not_invent_a_kernel_answer():
    manager = manager_from_config(
        _config(),
        runner=lambda argv: subprocess.CompletedProcess(list(argv), 0, "", ""),
        dry_run=False,
    )
    assert manager.read_installed() is None


def test_class_show_with_no_client_class_is_an_empty_policy():
    manager = manager_from_config(
        _config(),
        runner=lambda argv: subprocess.CompletedProcess(list(argv), 0, "[]", ""),
        dry_run=False,
    )
    assert manager.read_installed() == {}


def test_run_command_uses_an_argument_vector_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["shell"] = kwargs["shell"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("bandwidth_manager.enforcement.executor.shutil.which", lambda name: f"/sbin/{name}")
    monkeypatch.setattr("bandwidth_manager.enforcement.executor.subprocess.run", fake_run)
    run_command(["tc", "qdisc", "show"])
    assert seen["shell"] is False
    assert seen["argv"] == ["/sbin/tc", "qdisc", "show"]
