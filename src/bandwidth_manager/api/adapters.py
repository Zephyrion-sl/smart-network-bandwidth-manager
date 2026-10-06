"""Adapters that sit in front of the monitor and QoS manager.

They do not calculate throughput or build tc commands. The monitor still
does the rates. The manager still builds and runs tc. These wrappers only
remember the last result and fold accepted byte deltas into usage buckets.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from bandwidth_manager.api.db import Database
from bandwidth_manager.enforcement.models import EnforcementResult
from bandwidth_manager.monitoring.models import TrafficMetrics
from bandwidth_manager.monitoring.service import MonitoringService


class SamplingMonitor:
    """The object the controller samples. Each sample updates the usage buckets."""

    def __init__(
        self,
        inner: MonitoringService,
        database: Database,
        now: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._db = database
        self._now = now
        self.latest: dict[str, TrafficMetrics] = {}
        self._session: dict[str, tuple[int, int]] = {}
        self.sampled = False
        self.last_sampled_at: datetime | None = None
        self._lock = threading.Lock()

    def view(self) -> tuple[bool, dict[str, TrafficMetrics]]:
        """Copy of the latest sample. Safe to read while another request samples."""

        with self._lock:
            return self.sampled, dict(self.latest)

    def sample_once(self) -> list[TrafficMetrics]:
        with self._lock:
            return self._sample_once()

    def _sample_once(self) -> list[TrafficMetrics]:
        rows = self._inner.sample_once()
        when = self._now()
        seen = set()
        for row in rows:
            seen.add(row.client_id)
            previous = self._session.get(row.client_id, (0, 0))
            rx_delta = row.session_rx_bytes - previous[0]
            tx_delta = row.session_tx_bytes - previous[1]
            self._session[row.client_id] = (row.session_rx_bytes, row.session_tx_bytes)
            self._db.add_usage(
                client_id=row.client_id,
                when=when,
                rx_bytes=max(0, rx_delta),
                tx_bytes=max(0, tx_delta),
                rx_bps=row.instantaneous_rx_bps,
                tx_bps=row.instantaneous_tx_bps,
            )
            self.latest[row.client_id] = row
        for client_id in list(self.latest):
            if client_id not in seen:
                del self.latest[client_id]
        self.sampled = True
        self.last_sampled_at = when
        return rows


class RecordingQos:
    """Delegates to QoSManager and keeps the last EnforcementResult."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.last: EnforcementResult | None = None

    def apply_allocation(self, allocation: Sequence[object] | Mapping[str, int]) -> EnforcementResult:
        self.last = self._inner.apply_allocation(allocation)  # type: ignore[attr-defined]
        return self.last

    def clear_all_rules(self) -> EnforcementResult:
        self.last = self._inner.clear_all_rules()  # type: ignore[attr-defined]
        return self.last

    def read_installed(self) -> dict[str, int] | None:
        reader = getattr(self._inner, "read_installed", None)
        if reader is None:
            return None
        return reader()
