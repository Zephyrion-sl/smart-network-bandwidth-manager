"""Load monitor configuration. Addresses and names are validated before use."""

from ipaddress import IPv4Address
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bandwidth_manager.monitoring.collector import ClientEndpoint
from bandwidth_manager.monitoring.validate import validate_client_id, validate_interface, validate_namespace


class ClientConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    ipv4: IPv4Address
    interface: str
    namespace: str | None = None
    link_capacity_bps: int | None = Field(default=None, gt=0)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return validate_client_id(value)

    @field_validator("interface")
    @classmethod
    def _interface(cls, value: str) -> str:
        return validate_interface(value)

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_namespace(value)

    def endpoint(self) -> ClientEndpoint:
        return ClientEndpoint(
            client_id=self.id,
            ip_address=str(self.ipv4),
            interface=self.interface,
            namespace=self.namespace,
            link_capacity_bps=self.link_capacity_bps,
        )


class MonitorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_interval_seconds: float = Field(default=1.0, gt=0)
    link_capacity_bps: int | None = Field(default=None, gt=0)
    clients: list[ClientConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_clients(self) -> "MonitorConfig":
        ids = [client.id for client in self.clients]
        if len(ids) != len(set(ids)):
            raise ValueError("client ids must be unique")
        return self

    def endpoints(self) -> list[ClientEndpoint]:
        return [client.endpoint() for client in self.clients]


def load_monitor_config(path: Path) -> MonitorConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    return MonitorConfig.model_validate(raw)
