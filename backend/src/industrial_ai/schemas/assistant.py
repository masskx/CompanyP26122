from datetime import datetime
from typing import Literal

from pydantic import Field

from industrial_ai.schemas.common import ApiModel


class ChatRequest(ApiModel):
    session_id: str | None = None
    message: str = Field(min_length=1)


class ChatMessage(ApiModel):
    message_id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    knowledge_index_version: str | None = None


class ChatSession(ApiModel):
    session_id: str
    user_id: str | None
    created_at: datetime
