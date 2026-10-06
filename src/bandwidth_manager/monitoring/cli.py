"""Live view of per-client counters. Rates come from the monitoring service."""

import argparse
import logging
import sys
from pathlib import Path

from bandwidth_manager.monitoring.collector import TrafficCollector
from bandwidth_manager.monitoring.config import load_monitor_config
from bandwidth_manager.monitoring.linux_collector import LinuxCounterSource
from bandwidth_manager.monitoring.models import TrafficMetrics
from bandwidth_manager.monitoring.service import MonitoringService

logger = logging.getLogger(__name__)


def format_table(metrics: list[TrafficMetrics]) -> str:
    header = (
        f"{'Client':<12} {'IP':<16} {'RX Mbps':>10} {'TX Mbps':>10} "
        f"{'Avg RX':>10} {'Avg TX':>10} {'Util%':>8} {'Total':>16}"
    )
    lines = [header]
    for row in metrics:
        lines.append(
            f"{row.client_id:<12} {row.ip_address:<16} "
            f"{_mbps(row.instantaneous_rx_bps):>10} {_mbps(row.instantaneous_tx_bps):>10} "
            f"{_mbps(row.average_rx_bps):>10} {_mbps(row.average_tx_bps):>10} "
            f"{_percent(row.utilization_percent):>8} {row.total_bytes:>16}"
        )
    if len(metrics) == 0:
        lines.append("(no interfaces readable this interval)")
    return "\n".join(lines)


def _mbps(bps: float | None) -> str:
    if bps is None:
        return "-"
    return f"{bps / 1_000_000:.3f}"


def _percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def build_service(config_path: Path) -> tuple[MonitoringService, float]:
    config = load_monitor_config(config_path)
    collector = TrafficCollector(LinuxCounterSource())
    service = MonitoringService(
        collector,
        config.endpoints(),
        link_capacity_bps=config.link_capacity_bps,
    )
    return service, config.sample_interval_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print live per-client throughput from Linux interface counters. "
            "RX/TX are the client interface's receive and transmit directions."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/monitor.example.yaml"),
        help="YAML file listing client namespaces and interfaces",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="sample interval in seconds (overrides the config file)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print one sample and exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not args.config.is_file():
        logger.error("config file not found: %s", args.config)
        return 2

    try:
        service, configured_interval = build_service(args.config)
    except (OSError, ValueError) as exc:
        logger.error("cannot load config: %s", exc)
        return 2

    interval = args.interval if args.interval is not None else configured_interval
    if interval <= 0:
        logger.error("sample interval must be positive")
        return 2

    print(
        "Source: /sys/class/net/<interface>/statistics/{rx,tx}_{bytes,packets} "
        "inside each client namespace. '-' means the rate is not computable yet.",
        flush=True,
    )

    try:
        if args.once:
            print(format_table(service.sample_once()), flush=True)
            return 0
        for batch in service.iter_samples(interval):
            print(format_table(batch), flush=True)
            print(flush=True)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
