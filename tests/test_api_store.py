"""SQLite retention. Raw samples are not a table."""

from datetime import UTC, datetime

from bandwidth_manager.api.db import Database


def _db() -> Database:
    return Database(
        ":memory:",
        bucket_seconds=60,
        retention_seconds=120,
        max_events=2,
        max_allocation_history=2,
    )


def test_usage_is_aggregated_and_old_buckets_are_dropped():
    database = _db()
    when = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    database.add_usage(
        client_id="client-a",
        when=when,
        rx_bytes=0,
        tx_bytes=100,
        rx_bps=None,
        tx_bps=800.0,
    )
    database.add_usage(
        client_id="client-a",
        when=when,
        rx_bytes=0,
        tx_bytes=50,
        rx_bps=None,
        tx_bps=400.0,
    )
    rows = database.usage("client-a")
    assert len(rows) == 1
    assert rows[0]["samples"] == 2
    assert rows[0]["tx_bytes"] == 150
    assert rows[0]["sum_tx_bps"] == 1200.0
    assert rows[0]["tx_rate_samples"] == 2
    assert rows[0]["rx_rate_samples"] == 0

    database.add_usage(
        client_id="client-a",
        when=datetime(2020, 1, 1, tzinfo=UTC),
        rx_bytes=1,
        tx_bytes=1,
        rx_bps=1.0,
        tx_bps=1.0,
    )
    database.add_usage(
        client_id="client-b",
        when=datetime(2026, 10, 6, 12, 5, tzinfo=UTC),
        rx_bytes=5,
        tx_bytes=5,
        rx_bps=5.0,
        tx_bps=5.0,
    )
    kept = {row["client_id"] for row in database.usage()}
    assert kept == {"client-b"}
    assert "traffic_samples" not in database.tables()
    assert "usage_buckets" in database.tables()


def test_events_and_allocation_history_are_capped():
    database = _db()
    for index in range(5):
        database.add_event(
            timestamp="2026-10-06T00:00:00+00:00",
            kind="note",
            message=str(index),
            details={"index": index},
        )
        database.replace_allocation(
            {"client-a": index},
            decided_at="2026-10-06T00:00:00+00:00",
            reason="applied",
            applied=True,
            utilization=0.9,
            congested=True,
        )
    messages = [row["message"] for row in database.list_events(10)]
    assert messages == ["4", "3"]
    history = database.allocation_history(10)
    assert [row["allocation"]["client-a"] for row in history] == [4, 3]
    assert database.current_allocation()[0]["allocated_bps"] == 4
