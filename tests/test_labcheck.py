"""The live-test judge must reject a ceiling that did not change goodput."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bandwidth_manager.labcheck import (
    CheckError,
    judge_allocation,
    judge_cap,
    judge_clients,
    judge_recovery,
    rates_close,
    throughput_bps,
)

MBPS = 1_000_000


def test_cap_passes_only_when_goodput_falls_toward_the_allocation():
    message = judge_cap(uncapped_bps=200 * MBPS, capped_bps=20 * MBPS, allocation_bps=20 * MBPS)
    assert message.startswith("PASS")


def test_cap_fails_when_throughput_does_not_drop():
    with pytest.raises(CheckError) as caught:
        judge_cap(uncapped_bps=200 * MBPS, capped_bps=190 * MBPS, allocation_bps=20 * MBPS)
    assert caught.value.status == 1


def test_cap_is_inconclusive_when_the_link_was_already_slow():
    with pytest.raises(CheckError) as caught:
        judge_cap(uncapped_bps=10 * MBPS, capped_bps=9 * MBPS, allocation_bps=20 * MBPS)
    assert caught.value.status == 2


def test_three_clients_need_at_least_one_fast_sender():
    with pytest.raises(CheckError) as caught:
        judge_clients(
            [
                {
                    "client_id": "client-a",
                    "uncapped_bps": 5 * MBPS,
                    "capped_bps": 4 * MBPS,
                    "allocation_bps": 33 * MBPS,
                }
            ]
        )
    assert caught.value.status == 2
    message = judge_clients(
        [
            {
                "client_id": "client-b",
                "uncapped_bps": 150 * MBPS,
                "capped_bps": 30 * MBPS,
                "allocation_bps": 33 * MBPS,
            }
        ]
    )
    assert "client-b" in message


def test_allocation_cannot_exceed_capacity():
    with pytest.raises(CheckError) as caught:
        judge_allocation(100 * MBPS, {"client-a": 60 * MBPS, "client-b": 50 * MBPS})
    assert caught.value.status == 1
    assert judge_allocation(100 * MBPS, {"client-a": 34 * MBPS, "client-b": 33 * MBPS, "client-c": 33 * MBPS}).startswith(
        "PASS"
    )


def test_recovery_must_rise_above_the_removed_ceiling():
    with pytest.raises(CheckError):
        judge_recovery(capped_bps=20 * MBPS, recovered_bps=18 * MBPS, allocation_bps=20 * MBPS)
    assert judge_recovery(capped_bps=20 * MBPS, recovered_bps=80 * MBPS, allocation_bps=20 * MBPS).startswith("PASS")


def test_monitor_rate_must_match_the_counter_delta():
    assert throughput_bps(0, 1_000_000, 1.0) == 8_000_000
    with pytest.raises(CheckError) as caught:
        rates_close(100 * MBPS, 10 * MBPS)
    assert caught.value.status == 1
    assert rates_close(100 * MBPS, 90 * MBPS).startswith("PASS")


def test_cli_does_not_pass_a_failed_ceiling():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bandwidth_manager.labcheck",
            "judge-cap",
            "--uncapped",
            str(200 * MBPS),
            "--capped",
            str(180 * MBPS),
            "--allocation",
            str(20 * MBPS),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "FAIL" in completed.stderr


def test_end_to_end_script_refuses_to_pass_without_live_tools():
    script = Path("scripts/test_end_to_end.sh").read_text(encoding="utf-8")
    assert "iperf3" in script
    assert "ENFORCEMENT NOT VERIFIED" in script
    assert "exit 2" in script
    assert "simulated" not in script.lower()
    # The pass line is the last claim and it requires the iperf judges above it.
    assert script.index("judge-clients") < script.index("live iperf goodput fell")
    rows = json.dumps([{"client_id": "client-a", "uncapped_bps": 1, "capped_bps": 1, "allocation_bps": 1}])
    completed = subprocess.run(
        [sys.executable, "-m", "bandwidth_manager.labcheck", "judge-clients", "--json", rows],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
