"""SQLite storage for clients, policy, allocations, events, and usage buckets.

Raw per-sample counters are not stored. Usage is summed into time buckets,
and events and allocation history are capped.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Database:
    def __init__(
        self,
        path: str,
        *,
        bucket_seconds: int,
        retention_seconds: int,
        max_events: int,
        max_allocation_history: int,
    ) -> None:
        if path != ":memory:":
            file_path = Path(path)
            if str(file_path.parent) not in {"", "."}:
                file_path.parent.mkdir(parents=True, exist_ok=True)
        self.bucket_seconds = bucket_seconds
        self.retention_seconds = retention_seconds
        self.max_events = max_events
        self.max_allocation_history = max_allocation_history
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                ipv4 TEXT NOT NULL,
                mac TEXT,
                interface TEXT NOT NULL,
                namespace TEXT
            );
            CREATE TABLE IF NOT EXISTS control_policy (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                congestion_threshold REAL NOT NULL,
                clear_margin REAL NOT NULL,
                total_capacity INTEGER NOT NULL,
                minimum_bandwidth INTEGER NOT NULL,
                maximum_bandwidth INTEGER,
                cooldown REAL NOT NULL,
                change_tolerance_bps INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS client_policy (
                client_id TEXT PRIMARY KEY,
                weight INTEGER NOT NULL,
                min_bandwidth INTEGER,
                max_bandwidth INTEGER
            );
            CREATE TABLE IF NOT EXISTS current_allocation (
                client_id TEXT PRIMARY KEY,
                allocated_bps INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allocation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decided_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                applied INTEGER NOT NULL,
                utilization REAL,
                congested INTEGER NOT NULL,
                allocation_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_buckets (
                client_id TEXT NOT NULL,
                bucket_start INTEGER NOT NULL,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL,
                samples INTEGER NOT NULL,
                rx_rate_samples INTEGER NOT NULL,
                tx_rate_samples INTEGER NOT NULL,
                sum_rx_bps REAL NOT NULL,
                sum_tx_bps REAL NOT NULL,
                PRIMARY KEY (client_id, bucket_start)
            );
            """
        )
        self._conn.commit()

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def replace_clients(self, clients: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM clients")
            self._conn.executemany(
                """
                INSERT INTO clients (client_id, ipv4, mac, interface, namespace)
                VALUES (:client_id, :ip, :mac, :interface, :namespace)
                """,
                clients,
            )
            self._conn.commit()

    def list_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM clients ORDER BY client_id").fetchall()
        return [dict(row) for row in rows]

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return dict(row) if row else None

    def policy_exists(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT id FROM control_policy WHERE id = 1").fetchone()
        return row is not None

    def save_policy(
        self,
        *,
        congestion_threshold: float,
        clear_margin: float,
        total_capacity: int,
        minimum_bandwidth: int,
        maximum_bandwidth: int | None,
        cooldown: float,
        change_tolerance_bps: int,
        weights: dict[str, int],
        updated_at: str,
    ) -> None:
        """Store the shared policy and weights. Per-client min/max overrides are kept."""

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO control_policy (
                    id, congestion_threshold, clear_margin, total_capacity,
                    minimum_bandwidth, maximum_bandwidth, cooldown,
                    change_tolerance_bps, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    congestion_threshold = excluded.congestion_threshold,
                    clear_margin = excluded.clear_margin,
                    total_capacity = excluded.total_capacity,
                    minimum_bandwidth = excluded.minimum_bandwidth,
                    maximum_bandwidth = excluded.maximum_bandwidth,
                    cooldown = excluded.cooldown,
                    change_tolerance_bps = excluded.change_tolerance_bps,
                    updated_at = excluded.updated_at
                """,
                (
                    congestion_threshold,
                    clear_margin,
                    total_capacity,
                    minimum_bandwidth,
                    maximum_bandwidth,
                    cooldown,
                    change_tolerance_bps,
                    updated_at,
                ),
            )
            for client_id, weight in weights.items():
                self._conn.execute(
                    """
                    INSERT INTO client_policy (client_id, weight, min_bandwidth, max_bandwidth)
                    VALUES (?, ?, NULL, NULL)
                    ON CONFLICT(client_id) DO UPDATE SET weight = excluded.weight
                    """,
                    (client_id, weight),
                )
            self._conn.commit()

    def save_client_policy(
        self,
        client_id: str,
        *,
        weight: int,
        min_bandwidth: int | None,
        max_bandwidth: int | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO client_policy (client_id, weight, min_bandwidth, max_bandwidth)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    weight = excluded.weight,
                    min_bandwidth = excluded.min_bandwidth,
                    max_bandwidth = excluded.max_bandwidth
                """,
                (client_id, weight, min_bandwidth, max_bandwidth),
            )
            self._conn.commit()

    def load_policy(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM control_policy WHERE id = 1").fetchone()
            if row is None:
                return None
            clients = self._conn.execute(
                "SELECT * FROM client_policy ORDER BY client_id"
            ).fetchall()
        return {"global": dict(row), "clients": [dict(item) for item in clients]}

    def replace_allocation(
        self,
        allocation: dict[str, int],
        *,
        decided_at: str,
        reason: str,
        applied: bool,
        utilization: float | None,
        congested: bool,
    ) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM current_allocation")
            self._conn.executemany(
                """
                INSERT INTO current_allocation (client_id, allocated_bps, updated_at, reason)
                VALUES (?, ?, ?, ?)
                """,
                [(client_id, bps, decided_at, reason) for client_id, bps in sorted(allocation.items())],
            )
            self._conn.execute(
                """
                INSERT INTO allocation_history (
                    decided_at, reason, applied, utilization, congested, allocation_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decided_at,
                    reason,
                    1 if applied else 0,
                    utilization,
                    1 if congested else 0,
                    json.dumps(allocation, sort_keys=True),
                ),
            )
            self._prune_history()
            self._conn.commit()

    def current_allocation(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM current_allocation ORDER BY client_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def allocation_history(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM allocation_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["allocation"] = json.loads(item.pop("allocation_json"))
            item["applied"] = bool(item["applied"])
            item["congested"] = bool(item["congested"])
            history.append(item)
        return history

    def add_event(self, *, timestamp: str, kind: str, message: str, details: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (timestamp, kind, message, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (timestamp, kind, message, json.dumps(details, sort_keys=True)),
            )
            self._prune_events()
            self._conn.commit()

    def list_events(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            events.append(item)
        return events

    def add_usage(
        self,
        *,
        client_id: str,
        when: datetime,
        rx_bytes: int,
        tx_bytes: int,
        rx_bps: float | None,
        tx_bps: float | None,
    ) -> None:
        stamp = int(when.timestamp())
        bucket = stamp - (stamp % self.bucket_seconds)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usage_buckets (
                    client_id, bucket_start, rx_bytes, tx_bytes, samples,
                    rx_rate_samples, tx_rate_samples, sum_rx_bps, sum_tx_bps
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(client_id, bucket_start) DO UPDATE SET
                    rx_bytes = rx_bytes + excluded.rx_bytes,
                    tx_bytes = tx_bytes + excluded.tx_bytes,
                    samples = samples + 1,
                    rx_rate_samples = rx_rate_samples + excluded.rx_rate_samples,
                    tx_rate_samples = tx_rate_samples + excluded.tx_rate_samples,
                    sum_rx_bps = sum_rx_bps + excluded.sum_rx_bps,
                    sum_tx_bps = sum_tx_bps + excluded.sum_tx_bps
                """,
                (
                    client_id,
                    bucket,
                    rx_bytes,
                    tx_bytes,
                    0 if rx_bps is None else 1,
                    0 if tx_bps is None else 1,
                    0.0 if rx_bps is None else rx_bps,
                    0.0 if tx_bps is None else tx_bps,
                ),
            )
            self._prune_usage(when)
            self._conn.commit()

    def usage(self, client_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if client_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM usage_buckets ORDER BY client_id, bucket_start"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM usage_buckets
                    WHERE client_id = ?
                    ORDER BY bucket_start
                    """,
                    (client_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def tables(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {row["name"] for row in rows}

    def _prune_events(self) -> None:
        self._conn.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT ?
            )
            """,
            (self.max_events,),
        )

    def _prune_history(self) -> None:
        self._conn.execute(
            """
            DELETE FROM allocation_history
            WHERE id NOT IN (
                SELECT id FROM allocation_history ORDER BY id DESC LIMIT ?
            )
            """,
            (self.max_allocation_history,),
        )

    def _prune_usage(self, now: datetime) -> None:
        cutoff = int(now.timestamp()) - self.retention_seconds
        self._conn.execute("DELETE FROM usage_buckets WHERE bucket_start < ?", (cutoff,))


def utc_now() -> datetime:
    return datetime.now(UTC)
