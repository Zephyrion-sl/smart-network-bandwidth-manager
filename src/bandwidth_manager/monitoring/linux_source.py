"""Read sysfs counters, entering a network namespace when the interface is not local.

Namespace entry uses `ip netns exec` with an argument vector. The command is
assembled only from validated names. There is no shell.
"""

import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from bandwidth_manager.monitoring.models import InterfaceCounters
from bandwidth_manager.monitoring.sysfs import (
    InvalidMeasurement,
    SysfsStatisticsReader,
    parse_counter_block,
)
from bandwidth_manager.monitoring.validate import validate_interface, validate_namespace

logger = logging.getLogger(__name__)

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def sysfs_cat_argv(namespace: str, interface: str) -> list[str]:
    """Argv that prints the four statistics files from inside a namespace."""

    validate_namespace(namespace)
    validate_interface(interface)
    stats = f"/sys/class/net/{interface}/statistics"
    return [
        "ip",
        "netns",
        "exec",
        namespace,
        "cat",
        f"{stats}/rx_bytes",
        f"{stats}/tx_bytes",
        f"{stats}/rx_packets",
        f"{stats}/tx_packets",
    ]


def run_argv(argv: Sequence[str], *, timeout_seconds: float = 5.0) -> subprocess.CompletedProcess[str]:
    ip_path = shutil.which(argv[0])
    if ip_path is None:
        raise FileNotFoundError(f"required command not found: {argv[0]}")
    command = [ip_path, *argv[1:]]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout_seconds,
    )


class LinuxInterfaceSource:
    """Interface counter source used by the lab.

    A client with no namespace is read from this process's sysfs. A client
    with a namespace is read by entering that namespace and catting the same
    statistics files. A missing interface returns None. It is not replaced
    with zeroes.
    """

    def __init__(
        self,
        *,
        sysfs_root: Path = Path("/sys"),
        runner: CommandRunner | None = None,
    ) -> None:
        self._local = SysfsStatisticsReader(sysfs_root)
        self._runner = runner or run_argv

    def read_counters(self, *, namespace: str | None, interface: str) -> InterfaceCounters | None:
        validate_interface(interface)
        if namespace is None:
            return self._local.read(interface)
        validate_namespace(namespace)
        argv = sysfs_cat_argv(namespace, interface)
        try:
            completed = self._runner(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error(
                "failed to read %s in namespace %s: %s",
                interface,
                namespace,
                exc,
            )
            raise InvalidMeasurement(str(exc)) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            logger.error(
                "interface %s in namespace %s is not readable (exit %s): %s",
                interface,
                namespace,
                completed.returncode,
                detail,
            )
            # cat/ip use this phrase when the namespace or the statistics
            # directory is absent. Other failures keep the previous baseline.
            if "No such file" in detail:
                return None
            raise InvalidMeasurement(detail or f"exit {completed.returncode}")
        return parse_counter_block(completed.stdout, interface)
