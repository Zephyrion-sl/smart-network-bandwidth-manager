"""Read kernel interface counters from a sysfs statistics directory.

The live files on Linux are:

    /sys/class/net/<interface>/statistics/rx_bytes
    /sys/class/net/<interface>/statistics/tx_bytes
    /sys/class/net/<interface>/statistics/rx_packets
    /sys/class/net/<interface>/statistics/tx_packets

Those files exist inside the network namespace that owns the interface.
They are not visible from the host namespace for the lab veths.
"""

from pathlib import Path

from bandwidth_manager.monitoring.models import InterfaceCounters
from bandwidth_manager.monitoring.validate import validate_interface

STAT_FILES = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")


class InvalidMeasurement(ValueError):
    """A statistics file was present but its contents were not a counter."""


class SysfsStatisticsReader:
    """Read rx/tx byte and packet counters for one interface under a sysfs root."""

    def __init__(self, sysfs_root: Path = Path("/sys")) -> None:
        self._root = sysfs_root

    def statistics_dir(self, interface: str) -> Path:
        validate_interface(interface)
        return self._root / "class" / "net" / interface / "statistics"

    def read(self, interface: str) -> InterfaceCounters | None:
        directory = self.statistics_dir(interface)
        if not directory.is_dir():
            return None
        values: dict[str, int] = {}
        for name in STAT_FILES:
            values[name] = _read_counter(directory / name, interface)
        return InterfaceCounters(**values)


def _read_counter(path: Path, interface: str) -> int:
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise InvalidMeasurement(f"{interface}: missing counter file {path.name}") from None
    except OSError as exc:
        raise InvalidMeasurement(f"{interface}: cannot read {path.name}: {exc}") from exc
    return parse_counter(text, interface=interface, field=path.name)


def parse_counter(text: str, *, interface: str, field: str) -> int:
    stripped = text.strip()
    if stripped == "" or not stripped.isdecimal():
        raise InvalidMeasurement(f"{interface}: {field} is not an unsigned counter: {text!r}")
    return int(stripped)


def parse_counter_block(text: str, interface: str) -> InterfaceCounters:
    """Parse the four counters in rx_bytes, tx_bytes, rx_packets, tx_packets order."""

    lines = text.splitlines()
    if len(lines) != len(STAT_FILES):
        raise InvalidMeasurement(
            f"{interface}: expected {len(STAT_FILES)} counters, got {len(lines)}"
        )
    values = {
        name: parse_counter(line, interface=interface, field=name)
        for name, line in zip(STAT_FILES, lines, strict=True)
    }
    return InterfaceCounters(**values)
