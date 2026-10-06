"""Failures the control plane reports to callers.

These types carry a status code and a stable error code. The FastAPI app
turns them into one JSON shape. Route functions do not build that JSON.
"""


class ApiFailure(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(ApiFailure):
    status_code = 404
    code = "not_found"


class RejectionError(ApiFailure):
    status_code = 422
    code = "validation_error"


class NotEnforcedError(ApiFailure):
    status_code = 409
    code = "not_enforced"
