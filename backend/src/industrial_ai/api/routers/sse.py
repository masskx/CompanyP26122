from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


def sse_response(body: AsyncIterator[str], headers: dict[str, str] | None = None
                 ) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **(headers or {})},
    )
