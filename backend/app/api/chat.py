"""Chat, conversation history and assistant reports. All data is scoped to the current
user (see app/deps.py: X-User-Id, else X-Client-Id, else "anonymous").

Mounted at /api/v1 (what the main server's proxy calls today) and at
/internal/v1/assistant (design doc path).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.agent.chat import ChatError
from app.agent.reports import ReportRecord
from app.agent.workflows import WORKFLOWS
from app.db.chat_store import Message, Session
from app.deps import Resources, get_current_user, get_resources

router = APIRouter(tags=["assistant"])
Workflow = Literal["monitoring_report", "system_info", "dose_assessment",
                   "maintenance_advice", "general"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, examples=["请汇总全网站点运行状态"])
    session_id: str | None = Field(default=None, description="omit to start a new session")
    stream: bool = Field(default=True, description="SSE stream (true) or one JSON reply")
    workflow: Workflow | None = Field(
        default=None, description="preset workflow; omitted = chosen from the message")


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class FeedbackRequest(BaseModel):
    rating: int = Field(description="1 = helpful, -1 = not helpful")
    comment: str | None = Field(default=None, max_length=2000)


class ClaimRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.@:\-]+$")


def _sse(event: str, data: dict) -> str:
    seq = data.get("seq")
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _with_heartbeat(events: AsyncIterator[str], every: float) -> AsyncIterator[str]:
    """Forward `events`, inserting an SSE comment while nothing was sent for `every` s."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in events:
                await queue.put(item)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), every)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield item
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


@router.get("/assistant/workflows")
async def workflows():
    """The assistant's preset workflows (the four entry cards)."""
    return [{"id": k, "title": w["title"], "description": w["description"]}
            for k, w in WORKFLOWS.items()]


@router.post("/chat")
async def chat(body: ChatRequest, user: str = Depends(get_current_user),
               r: Resources = Depends(get_resources)):
    """Ask the assistant. With `stream=true` (default) the response is Server-Sent Events:
    `session`, `stage`, `tool`, `result`, `delta`, `citation`, then `done` or `error`;
    every payload has a monotonically increasing `seq` (also the SSE id)."""
    try:
        if not body.stream:
            return await r.chat.ask(user, body.message, body.session_id, body.workflow)
        session, history, user_msg = await r.chat.start(user, body.message, body.session_id)
    except ChatError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) \
            from exc

    async def events() -> AsyncIterator[str]:
        async for event, data in r.chat.stream(user, session, history, user_msg,
                                               body.workflow):
            yield _sse(event, data)

    return StreamingResponse(_with_heartbeat(events(), r.settings.sse_heartbeat_seconds),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "X-Session-Id": session.session_id})


@router.post("/sessions/claim")
async def claim_sessions(body: ClaimRequest, user: str = Depends(get_current_user),
                         r: Resources = Depends(get_resources)):
    """Once the client has login: move the conversations and reports of a browser
    (X-Client-Id) to the identified user (X-User-Id). Call once after the first login."""
    if user == "anonymous" or user.startswith("client:"):
        raise HTTPException(403, "claiming needs an identified user (X-User-Id)")
    source = f"client:{body.client_id}"
    return {"sessions": await r.chat_store.reassign(source, user),
            "reports": await r.reports.reassign(source, user)}


@router.get("/reports", response_model=list[ReportRecord])
async def list_reports(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                       user: str = Depends(get_current_user),
                       r: Resources = Depends(get_resources)):
    """Reports generated by the assistant for this user (history; download again)."""
    return await r.reports.list(user, limit, offset)


@router.get("/reports/{report_id}/download")
async def download_report(report_id: str, user: str = Depends(get_current_user),
                          r: Resources = Depends(get_resources)):
    rec = await r.reports.get(user, report_id)
    path = Path(r.settings.reports_dir) / f"{report_id}.xlsx"
    if rec is None or not path.is_file():
        raise HTTPException(404, "report not found")
    return FileResponse(path, filename=rec.file_name, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))


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
