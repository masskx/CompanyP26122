from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from pydantic import SecretStr, ValidationError

from industrial_ai.api.deps import build_container
from industrial_ai.api.main import create_app
from industrial_ai.core.config import Settings

from .conftest import TOKEN

pytestmark = pytest.mark.anyio

SVC = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "test-hmac-secret-that-is-long-enough-for-hs256"


async def me(settings: Settings, headers: dict) -> httpx.Response:
    container = build_container(settings)
    app = create_app(container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        resp = await c.get("/internal/v1/users/me", headers=headers)
    await container.aclose()
    return resp


def make_settings(settings, **overrides) -> Settings:
    return Settings(**{**settings.model_dump(), **overrides, "_env_file": None})


async def test_trusted_gateway_requires_service_token_first(settings):
    # A user header without the service token is never trusted.
    resp = await me(settings, {"X-User-Id": "alice"})
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"

    resp = await me(settings, {**SVC, "X-User-Id": "alice", "X-User-Name": "Alice",
                               "X-User-Roles": "operator, radiation_protection, bad role!"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["issuer"] == "mes-main-backend" and body["subject"] == "alice"
    assert body["roles"] == ["operator", "radiation_protection"]
    assert body["user_id"].startswith("usr_")


async def test_missing_or_invalid_user_identity(settings):
    resp = await me(settings, SVC)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "user_identity_required"
    resp = await me(settings, {**SVC, "X-User-Id": "x" * 300})
    assert resp.json()["error"]["code"] == "invalid_user_identity"


def _jwt_settings(settings) -> Settings:
    return make_settings(settings, user_identity_mode="jwt", user_jwt_algorithms=["HS256"],
                         user_jwt_key=SecretStr(SECRET), user_jwt_issuer="client-idp",
                         user_jwt_audience="industrial-ai")


def _token(**claims) -> str:
    base = {"sub": "bob", "iss": "client-idp", "aud": "industrial-ai", "name": "Bob",
            "roles": ["operator"], "exp": datetime.now(UTC) + timedelta(minutes=5)}
    return jwt.encode({**base, **claims}, SECRET, algorithm="HS256")


async def test_jwt_assertion(settings):
    s = _jwt_settings(settings)
    resp = await me(s, {**SVC, "X-User-Assertion": _token()})
    assert resp.status_code == 200
    assert resp.json()["subject"] == "bob" and resp.json()["auth_method"] == "jwt"

    # In jwt mode a bare X-User-Id is ignored, never trusted.
    resp = await me(s, {**SVC, "X-User-Id": "mallory"})
    assert resp.json()["error"]["code"] == "user_identity_required"


@pytest.mark.parametrize("claims", [
    {"exp": datetime.now(UTC) - timedelta(minutes=5)},
    {"aud": "someone-else"},
    {"iss": "evil-idp"},
])
async def test_jwt_rejections(settings, claims):
    resp = await me(_jwt_settings(settings), {**SVC, "X-User-Assertion": _token(**claims)})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_user_assertion"


async def test_jwt_bad_signature(settings):
    forged = jwt.encode({"sub": "bob", "iss": "client-idp", "aud": "industrial-ai",
                         "exp": datetime.now(UTC) + timedelta(minutes=5)},
                        "a-different-secret-of-sufficient-length!!", algorithm="HS256")
    resp = await me(_jwt_settings(settings), {**SVC, "X-User-Assertion": forged})
    assert resp.json()["error"]["code"] == "invalid_user_assertion"


async def test_dev_mode(settings):
    resp = await me(make_settings(settings, user_identity_mode="dev"), SVC)
    assert resp.json()["subject"] == "dev-user" and resp.json()["issuer"] == "dev"


def test_dev_mode_refused_in_production():
    with pytest.raises(ValidationError, match="not allowed"):
        Settings(_env_file=None, app_env="production", user_identity_mode="dev")


def test_jwt_mode_requires_key_and_audience():
    with pytest.raises(ValidationError, match="USER_JWT_KEY"):
        Settings(_env_file=None, user_identity_mode="jwt")
