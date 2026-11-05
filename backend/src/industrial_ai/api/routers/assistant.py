from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse

from industrial_ai.api.deps import Container, get_container
from industrial_ai.api.routers.sse import sse_response
from industrial_ai.core.security import CallerContext, require_service_token
from industrial_ai.schemas.assistant import ChatMessage, ChatRequest

router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    body: ChatRequest,
    caller: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> StreamingResponse:
    session = await c.assistant.open_session(body, caller)  # errors before streaming starts

    async def stream() -> AsyncIterator[str]:
        async for event in c.assistant.stream_reply(session, body):
            yield event.to_sse()

    return sse_response(stream(), headers={"X-Session-Id": session.session_id})


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessage])
async def list_messages(
    session_id: str = Path(pattern=r"^cs_[0-9a-f]{32}$"),
    caller: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> list[ChatMessage]:
    return await c.assistant.list_messages(session_id, caller)
