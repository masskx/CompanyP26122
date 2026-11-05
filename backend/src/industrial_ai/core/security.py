"""Service-to-service authentication.

The MES main backend authenticates users and checks permissions. This service only
verifies that the caller is the main backend (shared bearer token). User headers are
audit metadata and are never used for authentication.
"""

import logging
import secrets
from dataclasses import dataclass, field

from fastapi import Depends, Header, Request

from industrial_ai.core.config import Settings
from industrial_ai.core.errors import AppError

logger = logging.getLogger("industrial_ai.audit")


@dataclass(frozen=True)
class CallerContext:
    request_id: str | None
    user_id: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)


def _settings(request: Request) -> Settings:
    return request.app.state.container.settings


def require_service_token(
    request: Request,
    settings: Settings = Depends(_settings),
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_roles: str | None = Header(default=None),
) -> CallerContext:
    request_id = getattr(request.state, "request_id", None)
    client = request.client.host if request.client else None
    expected = settings.compute_service_token

    if expected is None or not expected.get_secret_value():
        logger.error(
            "service token not configured; rejecting request",
            extra={"fields": {"event": "auth_rejected", "reason": "not_configured",
                              "client": client, "path": request.url.path}},
        )
        raise AppError("auth_not_configured", "服务认证未配置", status_code=503)

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.encode(), expected.get_secret_value().encode()
    ):
        logger.warning(
            "invalid service token",
            extra={"fields": {"event": "auth_rejected", "reason": "invalid_token",
                              "client": client, "path": request.url.path,
                              "user_id": x_user_id}},
        )
        raise AppError("unauthorized", "服务令牌无效", status_code=401)

    roles = tuple(r.strip() for r in (x_user_roles or "").split(",") if r.strip())
    return CallerContext(request_id=request_id, user_id=x_user_id, roles=roles)
