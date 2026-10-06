"""Wire the already-tested monitor, controller, and QoS manager into the API."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bandwidth_manager.api.adapters import RecordingQos, SamplingMonitor
from bandwidth_manager.api.catalog import ClientIdentity
from bandwidth_manager.api.db import Database
from bandwidth_manager.api.services import ControlPlane
from bandwidth_manager.api.settings import Settings
from bandwidth_manager.controller.controller import BandwidthController, ControlConfig
from bandwidth_manager.controller.__main__ import load_control_config
from bandwidth_manager.enforcement.config import load_qos_config
from bandwidth_manager.enforcement.manager import QoSManager, manager_from_config
from bandwidth_manager.monitoring.collector import TrafficCollector
from bandwidth_manager.monitoring.config import load_monitor_config
from bandwidth_manager.monitoring.linux_collector import LinuxCounterSource
from bandwidth_manager.monitoring.service import MonitoringService


@dataclass
class Runtime:
    settings: Settings
    database: Database
    monitor: SamplingMonitor
    controller: BandwidthController
    qos: RecordingQos
    plane: ControlPlane


def assemble(
    *,
    settings: Settings,
    control: ControlConfig,
    monitor: MonitoringService,
    qos: QoSManager,
    catalog: Sequence[ClientIdentity],
    monotonic: Callable[[], float] | None = None,
    wall_time: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> Runtime:
    """Build the API around existing objects. Tests pass fakes here."""

    known = {client.client_id for client in catalog}
    if known != set(control.weights):
        raise ValueError("monitor clients and control weights must be the same set")
    now = wall_time or (lambda: datetime.now(UTC))
    database = Database(
        settings.database_path,
        bucket_seconds=settings.usage_bucket_seconds,
        retention_seconds=settings.usage_retention_seconds,
        max_events=settings.max_events,
        max_allocation_history=settings.max_allocation_history,
    )
    database.replace_clients(
        [
            {
                "client_id": client.client_id,
                "ip": client.ip,
                "mac": client.mac,
                "interface": client.interface,
                "namespace": client.namespace,
            }
            for client in catalog
        ]
    )
    sampling = SamplingMonitor(monitor, database, now)
    recorder = RecordingQos(qos)
    controller = BandwidthController(
        control,
        sampling,  # type: ignore[arg-type]
        recorder,  # type: ignore[arg-type]
        monotonic=monotonic,
        wall_time=now,
    )
    plane = ControlPlane(
        database=database,
        monitor=sampling,
        controller=controller,
        qos=recorder,
        config=control,
        now=now,
        dry_run=dry_run,
    )
    plane.policies.seed_from_config()
    return Runtime(
        settings=settings,
        database=database,
        monitor=sampling,
        controller=controller,
        qos=recorder,
        plane=plane,
    )


def build_runtime(settings: Settings | None = None) -> Runtime:
    """Load the lab YAML files and the real Linux counter source."""

    settings = settings or Settings.from_env()
    control, monitor_path, qos_path = load_control_config(settings.controller_config)
    if settings.monitor_config is not None:
        monitor_path = settings.monitor_config
    if settings.qos_config is not None:
        qos_path = settings.qos_config
    monitor_config = load_monitor_config(monitor_path)
    qos_config = load_qos_config(qos_path)
    monitor = MonitoringService(
        TrafficCollector(LinuxCounterSource()),
        monitor_config.endpoints(),
        link_capacity_bps=monitor_config.link_capacity_bps,
    )
    qos = manager_from_config(qos_config)
    catalog = [
        ClientIdentity(
            client_id=client.id,
            ip=str(client.ipv4),
            mac=None,
            interface=client.interface,
            namespace=client.namespace,
        )
        for client in monitor_config.clients
    ]
    return assemble(
        settings=settings,
        control=control,
        monitor=monitor,
        qos=qos,
        catalog=catalog,
        dry_run=qos_config.dry_run,
    )
