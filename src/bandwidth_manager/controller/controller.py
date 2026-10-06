"""One control tick: measure, detect, allocate, enforce only if the share changed."""

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from bandwidth_manager.allocation.allocator import allocate
from bandwidth_manager.allocation.congestion import utilization
from bandwidth_manager.controller.policy import (
    client_requests,
    cooldown_active,
    decide,
    measured_load_bps,
    next_congested,
)
from bandwidth_manager.controller.state import ControlState, DecisionRecord
from bandwidth_manager.enforcement.manager import QoSManager
from bandwidth_manager.monitoring.service import MonitoringService

logger = logging.getLogger(__name__)


class ControlConfig:
    """Knobs for the loop. Bandwidth fields are bits per second."""

    def __init__(
        self,
        *,
        sampling_interval: float,
        congestion_threshold: float,
        total_capacity: int,
        minimum_bandwidth: int,
        maximum_bandwidth: int | None,
        weights: Mapping[str, int],
        cooldown: float,
        clear_margin: float = 0.1,
        change_tolerance_bps: int = 1_000_000,
        idle_bps: int = 10_000,
        client_minimums: Mapping[str, int] | None = None,
        client_maximums: Mapping[str, int | None] | None = None,
    ) -> None:
        if sampling_interval <= 0:
            raise ValueError("sampling_interval must be positive")
        if not 0 < congestion_threshold <= 1:
            raise ValueError("congestion_threshold must be in (0, 1]")
        if clear_margin < 0 or clear_margin >= congestion_threshold:
            raise ValueError("clear_margin must be >= 0 and smaller than congestion_threshold")
        if not isinstance(total_capacity, int) or isinstance(total_capacity, bool) or total_capacity <= 0:
            raise ValueError("total_capacity must be a positive int")
        if not isinstance(minimum_bandwidth, int) or isinstance(minimum_bandwidth, bool) or minimum_bandwidth < 0:
            raise ValueError("minimum_bandwidth must be >= 0")
        if maximum_bandwidth is not None and (
            not isinstance(maximum_bandwidth, int) or isinstance(maximum_bandwidth, bool) or maximum_bandwidth <= 0
        ):
            raise ValueError("maximum_bandwidth must be positive when set")
        if cooldown < 0:
            raise ValueError("cooldown must be >= 0")
        if not isinstance(change_tolerance_bps, int) or change_tolerance_bps < 0:
            raise ValueError("change_tolerance_bps must be >= 0")
        if not weights:
            raise ValueError("weights must name at least one client")
        for client_id, weight in weights.items():
            if not isinstance(weight, int) or isinstance(weight, bool) or weight < 1:
                raise ValueError(f"weight for {client_id} must be a positive int")
        self.sampling_interval = sampling_interval
        self.congestion_threshold = congestion_threshold
        self.total_capacity = total_capacity
        self.minimum_bandwidth = minimum_bandwidth
        self.maximum_bandwidth = maximum_bandwidth
        self.weights = dict(weights)
        self.cooldown = cooldown
        self.clear_margin = clear_margin
        self.change_tolerance_bps = change_tolerance_bps
        self.idle_bps = idle_bps
        self.client_minimums = dict(client_minimums or {})
        self.client_maximums = dict(client_maximums or {})
        for client_id, minimum in self.client_minimums.items():
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise ValueError(f"minimum override for {client_id} must be >= 0")
        for client_id, maximum in self.client_maximums.items():
            if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0):
                raise ValueError(f"maximum override for {client_id} must be positive when set")

    @property
    def client_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.weights))


