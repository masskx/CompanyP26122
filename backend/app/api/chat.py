"""Chat and conversation history. All data is scoped to the current user (see deps.py)."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.chat import ChatError
from app.db.chat_store import Message, Session
from app.deps import Resources, get_current_user, get_resources

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, examples=["AS01-ISO-RM-02 最近6小时剂量率情况如何？"])
    session_id: str | None = Field(default=None, description="omit to start a new session")
    stream: bool = Field(default=True, description="SSE stream (true) or one JSON reply")


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class FeedbackRequest(BaseModel):
    rating: int = Field(description="1 = helpful, -1 = not helpful")
    comment: str | None = Field(default=None, max_length=2000)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(body: ChatRequest, user: str = Depends(get_current_user),
               r: Resources = Depends(get_resources)):
    """Ask the assistant. With `stream=true` (default) the response is Server-Sent Events:
    `session`, `tool`, `delta`, `citation`, then `done` (stored message) or `error`."""
    try:
        if not body.stream:
            return await r.chat.ask(user, body.message, body.session_id)
        session, history, user_msg = await r.chat.start(user, body.message, body.session_id)
    except ChatError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) \
            from exc

    async def events() -> AsyncIterator[str]:
        async for event, data in r.chat.stream(user, session, history, user_msg):
            yield _sse(event, data)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Session-Id": session.session_id})


@router.get("/sessions", response_model=list[Session])
async def list_sessions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                        user: str = Depends(get_current_user),
                        r: Resources = Depends(get_resources)):
    return await r.chat_store.list_sessions(user, limit, offset)


@router.post("/sessions", response_model=Session, status_code=201)
async def create_session(body: RenameRequest | None = None,
                         user: str = Depends(get_current_user),
                         r: Resources = Depends(get_resources)):
    return await r.chat_store.create_session(user, body.title if body else None)


@router.get("/sessions/{session_id}", response_model=Session)
async def get_session(session_id: str, user: str = Depends(get_current_user),
                      r: Resources = Depends(get_resources)):
    session = await r.chat_store.get_session(user, session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@router.patch("/sessions/{session_id}", response_model=Session)
async def rename_session(session_id: str, body: RenameRequest,
                         user: str = Depends(get_current_user),
                         r: Resources = Depends(get_resources)):
    session = await r.chat_store.rename_session(user, session_id, body.title)
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, user: str = Depends(get_current_user),
                         r: Resources = Depends(get_resources)):
    if not await r.chat_store.delete_session(user, session_id):
        raise HTTPException(404, "session not found")
    return Response(status_code=204)


@router.get("/sessions/{session_id}/messages", response_model=list[Message])
async def list_messages(session_id: str, limit: int = Query(50, ge=1, le=200),
                        before_seq: int | None = Query(None, ge=1),
                        user: str = Depends(get_current_user),
                        r: Resources = Depends(get_resources)):
    if await r.chat_store.get_session(user, session_id) is None:
        raise HTTPException(404, "session not found")
    return await r.chat_store.messages(user, session_id, limit, before_seq)


@router.post("/messages/{message_id}/feedback", status_code=204)
async def feedback(message_id: str, body: FeedbackRequest, user: str = Depends(get_current_user),
                   r: Resources = Depends(get_resources)):
    if body.rating not in (1, -1):
        raise HTTPException(422, "rating must be 1 or -1")
    if not await r.chat_store.set_feedback(user, message_id, body.rating, body.comment):
        raise HTTPException(404, "message not found")
    return Response(status_code=204)
