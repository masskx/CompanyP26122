"""Assistant API. Every route requires the service token AND an end-user identity; all data
is scoped to that user. There are deliberately no document upload/delete/index endpoints:
the knowledge base is built offline (industrial-ai-kb)."""

from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query, Response, status
from fastapi.responses import StreamingResponse

from industrial_ai.api.deps import Container, get_container
from industrial_ai.api.routers.sse import sse_response
from industrial_ai.core.identity import Principal, require_user
from industrial_ai.schemas.assistant import (
    ChatRequest,
    ChatSession,
    FeedbackCreate,
    MessagePage,
    SessionCreate,
    SessionPage,
    SessionUpdate,
)

router = APIRouter(prefix="/assistant", tags=["assistant"])

_SESSION_ID = Path(pattern=r"^cs_[0-9a-f]{32}$")


@router.post("/chat", response_class=StreamingResponse)
async def chat(
    body: ChatRequest,
    user: Principal = Depends(require_user),
    c: Container = Depends(get_container),
) -> StreamingResponse:
    """Ask a question in a new (no session_id) or existing session. Streams SSE events:
    stage, delta, citation, result, error, done."""
    turn = await c.assistant.start_turn(user, body)  # errors before streaming starts

    async def stream() -> AsyncIterator[str]:
        async for event in c.assistant.stream_turn(turn):
            yield event.to_sse()

    return sse_response(stream(), headers={"X-Session-Id": turn.session.session_id})


@router.post("/sessions", response_model=ChatSession, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, user: Principal = Depends(require_user),
                         c: Container = Depends(get_container)) -> ChatSession:
    return await c.assistant.create_session(user, body.title)


@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    before: datetime | None = Query(default=None, description="next_before from previous page"),
    include_archived: bool = False,
    user: Principal = Depends(require_user),
    c: Container = Depends(get_container),
) -> SessionPage:
    return await c.assistant.list_sessions(user, limit=limit, before=before,
                                           include_archived=include_archived)


@router.get("/sessions/{session_id}", response_model=ChatSession)
async def get_session(session_id: str = _SESSION_ID, user: Principal = Depends(require_user),
                      c: Container = Depends(get_container)) -> ChatSession:
    return await c.assistant.get_session(user, session_id)


@router.patch("/sessions/{session_id}", response_model=ChatSession)
async def update_session(body: SessionUpdate, session_id: str = _SESSION_ID,
                         user: Principal = Depends(require_user),
                         c: Container = Depends(get_container)) -> ChatSession:
    return await c.assistant.update_session(user, session_id, title=body.title,
                                            archived=body.archived)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str = _SESSION_ID, user: Principal = Depends(require_user),
                         c: Container = Depends(get_container)) -> Response:
    await c.assistant.delete_session(user, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", response_model=MessagePage)
async def list_messages(
    session_id: str = _SESSION_ID,
    limit: int = Query(default=50, ge=1, le=200),
    before_seq: int | None = Query(default=None, ge=1),
    user: Principal = Depends(require_user),
    c: Container = Depends(get_container),
) -> MessagePage:
    return await c.assistant.list_messages(user, session_id, limit=limit, before_seq=before_seq)


@router.post("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(body: FeedbackCreate,
                   message_id: str = Path(pattern=r"^msg_[0-9a-f]{32}$"),
                   user: Principal = Depends(require_user),
                   c: Container = Depends(get_container)) -> Response:
    await c.assistant.add_feedback(user, message_id, body.rating, body.comment)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
