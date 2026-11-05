"""FastAPI application factory."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from industrial_ai import __version__
from industrial_ai.api.deps import Container, build_container
from industrial_ai.api.routers import assistant, equipment, health, maintenance
from industrial_ai.core.config import Settings, get_settings
from industrial_ai.core.errors import AppError, app_error_handler
from industrial_ai.core.logging import configure_logging, request_id_var

API_PREFIX = "/internal/v1"
_MAX_REQUEST_ID_LEN = 128


class RequestIdMiddleware:
    """Propagate X-Request-Id (generate one if absent) into logs and the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = incoming[:_MAX_REQUEST_ID_LEN] if incoming else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    error = AppError("validation_error", "请求参数校验失败", status_code=422,
                     details={"errors": jsonable_encoder(errors)})
    return JSONResponse(status_code=422, content={"error": error.to_payload()})


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.app_env)
        if getattr(app.state, "container", None) is None:
            app.state.container = build_container(settings)
        try:
            yield
        finally:
            await app.state.container.aclose()

    app = FastAPI(
        title="industrial-ai compute service",
        version=__version__,
        lifespan=lifespan,
        # Internal service: interactive docs only outside production.
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
    )
    app.state.container = container
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    app.include_router(health.router)
    app.include_router(equipment.router, prefix=API_PREFIX)
    app.include_router(maintenance.router, prefix=API_PREFIX)
    app.include_router(assistant.router, prefix=API_PREFIX)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory industrial_ai.api.main:app_factory`."""
    return create_app()
