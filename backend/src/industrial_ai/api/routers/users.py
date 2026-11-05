from fastapi import APIRouter, Depends

from industrial_ai.api.deps import Container, get_container
from industrial_ai.core.identity import Principal, require_user
from industrial_ai.schemas.assistant import UserInfo

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserInfo)
async def me(user: Principal = Depends(require_user),
             c: Container = Depends(get_container)) -> UserInfo:
    """The end user as resolved from the request (useful when integrating the client)."""
    return await c.assistant.me(user)
