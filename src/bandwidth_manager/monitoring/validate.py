"""Reject names that must never be interpolated into a path or an argv list."""

import re

INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def validate_interface(name: str) -> str:
    if not INTERFACE_RE.fullmatch(name):
        raise ValueError(f"invalid interface name: {name!r}")
    return name


def validate_namespace(name: str) -> str:
    if not NAMESPACE_RE.fullmatch(name):
        raise ValueError(f"invalid network namespace name: {name!r}")
    return name


def validate_client_id(name: str) -> str:
    if not CLIENT_ID_RE.fullmatch(name):
        raise ValueError(f"invalid client id: {name!r}")
    return name
