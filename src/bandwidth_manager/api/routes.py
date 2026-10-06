"""HTTP routes. Each handler delegates to the service layer."""

from fastapi import APIRouter, Depends, Query

from bandwidth_manager.api.deps import get_plane, parse_client_id
from bandwidth_manager.api.schemas import (
    AllocationsResponse,
    ClientDetail,
    ClientList,
    ClientPolicyWrite,
    EventList,
    HealthResponse,
    MetricsResponse,
    PolicyResponse,
    PolicyWrite,
    QosActionResponse,
)
from bandwidth_manager.api.services import ControlPlane

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(plane: ControlPlane = Depends(get_plane)) -> HealthResponse:
    return plane.health()


@router.get("/clients", response_model=ClientList, tags=["clients"])
def list_clients(plane: ControlPlane = Depends(get_plane)) -> ClientList:
    return plane.clients.list_clients()


@router.get("/clients/{client_id}", response_model=ClientDetail, tags=["clients"])
def get_client(client_id: str = Depends(parse_client_id), plane: ControlPlane = Depends(get_plane)) -> ClientDetail:
    return plane.clients.get_client(client_id)


@router.get("/metrics", response_model=MetricsResponse, tags=["metrics"])
def metrics(plane: ControlPlane = Depends(get_plane)) -> MetricsResponse:
    return plane.metrics.collect()


@router.get("/allocations", response_model=AllocationsResponse, tags=["allocations"])
def allocations(plane: ControlPlane = Depends(get_plane)) -> AllocationsResponse:
    return plane.allocations.current()


@router.get("/policies", response_model=PolicyResponse, tags=["policies"])
def get_policies(plane: ControlPlane = Depends(get_plane)) -> PolicyResponse:
    return plane.policies.get_policy()


@router.post("/policies", response_model=PolicyResponse, tags=["policies"])
def replace_policies(body: PolicyWrite, plane: ControlPlane = Depends(get_plane)) -> PolicyResponse:
    return plane.policies.replace_policy(body)


@router.put("/policies/{client_id}", response_model=PolicyResponse, tags=["policies"])
def update_client_policy(
    body: ClientPolicyWrite,
    client_id: str = Depends(parse_client_id),
    plane: ControlPlane = Depends(get_plane),
) -> PolicyResponse:
    return plane.policies.update_client(client_id, body)


@router.post("/qos/apply", response_model=QosActionResponse, tags=["qos"])
def apply_qos(plane: ControlPlane = Depends(get_plane)) -> QosActionResponse:
    return plane.qos.apply()


@router.post("/qos/clear", response_model=QosActionResponse, tags=["qos"])
def clear_qos(plane: ControlPlane = Depends(get_plane)) -> QosActionResponse:
    return plane.qos.clear()


@router.get("/events", response_model=EventList, tags=["events"])
def events(
    limit: int = Query(default=50, ge=1, le=500),
    plane: ControlPlane = Depends(get_plane),
) -> EventList:
    return plane.events.list_events(limit)