class BandwidthController:
    def __init__(
        self,
        config: ControlConfig,
        monitor: MonitoringService,
        qos: QoSManager,
        *,
        state: ControlState | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_time: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._monitor = monitor
        self._qos = qos
        self.state = state or ControlState()
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._lock = threading.RLock()

    def tick(self) -> DecisionRecord:
        """Run one pass of the loop and append a decision record."""

        with self._lock:
            return self._tick()

    def exclusive(self) -> threading.RLock:
        """Lock shared with policy updates so a tick does not read a half-written config."""

        return self._lock

    def installed_allocation(self) -> dict[str, int]:
        """Share installed by a successful tc call in this process. Empty after restart."""

        with self._lock:
            return dict(self.state.allocation)

    def live_status(self) -> tuple[bool, float | None, dict[str, int]]:
        """Congestion, last utilization, and the share this process last installed."""

        with self._lock:
            observed = None
            if self.state.decisions:
                observed = self.state.decisions[-1].observed_utilization
            return self.state.congested, observed, dict(self.state.allocation)

    def _tick(self) -> DecisionRecord:
        previous = dict(self.state.allocation)
        loads, missing = self._loads()
        if missing:
            return self._record(
                utilization_value=None,
                congested=self.state.congested,
                previous=previous,
                desired=previous,
                reason="insufficient_samples",
                applied=False,
            )

        used = sum(loads.values())
        ratio = utilization(used, self._config.total_capacity)
        congested = next_congested(
            self.state.congested,
            used,
            self._config.total_capacity,
            self._config.congestion_threshold,
            self._config.clear_margin,
        )
        desired = self._desired(loads) if congested else {}
        cooling = cooldown_active(self._monotonic(), self.state.last_change_monotonic, self._config.cooldown)
        reason, touch = decide(
            previous,
            desired,
            congested=congested,
            cooldown=cooling,
            tolerance_bps=self._config.change_tolerance_bps,
        )
        applied = False
        if touch:
            outcome = self._qos.apply_allocation(desired) if desired else self._qos.clear_all_rules()
            applied = outcome.enforced
            if applied:
                self.state.allocation = dict(desired)
                self.state.last_change_monotonic = self._monotonic()
            else:
                reason = "enforcement_failed"
        self.state.congested = congested
        return self._record(
            utilization_value=ratio,
            congested=congested,
            previous=previous,
            desired=desired,
            reason=reason,
            applied=applied,
        )

    def run(self, iterations: int | None = None) -> None:
        """Repeat tick on sampling_interval. None runs until interrupted."""

        completed = 0
        while iterations is None or completed < iterations:
            started = self._monotonic()
            self.tick()
            completed += 1
            if iterations is not None and completed >= iterations:
                return
            delay = self._config.sampling_interval - (self._monotonic() - started)
            if delay > 0:
                self._sleep(delay)

    def _loads(self) -> tuple[dict[str, int], bool]:
        by_id = {row.client_id: row for row in self._monitor.sample_once()}
        loads: dict[str, int] = {}
        for client_id in self._config.client_ids:
            row = by_id.get(client_id)
            if row is None:
                return {}, True
            load = measured_load_bps(row.instantaneous_rx_bps, row.instantaneous_tx_bps)
            if load is None:
                return {}, True
            loads[client_id] = load
        return loads, False

    def _desired(self, loads: Mapping[str, int]) -> dict[str, int]:
        result = allocate(
            self._config.total_capacity,
            client_requests(
                loads,
                weights=self._config.weights,
                minimum_bandwidth=self._config.minimum_bandwidth,
                maximum_bandwidth=self._config.maximum_bandwidth,
                idle_bps=self._config.idle_bps,
                minimum_overrides=self._config.client_minimums,
                maximum_overrides=self._config.client_maximums,
            ),
        )
        return {
            row.client_id: row.allocated_bandwidth
            for row in result.allocations
            if row.allocated_bandwidth > 0
        }

    def _record(
        self,
        *,
        utilization_value: float | None,
        congested: bool,
        previous: dict[str, int],
        desired: dict[str, int],
        reason: str,
        applied: bool,
    ) -> DecisionRecord:
        record = DecisionRecord(
            timestamp=self._wall_time(),
            observed_utilization=utilization_value,
            congestion_state=congested,
            previous_allocation=dict(previous),
            new_allocation=dict(desired),
            reason=reason,
            applied=applied,
        )
        self.state.decisions.append(record)
        logger.info("control %s", json.dumps(record.as_dict(), sort_keys=True))
        return record
