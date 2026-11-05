from datetime import datetime
from typing import Literal

from pydantic import Field

from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.schemas.common import ApiModel


class ChatRequest(ApiModel):
    session_id: str | None = Field(default=None, pattern=r"^cs_[0-9a-f]{32}$")
    message: str = Field(min_length=1)


class Citation(ApiModel):
    """A source the answer relies on. `ref` matches the [n] markers in the answer text."""

    ref: int
    kind: Literal["knowledge", "data"]
    title: str
    source: str | None = None
    # knowledge
    doc_id: str | None = None
    chunk_id: str | None = None
    section: str | None = None
    page: int | None = None
    kb_version: str | None = None
    score: float | None = None
    # data
    equipment_id: str | None = None
    time_range: str | None = None
    snippet: str | None = None


class ChatMessage(ApiModel):
    message_id: str
    session_id: str
    seq: int
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "error", "interrupted"] = "complete"
    created_at: datetime
    citations: list[Citation] = Field(default_factory=list)
    knowledge_index_version: str | None = None
    model: ModelInfo | None = None
    tools_used: list[str] = Field(default_factory=list)
    error_code: str | None = None


class ChatSession(ApiModel):
    session_id: str
    title: str | None
    message_count: int
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class SessionCreate(ApiModel):
    title: str | None = Field(default=None, max_length=200)


class SessionUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class SessionPage(ApiModel):
    items: list[ChatSession]
    next_before: datetime | None = None


class MessagePage(ApiModel):
    items: list[ChatMessage]
    next_before_seq: int | None = None


class FeedbackCreate(ApiModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2000)


class UserInfo(ApiModel):
    user_id: str
    issuer: str
    subject: str
    display_name: str | None
    roles: list[str]
    auth_method: str
