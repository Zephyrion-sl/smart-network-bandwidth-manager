"""FastAPI dependencies. Handlers receive services; they do not construct them."""

from fastapi import Request

from bandwidth_manager.api.errors import RejectionError
from bandwidth_manager.api.services import ControlPlane
from bandwidth_manager.monitoring.validate import validate_client_id


def get_plane(request: Request) -> ControlPlane:
    return request.app.state.runtime.plane


def parse_client_id(client_id: str) -> str:
    try:
        return validate_client_id(client_id)
    except ValueError as exc:
        raise RejectionError(str(exc)) from exc
