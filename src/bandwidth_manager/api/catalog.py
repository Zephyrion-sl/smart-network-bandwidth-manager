"""Client identity known to the control plane. Measurements live elsewhere."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientIdentity:
    client_id: str
    ip: str
    mac: str | None
    interface: str
    namespace: str | None
