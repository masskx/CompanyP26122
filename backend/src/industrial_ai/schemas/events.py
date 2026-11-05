import json
from typing import Any, Literal

from industrial_ai.schemas.common import ApiModel

EventType = Literal["stage", "delta", "citation", "result", "error", "done"]


class StreamEvent(ApiModel):
    seq: int
    event: EventType
    data: dict[str, Any]

    def to_sse(self) -> str:
        payload = json.dumps({"seq": self.seq, **self.data}, ensure_ascii=False, default=str)
        return f"id: {self.seq}\nevent: {self.event}\ndata: {payload}\n\n"


SSE_HEARTBEAT = ": heartbeat\n\n"
