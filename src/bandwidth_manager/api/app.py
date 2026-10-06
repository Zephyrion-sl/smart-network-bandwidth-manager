"""FastAPI application. OpenAPI is served by FastAPI at /docs and /openapi.json."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bandwidth_manager.api.errors import ApiFailure
from bandwidth_manager.api.runtime import Runtime
from bandwidth_manager.api.routes import router

logger = logging.getLogger(__name__)
_STATIC = Path(__file__).resolve().parent / "static"


class AccessLog:
    """ASGI access log. This is not BaseHTTPMiddleware, which re-raises handled errors."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # type: ignore[misc]
            return
        status = {"code": 500}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)  # type: ignore[misc]
        finally:
            logger.info("http %s %s %s", scope.get("method"), scope.get("path"), status["code"])


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(
        title="Bandwidth manager",
        version="0.1.0",
        description=(
            "Control plane for the bandwidth lab. Routes call services. "
            "Services call the monitor, congestion check, allocator, and QoS manager."
        ),
    )
    app.state.runtime = runtime
    app.add_middleware(AccessLog)
    if runtime.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(runtime.settings.cors_origins),
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["*"],
        )
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def dashboard_page() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC), name="dashboard-static")

    @app.exception_handler(ApiFailure)
    async def api_failure(_request: Request, exc: ApiFailure) -> JSONResponse:
        logger.warning("api %s %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": jsonable_encoder(exc.details),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("api validation_error %s", exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled API error")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "internal error", "details": None}},
        )

    return app
