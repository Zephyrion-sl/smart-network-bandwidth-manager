"""Run the control loop against the lab monitor and QoS configs."""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from bandwidth_manager.controller.controller import BandwidthController, ControlConfig
from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.manager import manager_from_config
from bandwidth_manager.monitoring.collector import TrafficCollector
from bandwidth_manager.monitoring.config import load_monitor_config
from bandwidth_manager.monitoring.linux_collector import LinuxCounterSource
from bandwidth_manager.monitoring.service import MonitoringService

logger = logging.getLogger(__name__)


def load_control_config(path: Path) -> tuple[ControlConfig, Path, Path]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    monitor_path = Path(raw.pop("monitor_config"))
    qos_path = Path(raw.pop("qos_config"))
    maximum = raw.get("maximum_bandwidth")
    config = ControlConfig(
        sampling_interval=float(raw["sampling_interval"]),
        congestion_threshold=float(raw["congestion_threshold"]),
        total_capacity=int(raw["total_capacity"]),
        minimum_bandwidth=int(raw["minimum_bandwidth"]),
        maximum_bandwidth=None if maximum is None else int(maximum),
        weights={str(client_id): int(weight) for client_id, weight in raw["weights"].items()},
        cooldown=float(raw["cooldown"]),
        clear_margin=float(raw.get("clear_margin", 0.1)),
        change_tolerance_bps=int(raw.get("change_tolerance_bps", 1_000_000)),
        idle_bps=int(raw.get("idle_bps", 10_000)),
    )
    return config, monitor_path, qos_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bandwidth control loop.")
    parser.add_argument("--config", type=Path, default=Path("config/controller.example.yaml"))
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if not args.config.is_file():
        logger.error("config not found: %s", args.config)
        return 2
    control, monitor_path, qos_path = load_control_config(args.config)
    monitor_config = load_monitor_config(monitor_path)
    qos_config = load_qos_config(qos_path)
    monitor = MonitoringService(
        TrafficCollector(LinuxCounterSource()),
        monitor_config.endpoints(),
        link_capacity_bps=monitor_config.link_capacity_bps,
    )
    qos = manager_from_config(qos_config, dry_run=True if args.dry_run else None)
    BandwidthController(control, monitor, qos).run(iterations=args.iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
