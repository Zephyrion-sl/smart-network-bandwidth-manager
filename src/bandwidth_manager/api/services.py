"""Service layer between HTTP routes and the monitor, controller, and QoS manager."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from bandwidth_manager.api.adapters import RecordingQos, SamplingMonitor
from bandwidth_manager.api.db import Database
from bandwidth_manager.api.errors import NotEnforcedError, NotFoundError, RejectionError
from bandwidth_manager.api.schemas import (
    AllocationDecision,
    AllocationItem,
    AllocationsResponse,
    ClientDetail,
    ClientList,
    ClientPolicyModel,
    ClientPolicyView,
    ClientPolicyWrite,
    ClientView,
    CurrentBandwidth,
    EventItem,
    EventList,
    HealthResponse,
    MetricSample,
    MetricsResponse,
    PolicyResponse,
    PolicyWrite,
    QosActionResponse,
    UsageBucket,
)
from bandwidth_manager.controller.controller import BandwidthController, ControlConfig

logger = logging.getLogger(__name__)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _bucket(row: dict[str, object]) -> UsageBucket:
    rx_samples = int(row["rx_rate_samples"])
    tx_samples = int(row["tx_rate_samples"])
    start = int(row["bucket_start"])
    return UsageBucket(
        client_id=str(row["client_id"]),
        bucket_start=datetime.fromtimestamp(start, UTC),
        rx_bytes=int(row["rx_bytes"]),
        tx_bytes=int(row["tx_bytes"]),
        samples=int(row["samples"]),
        avg_rx_bps=None if rx_samples == 0 else float(row["sum_rx_bps"]) / rx_samples,
        avg_tx_bps=None if tx_samples == 0 else float(row["sum_tx_bps"]) / tx_samples,
    )


def _enforced_share(controller: BandwidthController, qos: RecordingQos) -> dict[str, int]:
    """Kernel class rates when tc answered. Otherwise the share this process installed."""

    live = qos.read_installed()
    if live is not None:
        return live
    return controller.installed_allocation()


class ClientService:
    def __init__(
        self,
        database: Database,
        monitor: SamplingMonitor,
        controller: BandwidthController,
        qos: RecordingQos,
    ) -> None:
        self._db = database
        self._monitor = monitor
        self._controller = controller
        self._qos = qos

    def list_clients(self) -> ClientList:
        return ClientList(clients=[self._view(row) for row in self._db.list_clients()])

    def get_client(self, client_id: str) -> ClientDetail:
        row = self._db.get_client(client_id)
        if row is None:
            raise NotFoundError(f"unknown client: {client_id}")
        view = self._view(row)
        return ClientDetail(**view.model_dump(), usage=[_bucket(item) for item in self._db.usage(client_id)])

    def _view(self, row: dict[str, object]) -> ClientView:
        client_id = str(row["client_id"])
        policy = self._policy_for(client_id)
        sampled, latest = self._monitor.view()
        sample = latest.get(client_id)
        if not sampled:
            status = "unknown"
        elif sample is None:
            status = "offline"
        else:
            status = "online"
        installed = self._installed().get(client_id)
        buckets = self._db.usage(client_id)
        usage_bytes = sum(int(item["rx_bytes"]) + int(item["tx_bytes"]) for item in buckets)
        bandwidth = CurrentBandwidth()
        if sample is not None:
            bandwidth = CurrentBandwidth(
                rx_bps=sample.instantaneous_rx_bps,
                tx_bps=sample.instantaneous_tx_bps,
                utilization_percent=sample.utilization_percent,
            )
        return ClientView(
            client_id=client_id,
            ip=str(row["ipv4"]),
            mac=None if row["mac"] is None else str(row["mac"]),
            interface=str(row["interface"]),
            namespace=None if row["namespace"] is None else str(row["namespace"]),
            connection_status=status,  # type: ignore[arg-type]
            current_bandwidth=bandwidth,
            allocation_bps=installed,
            policy=policy,
            usage_bytes=usage_bytes,
        )

    def _policy_for(self, client_id: str) -> ClientPolicyView:
        stored = self._db.load_policy()
        if stored is None:
            return ClientPolicyView(weight=1, min_bandwidth=None, max_bandwidth=None)
        for item in stored["clients"]:
            if item["client_id"] == client_id:
                return ClientPolicyView(
                    weight=int(item["weight"]),
                    min_bandwidth=item["min_bandwidth"],
                    max_bandwidth=item["max_bandwidth"],
                )
        return ClientPolicyView(weight=1, min_bandwidth=None, max_bandwidth=None)

    def _installed(self) -> dict[str, int]:
        return _enforced_share(self._controller, self._qos)


class MetricsService:
    def __init__(self, monitor: SamplingMonitor, database: Database) -> None:
        self._monitor = monitor
        self._db = database

    def collect(self) -> MetricsResponse:
        rows = self._monitor.sample_once()
        when = self._monitor.last_sampled_at
        if when is None:
            raise RejectionError("monitor did not produce a timestamp")
        return MetricsResponse(
            sampled_at=when,
            clients=[
                MetricSample(
                    client_id=row.client_id,
                    ip=row.ip_address,
                    interface=row.interface,
                    rx_bps=row.instantaneous_rx_bps,
                    tx_bps=row.instantaneous_tx_bps,
                    utilization_percent=row.utilization_percent,
                    interval_seconds=row.interval_seconds,
                    total_bytes=row.total_bytes,
                )
                for row in rows
            ],
            usage=[_bucket(item) for item in self._db.usage()],
        )


class PolicyService:
    def __init__(
        self,
        database: Database,
        config: ControlConfig,
        known_clients: set[str],
        now: Callable[[], datetime],
        events: "EventService",
        controller: BandwidthController,
    ) -> None:
        self._db = database
        self._config = config
        self._known = known_clients
        self._now = now
        self._events = events
        self._controller = controller

    def seed_from_config(self) -> None:
        stored = self._db.load_policy()
        if stored is None:
            self._persist_global(self._view_from_config())
            return
        self._apply_stored(stored)

    def get_policy(self) -> PolicyResponse:
        return self._view_from_config()

    def replace_policy(self, body: PolicyWrite) -> PolicyResponse:
        with self._controller.exclusive():
            self._require_known_weights(body.weights)
            self._require_overrides_fit(body.minimum_bandwidth, body.maximum_bandwidth)
            self._config.congestion_threshold = body.congestion_threshold
            self._config.clear_margin = body.clear_margin
            self._config.total_capacity = body.total_capacity
            self._config.minimum_bandwidth = body.minimum_bandwidth
            self._config.maximum_bandwidth = body.maximum_bandwidth
            self._config.cooldown = body.cooldown
            self._config.change_tolerance_bps = body.change_tolerance_bps
            self._config.weights = dict(body.weights)
            view = self._view_from_config()
            self._persist_global(view)
        self._events.record("policy_updated", "replaced shared policy", view.model_dump(mode="json"))
        logger.info("policy %s", json.dumps(view.model_dump(mode="json"), sort_keys=True))
        return view

    def update_client(self, client_id: str, body: ClientPolicyWrite) -> PolicyResponse:
        if client_id not in self._known:
            raise NotFoundError(f"unknown client: {client_id}")
        with self._controller.exclusive():
            self._require_client_bounds(client_id, body.min_bandwidth, body.max_bandwidth)
            self._db.save_client_policy(
                client_id,
                weight=body.weight,
                min_bandwidth=body.min_bandwidth,
                max_bandwidth=body.max_bandwidth,
            )
            self._config.weights[client_id] = body.weight
            if body.min_bandwidth is None:
                self._config.client_minimums.pop(client_id, None)
            else:
                self._config.client_minimums[client_id] = body.min_bandwidth
            if body.max_bandwidth is None:
                self._config.client_maximums.pop(client_id, None)
            else:
                self._config.client_maximums[client_id] = body.max_bandwidth
            view = self._view_from_config()
        self._events.record(
            "client_policy_updated",
            f"updated policy for {client_id}",
            {"client_id": client_id, "policy": body.model_dump(mode="json")},
        )
        logger.info("client policy %s %s", client_id, json.dumps(body.model_dump(mode="json"), sort_keys=True))
        return view

    def _view_from_config(self) -> PolicyResponse:
        stored = self._db.load_policy()
        by_id = {}
        if stored is not None:
            by_id = {item["client_id"]: item for item in stored["clients"]}
        clients = []
        for client_id in sorted(self._config.weights):
            row = by_id.get(client_id, {})
            clients.append(
                ClientPolicyModel(
                    client_id=client_id,
                    weight=int(self._config.weights[client_id]),
                    min_bandwidth=row.get("min_bandwidth"),
                    max_bandwidth=row.get("max_bandwidth"),
                )
            )
        return PolicyResponse(
            congestion_threshold=self._config.congestion_threshold,
            clear_margin=self._config.clear_margin,
            total_capacity=self._config.total_capacity,
            minimum_bandwidth=self._config.minimum_bandwidth,
            maximum_bandwidth=self._config.maximum_bandwidth,
            cooldown=self._config.cooldown,
            change_tolerance_bps=self._config.change_tolerance_bps,
            sampling_interval=self._config.sampling_interval,
            idle_bps=self._config.idle_bps,
            clients=clients,
        )

    def _persist_global(self, view: PolicyResponse) -> None:
        self._db.save_policy(
            congestion_threshold=view.congestion_threshold,
            clear_margin=view.clear_margin,
            total_capacity=view.total_capacity,
            minimum_bandwidth=view.minimum_bandwidth,
            maximum_bandwidth=view.maximum_bandwidth,
            cooldown=view.cooldown,
            change_tolerance_bps=view.change_tolerance_bps,
            weights={item.client_id: item.weight for item in view.clients},
            updated_at=self._now().isoformat(),
        )

    def _apply_stored(self, stored: dict[str, object]) -> None:
        global_row = stored["global"]
        assert isinstance(global_row, dict)
        clients = stored["clients"]
        assert isinstance(clients, list)
        weights = {str(item["client_id"]): int(item["weight"]) for item in clients}
        self._require_known_weights(weights)
        minimum = int(global_row["minimum_bandwidth"])
        maximum = global_row["maximum_bandwidth"]
        maximum_value = None if maximum is None else int(maximum)
        self._config.congestion_threshold = float(global_row["congestion_threshold"])
        self._config.clear_margin = float(global_row["clear_margin"])
        self._config.total_capacity = int(global_row["total_capacity"])
        self._config.minimum_bandwidth = minimum
        self._config.maximum_bandwidth = maximum_value
        self._config.cooldown = float(global_row["cooldown"])
        self._config.change_tolerance_bps = int(global_row["change_tolerance_bps"])
        self._config.weights = weights
        self._config.client_minimums = {
            str(item["client_id"]): int(item["min_bandwidth"])
            for item in clients
            if item["min_bandwidth"] is not None
        }
        self._config.client_maximums = {
            str(item["client_id"]): int(item["max_bandwidth"])
            for item in clients
            if item["max_bandwidth"] is not None
        }

    def _require_known_weights(self, weights: dict[str, int]) -> None:
        if set(weights) != self._known:
            raise RejectionError("weights must name exactly the configured clients")

    def _require_overrides_fit(self, minimum: int, maximum: int | None) -> None:
        stored = self._db.load_policy()
        clients = [] if stored is None else stored["clients"]
        for item in clients:
            client_min = minimum if item["min_bandwidth"] is None else int(item["min_bandwidth"])
            if item["max_bandwidth"] is None:
                client_max = maximum
            else:
                client_max = int(item["max_bandwidth"])
            if client_max is not None and client_min > client_max:
                raise RejectionError(
                    f"minimum for {item['client_id']} exceeds its maximum after this policy change"
                )

    def _require_client_bounds(
        self,
        client_id: str,
        minimum: int | None,
        maximum: int | None,
    ) -> None:
        effective_min = self._config.minimum_bandwidth if minimum is None else minimum
        if maximum is None:
            effective_max = self._config.maximum_bandwidth
        else:
            effective_max = maximum
        if effective_max is not None and effective_min > effective_max:
            raise RejectionError(f"minimum for {client_id} exceeds its maximum")


class AllocationService:
    def __init__(self, database: Database, controller: BandwidthController, qos: RecordingQos) -> None:
        self._db = database
        self._controller = controller
        self._qos = qos

    def current(self) -> AllocationsResponse:
        congested, observed, _memory = self._controller.live_status()
        installed = _enforced_share(self._controller, self._qos)
        return AllocationsResponse(
            congested=congested,
            observed_utilization=observed,
            allocations=[
                AllocationItem(client_id=client_id, allocated_bandwidth=installed[client_id])
                for client_id in sorted(installed)
            ],
            history=[
                AllocationDecision(
                    decided_at=_parse_time(row["decided_at"]),
                    reason=row["reason"],
                    applied=row["applied"],
                    observed_utilization=row["utilization"],
                    congested=row["congested"],
                    allocation={str(key): int(value) for key, value in row["allocation"].items()},
                )
                for row in self._db.allocation_history(20)
            ],
        )


class QosService:
    def __init__(
        self,
        controller: BandwidthController,
        qos: RecordingQos,
        database: Database,
        events: "EventService",
        *,
        dry_run: bool,
    ) -> None:
        self._controller = controller
        self._qos = qos
        self._db = database
        self._events = events
        self._dry_run = dry_run

    def apply(self) -> QosActionResponse:
        self._qos.last = None
        decision = self._controller.tick()
        body = self._body(
            reason=decision.reason,
            applied=decision.applied,
            enforced=bool(self._qos.last and self._qos.last.enforced),
            congestion_state=decision.congestion_state,
            observed_utilization=decision.observed_utilization,
            previous=decision.previous_allocation,
            new=decision.new_allocation,
        )
        if decision.applied:
            self._db.replace_allocation(
                decision.new_allocation,
                decided_at=decision.timestamp.isoformat(),
                reason=decision.reason,
                applied=True,
                utilization=decision.observed_utilization,
                congested=decision.congestion_state,
            )
        self._finish("control_decision", body)
        return body

    def clear(self) -> QosActionResponse:
        previous = dict(self._controller.state.allocation)
        result = self._qos.clear_all_rules()
        observed = None
        if self._controller.state.decisions:
            observed = self._controller.state.decisions[-1].observed_utilization
        if result.enforced:
            self._controller.state.allocation = {}
            self._controller.state.congested = False
            self._db.replace_allocation(
                {},
                decided_at=self._events.now().isoformat(),
                reason="cleared",
                applied=True,
                utilization=observed,
                congested=False,
            )
        body = self._body(
            reason="cleared" if result.enforced else "enforcement_failed",
            applied=result.enforced,
            enforced=result.enforced,
            congestion_state=self._controller.state.congested,
            observed_utilization=observed,
            previous=previous,
            new={} if result.enforced else previous,
        )
        self._finish("qos_cleared", body)
        return body

    def _body(
        self,
        *,
        reason: str,
        applied: bool,
        enforced: bool,
        congestion_state: bool,
        observed_utilization: float | None,
        previous: dict[str, int],
        new: dict[str, int],
    ) -> QosActionResponse:
        result = self._qos.last
        return QosActionResponse(
            reason=reason,
            applied=applied,
            enforced=enforced,
            dry_run=self._dry_run,
            congestion_state=congestion_state,
            observed_utilization=observed_utilization,
            previous_allocation=dict(previous),
            new_allocation=dict(new),
            errors=[] if result is None else list(result.errors),
            commands=[] if result is None else [" ".join(command) for command in result.commands],
        )

    def _finish(self, kind: str, body: QosActionResponse) -> None:
        payload = body.model_dump(mode="json")
        payload.pop("commands", None)
        self._events.record(kind, body.reason, payload)
        logger.info("qos %s", json.dumps(payload, sort_keys=True))
        if body.reason == "enforcement_failed":
            message = "Linux did not accept the traffic-control update"
            if body.errors:
                message = body.errors[0]
            raise NotEnforcedError(message, payload)


class EventService:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self._db = database
        self._now = now

    def now(self) -> datetime:
        return self._now()

    def record(self, kind: str, message: str, details: dict[str, object]) -> None:
        self._db.add_event(
            timestamp=self._now().isoformat(),
            kind=kind,
            message=message,
            details=details,
        )

    def list_events(self, limit: int) -> EventList:
        return EventList(
            events=[
                EventItem(
                    id=int(row["id"]),
                    timestamp=_parse_time(row["timestamp"]),
                    kind=row["kind"],
                    message=row["message"],
                    details=row["details"],
                )
                for row in self._db.list_events(limit)
            ]
        )


class ControlPlane:
    """Facade the routes call. Each attribute is one service."""

    def __init__(
        self,
        *,
        database: Database,
        monitor: SamplingMonitor,
        controller: BandwidthController,
        qos: RecordingQos,
        config: ControlConfig,
        now: Callable[[], datetime],
        dry_run: bool,
    ) -> None:
        self.events = EventService(database, now)
        self.clients = ClientService(database, monitor, controller, qos)
        self.metrics = MetricsService(monitor, database)
        self.policies = PolicyService(database, config, set(config.weights), now, self.events, controller)
        self.allocations = AllocationService(database, controller, qos)
        self.qos = QosService(controller, qos, database, self.events, dry_run=dry_run)
        self._db = database
        self._dry_run = dry_run

    def health(self) -> HealthResponse:
        self._db.ping()
        return HealthResponse(status="ok", database="ok", dry_run=self._dry_run)
