"""Load the lab interface map. Names outside this file are never shaped."""

from ipaddress import IPv4Address
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bandwidth_manager.enforcement.commands import DEFAULT_CLASS_MINOR, PARENT_MINOR
from bandwidth_manager.monitoring.validate import validate_client_id, validate_interface, validate_namespace


class QosClientConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    ipv4: IPv4Address
    download_interface: str
    upload_interface: str
    class_minor: int = Field(ge=2, le=0xFFE)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return validate_client_id(value)

    @field_validator("download_interface", "upload_interface")
    @classmethod
    def _interface(cls, value: str) -> str:
        return validate_interface(value)

    @field_validator("class_minor")
    @classmethod
    def _minor(cls, value: int) -> int:
        if value in {PARENT_MINOR, DEFAULT_CLASS_MINOR}:
            raise ValueError("class_minor collides with a reserved HTB class")
        return value


class QosConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    default_class_bps: int = Field(default=1_000_000, gt=0)
    max_bandwidth_bps: int = Field(default=10_000_000_000, gt=0)
    dry_run: bool = False
    clients: list[QosClientConfig] = Field(min_length=1)

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: str) -> str:
        return validate_namespace(value)

    @model_validator(mode="after")
    def _unique(self) -> "QosConfig":
        ids = [client.id for client in self.clients]
        minors = [client.class_minor for client in self.clients]
        if len(ids) != len(set(ids)):
            raise ValueError("client ids must be unique")
        if len(minors) != len(set(minors)):
            raise ValueError("class minors must be unique")
        downloads = {client.download_interface for client in self.clients}
        uploads = {client.upload_interface for client in self.clients}
        if downloads & uploads:
            raise ValueError("an interface cannot be both a download and an upload point")
        return self

    def allowlist(self) -> frozenset[str]:
        names: set[str] = set()
        for client in self.clients:
            names.add(client.download_interface)
            names.add(client.upload_interface)
        return frozenset(names)


def load_qos_config(path: Path) -> QosConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    return QosConfig.model_validate(raw)
