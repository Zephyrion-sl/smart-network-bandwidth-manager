"""Build tc argv arrays for HTB. This module does not execute anything.

Download (traffic toward a client) is shaped on that client's gateway veth,
which carries only that client's frames. Upload (traffic from a client) is
shaped on the gateway WAN veth, where the client source address is still
visible because this lab does not use NAT. Classes use rate = ceil so HTB
does not lend bandwidth above the allocation.

classid minor numbers are hexadecimal in tc. minor 16 is written 1:10.
"""

from collections.abc import Mapping
from ipaddress import IPv4Address

from bandwidth_manager.enforcement.models import TcCommand
from bandwidth_manager.monitoring.validate import validate_interface, validate_namespace

DEFAULT_CLASS_MINOR = 0x999
PARENT_MINOR = 1


class TcCommandBuilder:
    def __init__(
        self,
        *,
        namespace: str,
        client_addresses: Mapping[str, IPv4Address],
        download_interface: Mapping[str, str],
        upload_interface: Mapping[str, str],
        class_minor: Mapping[str, int],
        default_class_bps: int,
    ) -> None:
        self._namespace = validate_namespace(namespace)
        self._addresses = dict(client_addresses)
        self._download = {client_id: validate_interface(name) for client_id, name in download_interface.items()}
        self._upload = {client_id: validate_interface(name) for client_id, name in upload_interface.items()}
        self._minor = dict(class_minor)
        if default_class_bps <= 0:
            raise ValueError("default_class_bps must be positive")
        self._default_class_bps = default_class_bps
        overlap = set(self._download.values()) & set(self._upload.values())
        if overlap:
            raise ValueError(f"interface cannot be both download and upload: {sorted(overlap)}")

    def interfaces(self) -> tuple[str, ...]:
        names = set(self._download.values()) | set(self._upload.values())
        return tuple(sorted(names))

    def build(self, policy_bps: Mapping[str, int]) -> tuple[TcCommand, ...]:
        if not policy_bps:
            return self.clear_commands()
        unknown = set(policy_bps) - set(self._addresses)
        if unknown:
            raise ValueError(f"unknown client in policy: {sorted(unknown)}")
        for client_id, bandwidth in policy_bps.items():
            if not isinstance(bandwidth, int) or isinstance(bandwidth, bool) or bandwidth <= 0:
                raise ValueError(f"bandwidth for {client_id} must be a positive int")

        commands: list[TcCommand] = []
        commands.extend(self._interface_commands(policy_bps, direction="download"))
        commands.extend(self._interface_commands(policy_bps, direction="upload"))
        return tuple(commands)

    def clear_commands(self) -> tuple[TcCommand, ...]:
        return tuple(
            TcCommand(argv=self._argv("qdisc", "del", "dev", interface, "root"), ignore_missing=True)
            for interface in self.interfaces()
        )

    def class_show_commands(self) -> tuple[TcCommand, ...]:
        """Read HTB classes. tc -json reports rate in bits per second."""

        return tuple(
            TcCommand(argv=self._argv("-json", "class", "show", "dev", interface))
            for interface in self.interfaces()
        )

    def _interface_commands(self, policy_bps: Mapping[str, int], *, direction: str) -> list[TcCommand]:
        interfaces = self._download if direction == "download" else self._upload
        match = "dst" if direction == "download" else "src"
        grouped: dict[str, list[str]] = {}
        for client_id in sorted(policy_bps):
            interface = interfaces[client_id]
            grouped.setdefault(interface, []).append(client_id)

        commands: list[TcCommand] = []
        for interface in sorted(grouped):
            client_ids = grouped[interface]
            dedicated = direction == "download" and len(client_ids) == 1
            rates = {client_id: policy_bps[client_id] for client_id in client_ids}
            parent_rate = sum(rates.values())
            if not dedicated:
                parent_rate += self._default_class_bps
            default_minor = self._minor[client_ids[0]] if dedicated else DEFAULT_CLASS_MINOR
            commands.append(
                TcCommand(
                    argv=self._argv(
                        "qdisc",
                        "replace",
                        "dev",
                        interface,
                        "root",
                        "handle",
                        "1:",
                        "htb",
                        "default",
                        f"{default_minor:x}",
                    )
                )
            )
            commands.append(self._class(interface, PARENT_MINOR, None, parent_rate))
            for client_id in client_ids:
                minor = self._minor[client_id]
                commands.append(self._class(interface, minor, PARENT_MINOR, rates[client_id]))
                commands.append(self._filter(interface, minor, match, self._addresses[client_id]))
            if not dedicated:
                commands.append(self._class(interface, DEFAULT_CLASS_MINOR, PARENT_MINOR, self._default_class_bps))
        return commands

    def _class(self, interface: str, minor: int, parent: int | None, rate_bps: int) -> TcCommand:
        parent_ref = "1:" if parent is None else f"1:{parent:x}"
        rate = _rate(rate_bps)
        return TcCommand(
            argv=self._argv(
                "class",
                "replace",
                "dev",
                interface,
                "parent",
                parent_ref,
                "classid",
                f"1:{minor:x}",
                "htb",
                "rate",
                rate,
                "ceil",
                rate,
            )
        )

    def _filter(self, interface: str, minor: int, match: str, address: IPv4Address) -> TcCommand:
        return TcCommand(
            argv=self._argv(
                "filter",
                "replace",
                "dev",
                interface,
                "parent",
                "1:",
                "protocol",
                "ip",
                "prio",
                f"{minor:x}",
                "handle",
                f"{minor:x}",
                "u32",
                "match",
                "ip",
                match,
                f"{address}/32",
                "flowid",
                f"1:{minor:x}",
            )
        )

    def _argv(self, *tc_args: str) -> tuple[str, ...]:
        return ("ip", "netns", "exec", self._namespace, "tc", *tc_args)


def _rate(bps: int) -> str:
    """tc bit unit: 20000000bit is 20 Mbps. kbit is 1000 bits, not 1024."""

    return f"{bps}bit"
