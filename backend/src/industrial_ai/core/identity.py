"""End-user identity.

Two separate questions are answered for every assistant request:

1. Is the caller the trusted MES main backend?  -> service token (core/security.py)
2. Which end user is this request for?          -> an IdentityProvider (this module)

Step 2 only runs after step 1 succeeds, so a user identity is never accepted from an
unauthenticated caller. Providers:

* TrustedGatewayIdentity (default): the main backend has already authenticated the user
  and forwards X-User-Id / X-User-Name / X-User-Roles. Matches the current design doc.
* JwtAssertionIdentity: the request carries a signed user assertion (JWT) issued by the
  client's identity provider; signature, issuer, audience and expiry are verified, so even
  a compromised forwarder cannot impersonate a user. Use once the client provides tokens.
* DevIdentity: development only (refused when APP_ENV=production).

Users are keyed by (issuer, subject), so switching providers later keeps histories apart
rather than silently merging them.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import jwt
from fastapi import Depends, Request

from industrial_ai.core.config import Settings
from industrial_ai.core.errors import AppError
from industrial_ai.core.security import CallerContext, require_service_token

logger = logging.getLogger("industrial_ai.audit")

_SUBJECT_RE = re.compile(r"^[\w.@:\-]{1,256}$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    display_name: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)
    auth_method: str = "unknown"


class IdentityProvider(Protocol):
    def resolve(self, request: Request) -> Principal | None:
        """Return the end user, None if absent, or raise AppError(401) if invalid."""
        ...


def _clean_subject(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip()
    if not _SUBJECT_RE.fullmatch(value):
        raise AppError("invalid_user_identity", "用户标识格式无效", status_code=401)
    return value


def _clean_roles(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list | tuple):
        return ()
    return tuple(r.strip() for r in values if isinstance(r, str) and _ROLE_RE.fullmatch(r.strip()))


def _clean_name(value: object) -> str | None:
    return value.strip()[:256] if isinstance(value, str) and value.strip() else None


class TrustedGatewayIdentity:
    def __init__(self, issuer: str) -> None:
        self._issuer = issuer

    def resolve(self, request: Request) -> Principal | None:
        subject = _clean_subject(request.headers.get("x-user-id"))
        if subject is None:
            return None
        return Principal(
            issuer=self._issuer, subject=subject,
            display_name=_clean_name(request.headers.get("x-user-name")),
            roles=_clean_roles(request.headers.get("x-user-roles", "")),
            auth_method="trusted_gateway",
        )


class DevIdentity:
    def __init__(self, default_user: str) -> None:
        self._default = default_user

    def resolve(self, request: Request) -> Principal:
        subject = _clean_subject(request.headers.get("x-user-id")) or self._default
        return Principal(issuer="dev", subject=subject,
                         display_name=_clean_name(request.headers.get("x-user-name")),
                         roles=_clean_roles(request.headers.get("x-user-roles", "")),
                         auth_method="dev")


class JwtAssertionIdentity:
    def __init__(self, *, header: str, key: str, algorithms: list[str], issuer: str,
                 audience: str, leeway: int, name_claim: str, roles_claim: str) -> None:
        self._header = header
        self._key = key
        self._algorithms = algorithms
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway
        self._name_claim = name_claim
        self._roles_claim = roles_claim

    def resolve(self, request: Request) -> Principal | None:
        token = request.headers.get(self._header)
        if not token:
            return None
        if token.lower().startswith("bearer "):
            token = token[7:]
        try:
            claims = jwt.decode(
                token, self._key, algorithms=self._algorithms, audience=self._audience,
                issuer=self._issuer, leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("user assertion rejected", extra={"fields": {
                "event": "user_assertion_rejected", "reason": type(exc).__name__,
                "path": request.url.path}})
            raise AppError("invalid_user_assertion", "用户身份凭证无效或已过期",
                           status_code=401) from exc
        return Principal(
            issuer=self._issuer, subject=_clean_subject(str(claims["sub"])) or "",
            display_name=_clean_name(claims.get(self._name_claim)),
            roles=_clean_roles(claims.get(self._roles_claim, [])),
            auth_method="jwt",
        )


def build_identity_provider(settings: Settings) -> IdentityProvider:
    if settings.user_identity_mode == "dev":
        logger.warning("USER_IDENTITY_MODE=dev: user identity is not verified")
        return DevIdentity(settings.dev_default_user)
    if settings.user_identity_mode == "jwt":
        if settings.user_jwt_key_file is not None:
            key = settings.user_jwt_key_file.read_text(encoding="utf-8")
        else:
            assert settings.user_jwt_key is not None
            key = settings.user_jwt_key.get_secret_value()
        assert settings.user_jwt_issuer and settings.user_jwt_audience
        return JwtAssertionIdentity(
            header=settings.user_jwt_header, key=key, algorithms=settings.user_jwt_algorithms,
            issuer=settings.user_jwt_issuer, audience=settings.user_jwt_audience,
            leeway=settings.user_jwt_leeway_seconds, name_claim=settings.user_jwt_name_claim,
            roles_claim=settings.user_jwt_roles_claim,
        )
    return TrustedGatewayIdentity(settings.trusted_gateway_issuer)


def require_user(
    request: Request, _: CallerContext = Depends(require_service_token)
) -> Principal:
    """Service token first, then the end user. Raises 401 if no user can be established."""
    provider: IdentityProvider = request.app.state.container.identity
    principal = provider.resolve(request)
    if principal is None:
        raise AppError("user_identity_required", "缺少用户身份", status_code=401)
    return principal
