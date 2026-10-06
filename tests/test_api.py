"""Control-plane API tests. The monitor, allocator, and tc builder are the real ones."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from bandwidth_manager.allocation.allocator import allocate
from bandwidth_manager.api.app import create_app
from bandwidth_manager.api.catalog import ClientIdentity
from bandwidth_manager.api.runtime import assemble
from bandwidth_manager.api.settings import Settings
from bandwidth_manager.controller.controller import ControlConfig
from bandwidth_manager.controller.policy import client_requests
from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.manager import manager_from_config
from bandwidth_manager.monitoring.collector import ClientEndpoint, TrafficCollector
from bandwidth_manager.monitoring.service import MonitoringService
from tests.support import ManualClock, ScriptedSource, counters

ROOT = Path(__file__).resolve().parents[1]
MBPS = 1_000_000
CATALOG = (
    ClientIdentity("client-a", "192.168.100.11", None, "bwv-a", "bw-client-a"),
    ClientIdentity("client-b", "192.168.100.12", None, "bwv-b", "bw-client-b"),
    ClientIdentity("client-c", "192.168.100.13", None, "bwv-c", "bw-client-c"),
)


def _series(steps: list[tuple[int, int, int]]) -> dict[str, list]:
    names = ("client-a", "client-b", "client-c")
    totals = [0, 0, 0]
    series: dict[str, list] = {name: [counters(0, 0)] for name in names}
    for step in steps:
        for index, bps in enumerate(step):
            totals[index] += bps // 8
            series[names[index]].append(counters(0, totals[index]))
    return series


def _expected(config: ControlConfig, loads: dict[str, int]) -> dict[str, int]:
    result = allocate(
        config.total_capacity,
        client_requests(
            loads,
            weights=config.weights,
            minimum_bandwidth=config.minimum_bandwidth,
            maximum_bandwidth=config.maximum_bandwidth,
            idle_bps=config.idle_bps,
            minimum_overrides=config.client_minimums,
            maximum_overrides=config.client_maximums,
        ),
    )
    return {
        row.client_id: row.allocated_bandwidth
        for row in result.allocations
        if row.allocated_bandwidth > 0
    }


def lab_client(
    steps: list[tuple[int, int, int]],
    *,
    capacity: int = 30 * MBPS,
    cooldown: float = 0.0,
    runner_code: int = 0,
    cors: tuple[str, ...] = (),
    series: dict[str, list] | None = None,
    show_stdout: str = "",
):
    clock = ManualClock()
    source = series if series is not None else _series(steps)
    endpoints = [
        ClientEndpoint(
            client_id=client.client_id,
            ip_address=client.ip,
            interface=client.interface,
            namespace=client.namespace,
        )
        for client in CATALOG
    ]
    monitor = MonitoringService(
        TrafficCollector(ScriptedSource(source), monotonic_ns=clock.monotonic_ns, wall_time=clock.wall),
        endpoints,
    )
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        detail = "" if runner_code == 0 else "tc failed"
        stdout = show_stdout if "show" in argv else ""
        return subprocess.CompletedProcess(list(argv), runner_code, stdout, detail)

    qos = manager_from_config(
        load_qos_config(ROOT / "config" / "qos.example.yaml"),
        runner=runner,
        dry_run=False,
    )
    config = ControlConfig(
        sampling_interval=1.0,
        congestion_threshold=0.8,
        total_capacity=capacity,
        minimum_bandwidth=0,
        maximum_bandwidth=None,
        weights={"client-a": 1, "client-b": 1, "client-c": 1},
        cooldown=cooldown,
        clear_margin=0.1,
        change_tolerance_bps=MBPS,
        idle_bps=10_000,
    )
    scheduler = {"now": 0.0}
    settings = Settings(database_path=":memory:", cors_origins=cors, max_events=50, max_allocation_history=20)
    runtime = assemble(
        settings=settings,
        control=config,
        monitor=monitor,
        qos=qos,
        catalog=CATALOG,
        monotonic=lambda: scheduler["now"],
        wall_time=clock.wall,
        dry_run=False,
    )
    return TestClient(create_app(runtime)), clock, scheduler, calls, config, runtime


def _advance(clock: ManualClock) -> None:
    clock.ns += 1_000_000_000


def _policy_body(**overrides) -> dict:
    body = {
        "congestion_threshold": 0.8,
        "clear_margin": 0.1,
        "total_capacity": 30 * MBPS,
        "minimum_bandwidth": 0,
        "maximum_bandwidth": None,
        "cooldown": 0,
        "change_tolerance_bps": MBPS,
        "weights": {"client-a": 1, "client-b": 1, "client-c": 1},
    }
    body.update(overrides)
    return body


def test_dashboard_is_served_without_touching_enforcement():
    client, *_ = lab_client([])
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Observed" in page.text
    assert "Allocated" in page.text
    assert "Enforced" in page.text
    script = client.get("/static/dashboard.js")
    assert script.status_code == 200
    assert "enforced === true" in script.text
    assert client.get("/health").json()["status"] == "ok"


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("BW_DATABASE_PATH", ":memory:")
    monkeypatch.setenv("BW_CORS_ORIGINS", "http://a, http://b")
    monkeypatch.setenv("BW_PORT", "9000")
    settings = Settings.from_env()
    assert settings.database_path == ":memory:"
    assert settings.cors_origins == ("http://a", "http://b")
    assert settings.port == 9000
    assert settings.monitor_config is None


def test_health_clients_openapi_and_unknown_client():
    client, *_ = lab_client([])
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok", "dry_run": False}
    listed = client.get("/clients").json()["clients"]
    assert [row["client_id"] for row in listed] == ["client-a", "client-b", "client-c"]
    assert listed[0]["ip"] == "192.168.100.11"
    assert listed[0]["mac"] is None
    assert listed[0]["connection_status"] == "unknown"
    assert listed[0]["allocation_bps"] is None
    assert listed[0]["policy"]["weight"] == 1
    missing = client.get("/clients/client-z")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    invalid = client.get("/clients/bad id")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    for path in (
        "/health",
        "/clients",
        "/clients/{client_id}",
        "/metrics",
        "/allocations",
        "/policies",
        "/policies/{client_id}",
        "/qos/apply",
        "/qos/clear",
        "/events",
    ):
        assert path in paths
    assert client.get("/docs").status_code == 200
    assert not any(getattr(item, "cls", None) is CORSMiddleware for item in client.app.user_middleware)


def test_cors_is_installed_only_when_configured():
    client, *_ = lab_client([], cors=("http://127.0.0.1:3000",))
    assert any(getattr(item, "cls", None) is CORSMiddleware for item in client.app.user_middleware)


def test_policy_validation_and_storage(caplog):
    client, _, _, _, _, runtime = lab_client([])
    assert client.post("/policies", json={"congestion_threshold": 2}).status_code == 422
    assert client.post("/policies", json=_policy_body(clear_margin=0.8)).status_code == 422
    assert client.post("/policies", json=_policy_body(weights={"client-a": 1})).status_code == 422
    assert client.post("/policies", json=_policy_body(surprise=True)).status_code == 422
    assert client.put("/policies/client-z", json={"weight": 2}).status_code == 404
    assert client.put("/policies/client-a", json={"weight": 0}).status_code == 422
    too_tight = client.put(
        "/policies/client-a",
        json={"weight": 1, "min_bandwidth": 50, "max_bandwidth": 10},
    )
    assert too_tight.status_code == 422
    assert too_tight.json()["error"]["code"] == "validation_error"
    assert client.get("/events").json()["events"] == []
    with caplog.at_level("INFO"):
        updated = client.post("/policies", json=_policy_body(total_capacity=50 * MBPS, weights={
            "client-a": 1,
            "client-b": 2,
            "client-c": 1,
        }))
    assert updated.status_code == 200
    assert updated.json()["total_capacity"] == 50 * MBPS
    assert runtime.database.load_policy()["global"]["total_capacity"] == 50 * MBPS
    narrowed = client.put(
        "/policies/client-a",
        json={"weight": 4, "min_bandwidth": 1000, "max_bandwidth": None},
    )
    assert narrowed.status_code == 200
    client_a = next(item for item in narrowed.json()["clients"] if item["client_id"] == "client-a")
    assert client_a["weight"] == 4
    assert client_a["min_bandwidth"] == 1000
    assert any(record.message.startswith("policy") for record in caplog.records)


def test_metrics_aggregate_usage_and_mark_the_client_online():
    client, clock, *_ = lab_client([(MBPS, 0, 0)])
    first = client.get("/metrics")
    assert first.status_code == 200
    assert first.json()["clients"][0]["rx_bps"] is None
    _advance(clock)
    second = client.get("/metrics")
    usage = [row for row in second.json()["usage"] if row["client_id"] == "client-a"]
    assert len(usage) == 1
    assert usage[0]["samples"] == 2
    assert usage[0]["tx_bytes"] == MBPS // 8
    assert usage[0]["avg_tx_bps"] == MBPS
    detail = client.get("/clients/client-a").json()
    assert detail["connection_status"] == "online"
    assert detail["current_bandwidth"]["tx_bps"] == MBPS
    assert detail["usage_bytes"] == MBPS // 8
    assert len(detail["usage"]) == 1


def test_offline_when_the_interface_disappears():
    series = {
        "client-a": [counters(0, 0), counters(0, 0)],
        "client-b": [counters(0, 0), counters(0, 0)],
        "client-c": [counters(0, 0), None],
    }
    client, clock, *_ = lab_client([], series=series)
    client.get("/metrics")
    _advance(clock)
    client.get("/metrics")
    assert client.get("/clients/client-c").json()["connection_status"] == "offline"
    assert client.get("/clients/client-a").json()["connection_status"] == "online"


def test_normal_traffic_does_not_install_tc():
    client, clock, _, calls, *_ = lab_client([(MBPS, MBPS, MBPS)])
    baseline = client.post("/qos/apply")
    assert baseline.status_code == 200
    assert baseline.json()["reason"] == "insufficient_samples"
    assert calls == []
    _advance(clock)
    decision = client.post("/qos/apply")
    assert decision.status_code == 200
    body = decision.json()
    assert body["reason"] == "not_congested"
    assert body["observed_utilization"] == 0.1
    assert body["enforced"] is False
    assert body["commands"] == []
    assert calls == []
    assert client.get("/allocations").json()["allocations"] == []


def test_congestion_applies_tc_once_and_skips_the_same_share():
    heavy = (MBPS, 40 * MBPS, MBPS)
    client, clock, _, calls, config, _runtime = lab_client([heavy, heavy])
    assert client.post("/qos/apply").json()["reason"] == "insufficient_samples"
    _advance(clock)
    applied = client.post("/qos/apply")
    assert applied.status_code == 200
    body = applied.json()
    assert body["reason"] == "applied"
    assert body["enforced"] is True
    assert body["new_allocation"] == _expected(
        config,
        {"client-a": MBPS, "client-b": 40 * MBPS, "client-c": MBPS},
    )
    assert body["new_allocation"]["client-b"] == 28 * MBPS
    text = "\n".join(body["commands"])
    assert "ip netns exec bw-gw tc " in text
    assert "dev bwv-gw-b" in text
    assert "rate 28000000bit ceil 28000000bit" in text
    assert "match ip src 192.168.100.12/32" in text
    assert "eth0" not in text
    installed = len(calls)
    _advance(clock)
    again = client.post("/qos/apply")
    assert again.status_code == 200
    assert again.json()["reason"] == "unchanged"
    assert again.json()["applied"] is False
    assert len(calls) == installed
    current = {
        row["client_id"]: row["allocated_bandwidth"]
        for row in client.get("/allocations").json()["allocations"]
    }
    assert current == body["new_allocation"]


def test_weight_and_minimum_change_the_allocation():
    full = (100 * MBPS, 100 * MBPS, 100 * MBPS)
    client, clock, _, calls, config, _runtime = lab_client([full, full], capacity=100 * MBPS)
    client.post("/qos/apply")
    _advance(clock)
    equal = client.post("/qos/apply").json()["new_allocation"]
    assert client.put("/policies/client-b", json={"weight": 3}).status_code == 200
    _advance(clock)
    weighted = client.post("/qos/apply").json()
    assert weighted["reason"] == "applied"
    assert weighted["new_allocation"] == _expected(config, {name: 100 * MBPS for name in ("client-a", "client-b", "client-c")})
    assert weighted["new_allocation"]["client-b"] > equal["client-b"]
    assert weighted["new_allocation"]["client-b"] > weighted["new_allocation"]["client-a"]
    assert any("dev bwv-gw-b" in " ".join(command) for command in calls)

    client_min, clock_min, _, _, config_min, _runtime = lab_client(
        [(30 * MBPS, 30 * MBPS, 30 * MBPS)],
        capacity=30 * MBPS,
    )
    client_min.post("/qos/apply")
    assert client_min.put(
        "/policies/client-a",
        json={"weight": 1, "min_bandwidth": 20 * MBPS, "max_bandwidth": None},
    ).status_code == 200
    _advance(clock_min)
    limited = client_min.post("/qos/apply").json()
    loads = {"client-a": 30 * MBPS, "client-b": 30 * MBPS, "client-c": 30 * MBPS}
    assert limited["new_allocation"] == _expected(config_min, loads)
    assert limited["new_allocation"]["client-a"] >= 20 * MBPS


def test_clear_then_cooldown_blocks_immediate_reinstall():
    heavy = (MBPS, 40 * MBPS, MBPS)
    client, clock, scheduler, calls, _config, _runtime = lab_client([heavy, heavy, heavy], cooldown=10)
    client.post("/qos/apply")
    _advance(clock)
    assert client.post("/qos/apply").json()["reason"] == "applied"
    cleared = client.post("/qos/clear")
    assert cleared.status_code == 200
    assert cleared.json()["enforced"] is True
    assert cleared.json()["reason"] == "cleared"
    assert any("qdisc" in command and "del" in command for command in calls)
    assert client.get("/allocations").json()["allocations"] == []
    after_clear = len(calls)
    _advance(clock)
    blocked = client.post("/qos/apply")
    assert blocked.status_code == 200
    assert blocked.json()["reason"] == "cooldown"
    assert len(calls) == after_clear
    scheduler["now"] = 10
    _advance(clock)
    restored = client.post("/qos/apply")
    assert restored.status_code == 200
    assert restored.json()["reason"] == "applied"
    assert len(calls) > after_clear


def test_tc_failure_is_not_reported_as_enforced():
    client, clock, _, _, _, runtime = lab_client([(MBPS, 40 * MBPS, MBPS)], runner_code=1)
    client.post("/qos/apply")
    _advance(clock)
    failed = client.post("/qos/apply")
    assert failed.status_code == 409
    body = failed.json()["error"]
    assert body["code"] == "not_enforced"
    assert body["details"]["reason"] == "enforcement_failed"
    assert body["details"]["enforced"] is False
    assert runtime.database.current_allocation() == []
    kinds = [event["kind"] for event in client.get("/events").json()["events"]]
    assert "control_decision" in kinds


def test_events_limit_validation_and_internal_errors(monkeypatch):
    client, _, _, _, _, runtime = lab_client([])
    assert client.get("/events", params={"limit": 0}).status_code == 422

    def explode():
        raise RuntimeError("secret traceback")

    monkeypatch.setattr(runtime.plane, "health", explode)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    failed = quiet.get("/health")
    assert failed.status_code == 500
    assert failed.json()["error"] == {
        "code": "internal_error",
        "message": "internal error",
        "details": None,
    }
    assert "secret" not in failed.text


def test_saved_allocation_is_not_shown_as_enforced():
    client, *_rest, runtime = lab_client([])
    runtime.database.replace_allocation(
        {"client-b": 20 * MBPS},
        decided_at="2026-10-06T00:00:00+00:00",
        reason="applied",
        applied=True,
        utilization=1.0,
        congested=True,
    )
    listed = client.get("/clients").json()["clients"]
    assert all(row["allocation_bps"] is None for row in listed)
    body = client.get("/allocations").json()
    assert body["allocations"] == []
    assert body["history"][0]["allocation"]["client-b"] == 20 * MBPS


def test_kernel_class_show_overrides_the_remembered_share():
    heavy = (MBPS, 40 * MBPS, MBPS)
    shown = '[{"kind":"htb","handle":"1:20","options":{"rate":12300000}}]'
    client, clock, *_rest = lab_client([heavy, heavy], show_stdout=shown)
    client.post("/qos/apply")
    _advance(clock)
    assert client.post("/qos/apply").json()["enforced"] is True
    current = {
        row["client_id"]: row["allocated_bandwidth"]
        for row in client.get("/allocations").json()["allocations"]
    }
    assert current == {"client-b": 12_300_000}
    row = next(item for item in client.get("/clients").json()["clients"] if item["client_id"] == "client-b")
    assert row["allocation_bps"] == 12_300_000
