"""FastAPI control plane. Networking implementations stay in their own packages."""

from bandwidth_manager.api.app import create_app

__all__ = ["create_app"]
