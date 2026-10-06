"""Process configuration from the environment. Hosts and paths are not hardcoded."""

import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_path: str = "data/bandwidth.sqlite"
    controller_config: Path = Path("config/controller.example.yaml")
    monitor_config: Path | None = None
    qos_config: Path | None = None
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    usage_bucket_seconds: int = Field(default=60, gt=0)
    usage_retention_seconds: int = Field(default=86_400, gt=0)
    max_events: int = Field(default=500, gt=0)
    max_allocation_history: int = Field(default=200, gt=0)
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        """Read BW_* variables. Unset monitor and QoS paths follow the controller file."""

        cors = tuple(
            origin.strip()
            for origin in os.environ.get("BW_CORS_ORIGINS", "").split(",")
            if origin.strip()
        )
        monitor = os.environ.get("BW_MONITOR_CONFIG")
        qos = os.environ.get("BW_QOS_CONFIG")
        return cls(
            database_path=os.environ.get("BW_DATABASE_PATH", "data/bandwidth.sqlite"),
            controller_config=Path(os.environ.get("BW_CONTROLLER_CONFIG", "config/controller.example.yaml")),
            monitor_config=Path(monitor) if monitor else None,
            qos_config=Path(qos) if qos else None,
            log_level=os.environ.get("BW_LOG_LEVEL", "INFO"),
            host=os.environ.get("BW_HOST", "127.0.0.1"),
            port=int(os.environ.get("BW_PORT", "8000")),
            usage_bucket_seconds=int(os.environ.get("BW_USAGE_BUCKET_SECONDS", "60")),
            usage_retention_seconds=int(os.environ.get("BW_USAGE_RETENTION_SECONDS", "86400")),
            max_events=int(os.environ.get("BW_MAX_EVENTS", "500")),
            max_allocation_history=int(os.environ.get("BW_MAX_ALLOCATION_HISTORY", "200")),
            cors_origins=cors,
        )
